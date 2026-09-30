# Ported from Puppet 8 lib/puppet/functions/yaml_data.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Data backends: a self-registering ``Backend`` registry.

Every format or provider is a :class:`Backend` subclass. Registration is by
subclassing (``NAMES``, keyed by namespace) rather than an explicit call;
lookup goes through :meth:`Backend.find`/:meth:`Backend.get`/:meth:`Backend.new`.
"""

import contextvars
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import typing as _ty
from typing import NamedTuple

import yaml
from pathlib_next import Path

from .exceptions import BackendError, ConfigError, _one_line
from ._function_provider import LookupContext
from ._yaml_loader import RubySymbol, safe_load, symkeys_to_string

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "Backend",
    "NamePattern",
    "YAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "SopsBackend",
    "EyamlBackend",
    "DotenvBackend",
    "BackendError",
    "RubySymbol",
    "LookupContext",
    "has_hocon",
    "default_backends",
]

#: How long (seconds) to wait for the ``sops`` subprocess before giving up.
#: Kept finite so an unattended lookup never hangs forever on a wedged sops.
SOPS_TIMEOUT = 30

_STRICT_VALUES = ("error", "warning", "off")


def _default_strict() -> str:
    """The call-time default for :attr:`Backend.strict` when not set
    explicitly on the instance -- mirrors how ``yaml_data.rb`` reads
    ``Puppet[:strict]`` at call time rather than at construction.

    Reads ``hyera._invocation._STRICT`` (a ``ContextVar``, default
    ``"warning"``), set from ``invocation.scope.strict`` around the
    top-level lookup entry (``core.Hiera._get``) and reset in ``finally``.
    A level's backend is shared across scopes, so it never stores a scope's
    strictness itself; imported lazily to avoid a hard import-time
    dependency from this module onto ``_invocation``.
    """
    from ._invocation import _STRICT

    return _STRICT.get()


class NamePattern(NamedTuple):
    """A registered name that matches by regex instead of exact string.

    ``regex`` is matched with :meth:`re.Pattern.fullmatch`; its named groups
    are passed as keyword arguments to the backend's constructor.
    """

    display: str
    regex: "re.Pattern[str]"


#: The type of a ``Backend.NAMES``/subclass-``NAMES`` value: one tuple of
#: plain strings and/or :class:`NamePattern` per registered namespace.
_Names = _ty.Mapping[str, _ty.Tuple[_ty.Union[str, NamePattern], ...]]


class Backend:
    """A data format and/or Hiera 5 provider, found by name.

    A subclass registers itself by declaring ``NAMES``, a mapping of
    namespace (one of :attr:`KINDS`) to the names (plain strings and/or
    :class:`NamePattern`) it answers to in that namespace. Puppet function
    names (``data_hash``/``lookup_key``/``data_dig`` values in a v5
    hierarchy), v3 backend names, plain format names and CLI/render names
    are separate namespaces -- a name is only ever looked up within one.

    Anything a backend does not implement raises :class:`NotImplementedError`
    from the methods below (the base class's defaults); callers turn that
    into a specific, user-facing error.
    """

    KINDS: _ty.ClassVar[_ty.Tuple[str, ...]] = ("function", "v3", "format", "render")

    #: ``{kind: (name | NamePattern, ...)}``. Read only from the defining
    #: class's own ``__dict__`` at subclass time, so a subclass never
    #: re-registers its parent's names.
    NAMES: _ty.ClassVar[_Names] = {}

    #: File extensions (with leading dot) this backend's format answers to,
    #: longest-suffix-match, used by :meth:`for_path`.
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = ()

    _REGISTRY: _ty.Dict[str, _ty.Dict[str, _ty.Any]] = {
        kind: {"exact": {}, "patterns": []} for kind in KINDS
    }

    def __init__(
        self,
        conf: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        *,
        strict: _ty.Optional[str] = None,
    ) -> None:
        self.conf: _ty.Mapping[str, _ty.Any] = conf or {}
        if strict is not None and strict not in _STRICT_VALUES:
            raise ValueError(
                "strict must be one of {!r}, not {!r}".format(_STRICT_VALUES, strict)
            )
        self._strict = strict
        self.name: _ty.Optional[str] = type(self)._default_name()

    @property
    def strict(self) -> str:
        """This backend's effective strictness: an explicit constructor
        value, else the call-time default (see :func:`_default_strict`).
        Never cache a result that depends on this -- it can change call to
        call once the ContextVar-backed default lands.
        """
        return self._strict if self._strict is not None else _default_strict()

    @classmethod
    def _default_name(cls):
        for kind in cls.KINDS:
            for entry in cls.NAMES.get(kind, ()):
                if not isinstance(entry, NamePattern):
                    return entry
        return None

    def __init_subclass__(cls, **kwargs: _ty.Any) -> None:
        super().__init_subclass__(**kwargs)
        names = cls.__dict__.get("NAMES", {})
        if not names:
            return
        for kind in names:
            if kind not in Backend.KINDS:
                raise ValueError(
                    "{}: unknown backend kind {!r}; known: {}".format(
                        cls.__name__, kind, Backend.KINDS
                    )
                )
        if "render" in names and "dumps" not in cls.__dict__:
            raise TypeError(
                "{}: a 'render' backend must override dumps()".format(cls.__name__)
            )
        if "format" in names and "loads" not in cls.__dict__:
            raise TypeError(
                "{}: a 'format' backend must override loads()".format(cls.__name__)
            )
        for kind, entries in names.items():
            registry = Backend._REGISTRY.setdefault(kind, {"exact": {}, "patterns": []})
            for entry in entries:
                if isinstance(entry, NamePattern):
                    for existing, other in registry["patterns"]:
                        if existing.display == entry.display:
                            raise ValueError(
                                "{!r} ({} kind) is already registered to {}; "
                                "{} cannot reuse it".format(
                                    entry.display, kind, other.__name__, cls.__name__
                                )
                            )
                    registry["patterns"].append((entry, cls))
                else:
                    other = registry["exact"].get(entry)
                    if other is not None:
                        raise ValueError(
                            "{!r} ({} kind) is already registered to {}; "
                            "{} cannot reuse it".format(
                                entry, kind, other.__name__, cls.__name__
                            )
                        )
                    registry["exact"][entry] = cls

    # -- lookup -----------------------------------------------------------

    @classmethod
    def _match(cls, name, kind="function"):
        registry = cls._REGISTRY.get(kind, {"exact": {}, "patterns": []})
        found = registry["exact"].get(name)
        if found is not None:
            return found, {}
        for pattern, klass in registry["patterns"]:
            m = pattern.regex.fullmatch(name)
            if m:
                return klass, m.groupdict()
        return None, {}

    @classmethod
    def find(
        cls, name: str, kind: str = "function"
    ) -> "_ty.Optional[_ty.Type[Backend]]":
        """The registered class for ``name`` in ``kind``, or ``None``."""
        found, _captures = cls._match(name, kind)
        return found

    @classmethod
    def get(cls, name: str, kind: str = "function") -> "_ty.Type[Backend]":
        """The registered, available class for ``name`` in ``kind``.

        Raises :class:`BackendError` for an unknown name (listing the known
        names) or, via :meth:`check_available`, for a registered backend
        whose optional dependency is missing.
        """
        found = cls.find(name, kind)
        if found is None:
            raise BackendError(
                "Unknown {} backend {!r}; known: {}".format(
                    kind, name, ", ".join(cls.names(kind))
                )
            )
        found.check_available()
        return found

    @classmethod
    def new(
        cls,
        name: str,
        conf: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        *,
        kind: str = "function",
        strict: _ty.Optional[str] = None,
    ) -> "Backend":
        """Instantiate the registered backend for ``name`` in ``kind``.

        Any named groups captured by a matching :class:`NamePattern` are
        passed as constructor keywords. ``.name`` is set to ``name`` (the
        name actually asked for, which may be a pattern instance such as
        ``sops_json``, not the class's default name).
        """
        found, captures = cls._match(name, kind)
        if found is None:
            raise BackendError(
                "Unknown {} backend {!r}; known: {}".format(
                    kind, name, ", ".join(cls.names(kind))
                )
            )
        found.check_available()
        instance = found(conf, strict=strict, **captures)
        instance.name = name
        return instance

    @classmethod
    def names(cls, kind: str = "function") -> _ty.List[str]:
        """Registered names in ``kind``: exact names in registration order,
        then patterns by their :attr:`NamePattern.display`."""
        registry = cls._REGISTRY.get(kind, {"exact": {}, "patterns": []})
        return list(registry["exact"].keys()) + [
            pattern.display for pattern, _klass in registry["patterns"]
        ]

    @classmethod
    def for_path(
        cls, path: _ty.Union[str, "os.PathLike[str]"]
    ) -> "_ty.Optional[_ty.Type[Backend]]":
        """The ``format``-namespace backend class whose :attr:`EXTENSIONS`
        has the longest case-sensitive suffix match against ``path``, or
        ``None``."""
        name = os.fspath(path)
        registry = cls._REGISTRY.get("format", {"exact": {}, "patterns": []})
        candidates = {klass for klass in registry["exact"].values()}
        candidates.update(klass for _pattern, klass in registry["patterns"])
        best_cls = None
        best_len = -1
        for candidate in candidates:
            for ext in candidate.EXTENSIONS:
                if name.endswith(ext) and len(ext) > best_len:
                    best_len = len(ext)
                    best_cls = candidate
        return best_cls

    @classmethod
    def _overrides(cls, method_name):
        return getattr(cls, method_name) is not getattr(Backend, method_name)

    @classmethod
    def implements(cls, op: str) -> bool:
        """Whether this class overrides what ``op`` needs.

        ``load``/``dump`` follow ``loads``/``dumps``; ``data_hash`` is true
        when ``data_hash`` or ``loads`` is overridden (the base
        ``data_hash`` delegates to ``load`` -> ``loads``).
        """
        if op == "load":
            return cls._overrides("loads") or cls._overrides("load")
        if op == "dump":
            return cls._overrides("dumps") or cls._overrides("dump")
        if op == "data_hash":
            return cls._overrides("data_hash") or cls._overrides("loads")
        if op in ("loads", "dumps", "lookup_key", "data_dig"):
            return cls._overrides(op)
        raise ValueError("unknown Backend operation {!r}".format(op))

    @classmethod
    def check_available(cls) -> None:
        """Raise :class:`BackendError` if this backend cannot be used (e.g.
        a missing optional dependency). A no-op by default."""

    # -- serialization (json-module shaped) --------------------------------

    def loads(self, text: str) -> _ty.Any:
        """Parse ``text`` (a ``str``). Raises path-free problem text."""
        raise NotImplementedError(
            "{} does not implement .loads()".format(type(self).__name__)
        )

    def dumps(self, obj: _ty.Any, **kw: _ty.Any) -> str:
        """Render ``obj`` back to text. Raises path-free problem text."""
        raise NotImplementedError(
            "{} does not implement .dumps()".format(type(self).__name__)
        )

    def load(
        self,
        source: _ty.Union[str, "os.PathLike[str]", "_ty.IO[str]", "_ty.IO[bytes]"],
    ) -> _ty.Any:
        """Parse ``source`` -- a path-like or a file object.

        Reads the bytes and decodes them as strict UTF-8 (Puppet's data
        files are read this way: ``pops/lookup/context.rb:53``); a ``str``
        already produced by a text file object is used as-is. A decode
        error, or the problem text of a :class:`BackendError` from
        :meth:`loads`, is re-raised (outside the ``except`` block, so
        neither holds the original as ``__cause__``/``__context__``) as
        ``BackendError("Unable to parse (<path>): <problem>", path=...)``.
        Because ``.path`` is set here, a caller (``Hiera._load_file``) does
        not need to prefix it again.
        """
        is_file_obj = hasattr(source, "read")
        path = getattr(source, "name", "<unknown>") if is_file_obj else source
        problem = None
        try:
            if is_file_obj:
                content = source.read()
                text = content if isinstance(content, str) else content.decode("utf-8")
            else:
                reader = getattr(source, "read_bytes", None)
                if reader is not None:
                    data = reader()
                else:
                    with open(os.fspath(source), "rb") as fh:
                        data = fh.read()
                text = data.decode("utf-8")
            return self.loads(text)
        except UnicodeDecodeError as e:
            problem = str(e)
        except BackendError as e:
            problem = str(e)
        raise BackendError(
            "Unable to parse ({}): {}".format(path, problem), path=str(path)
        )

    def dump(self, obj: _ty.Any, fp: "_ty.IO[str]", **kw: _ty.Any) -> None:
        """Render ``obj`` and write it to the open text file ``fp``."""
        fp.write(self.dumps(obj, **kw))

    # -- Hiera 5 provider hooks ---------------------------------------------

    def _require_path_only(self, path, options) -> None:
        """Puppet's ``Struct[{path=>String[1]}]`` dispatch contract on the
        built-in file functions (``yaml_data.rb:18-21,42-44`` and its
        ``json_data``/``hocon_data``/``sops_data`` siblings): the function
        accepts a single ``path`` location and no hierarchy ``options`` at
        all. Raised whenever there is no path location, or ``options``
        carries anything besides the ``path`` :meth:`~Backend.data_hash`
        itself received (a ``uri`` location, or any user-declared option)."""
        if path is None or set(options) - {"path"}:
            raise ConfigError(
                "'{}' one of 'path', 'paths' 'glob', 'globs' or "
                "'mapped_paths' must be declared in hiera.yaml when using "
                "this data_hash function".format(self.name)
            )

    def data_hash(
        self,
        path: "Path",
        options: _ty.Mapping[str, _ty.Any],
    ) -> _ty.Dict[str, _ty.Any]:
        """The ``data_hash`` provider hook: parse the whole file at
        ``path`` and adapt it into hiera data (see :meth:`_as_data_hash`).

        The base implementation is a *file* function: it accepts only a
        single ``path`` location and no hierarchy ``options``
        (:meth:`_require_path_only`), Puppet's own ``yaml_data``/
        ``json_data``/``hocon_data`` contract.
        """
        self._require_path_only(path, options)
        return self._as_data_hash(self.load(path), path)

    def _as_data_hash(self, parsed, path):
        """Adapt a parsed document into hiera data. The base class is the
        identity; :class:`YAMLBackend` overrides it for ``yaml_data``'s
        non-Hash rule (``yaml_data.rb:27-35``)."""
        return parsed

    def lookup_key(
        self,
        key: str,
        options: _ty.Mapping[str, _ty.Any],
        context: LookupContext,
    ) -> _ty.Any:
        """The ``lookup_key`` provider hook: resolve one dotted ``key`` in
        one hierarchy location. Raises :class:`NotImplementedError` unless
        overridden (see :class:`EyamlBackend`)."""
        raise NotImplementedError(
            "{} does not implement .lookup_key()".format(type(self).__name__)
        )

    def data_dig(
        self,
        key_segments: _ty.Sequence[str],
        options: _ty.Mapping[str, _ty.Any],
        context: LookupContext,
    ) -> _ty.Any:
        """The ``data_dig`` provider hook: resolve one already-split
        ``key_segments`` path in one hierarchy location. Raises
        :class:`NotImplementedError` unless overridden."""
        raise NotImplementedError(
            "{} does not implement .data_dig()".format(type(self).__name__)
        )


class YAMLBackend(Backend):
    NAMES: _ty.ClassVar[_Names] = {"function": ("yaml_data",), "format": ("yaml",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".yaml", ".yml")

    def loads(self, text: str) -> _ty.Any:
        """Parse YAML the way Puppet's ``yaml_data`` does (Ruby Psych
        semantics via :mod:`hyera._yaml_loader`), not PyYAML's own
        Python-flavored resolver."""
        # Psych's rules (types, BOM, one-document, symbol keys/values),
        # ported in ``_yaml_loader``: numbers/booleans/dates/symbols per
        # Ruby's ScalarScanner, not PyYAML's own Python-flavored resolver.
        return safe_load(text)

    def dumps(self, obj: _ty.Any, **kw: _ty.Any) -> str:
        """Render ``obj`` as YAML (block style, sorted keys off, Unicode
        left unescaped)."""
        kw.setdefault("sort_keys", False)
        kw.setdefault("allow_unicode", True)
        kw.setdefault("default_flow_style", False)
        return yaml.safe_dump(obj, **kw)

    def _as_data_hash(self, parsed, path):
        """Port of ``yaml_data.rb:27-35``: a Hash passes through (with any
        ``RubySymbol`` key turned into its plain-string name); ``nil``/
        ``false`` always warn-and-empty; any other non-Hash value errors
        under ``strict == "error"``, else warns-and-empties."""
        if isinstance(parsed, dict):
            return symkeys_to_string(parsed)
        message = "{}: file does not contain a valid yaml hash".format(path)
        if parsed is None or parsed is False:
            _LOGGER.warning(message)
            return {}
        if self.strict == "error":
            raise BackendError(message)
        _LOGGER.warning(message)
        return {}


def _strip_json_comments(text: str) -> str:
    """Blank ``/* ... */`` and ``// ...`` outside string literals, the way
    Ruby's ``json`` gem (MultiJson's ``JsonGem`` adapter) accepts them but
    Python's ``json`` module does not. Replaces every non-newline character
    of a comment with a space, so a later ``JSONDecodeError``'s line/column
    still line up with the original text. Tracks ``"``/``\\`` escapes so a
    comment-*looking* substring inside a string is left alone. An
    unterminated ``/*`` is left in place (its own unbalanced ``/*`` then
    fails in ``json.loads`` exactly as it would without stripping).
    """
    n = len(text)
    out = list(text)
    i = 0
    in_string = False
    escaped = False
    while i < n:
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = (end + 2) if end != -1 else n
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _reject_json_constant(name: str):
    # Ruby's json gem rejects `NaN`/`Infinity`/`-Infinity` outright, unlike
    # Python's own `json.loads`, which accepts them by default.
    raise ValueError("unexpected token '{}'".format(name))


def _has_lone_surrogate(text: str) -> bool:
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def _reject_lone_surrogates(obj) -> None:
    """Walk a parsed JSON value and raise if any string (key or value)
    holds a lone (unpaired) surrogate code point -- Ruby's json gem
    rejects ``"\\ud800"`` ("incomplete surrogate pair"); Python's decoder
    accepts it, keeping the bare surrogate in the resulting ``str``.
    """
    if isinstance(obj, str):
        if _has_lone_surrogate(obj):
            raise ValueError("incomplete surrogate pair")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _has_lone_surrogate(key):
                raise ValueError("incomplete surrogate pair")
            _reject_lone_surrogates(value)
    elif isinstance(obj, list):
        for item in obj:
            _reject_lone_surrogates(item)


class JSONBackend(Backend):
    NAMES: _ty.ClassVar[_Names] = {"function": ("json_data",), "format": ("json",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".json",)

    def loads(self, text: str) -> _ty.Any:
        """Parse JSON the way Ruby's ``json`` gem does: ``/* */``/``//``
        comments allowed, ``NaN``/``Infinity``/``-Infinity`` and a lone
        surrogate rejected."""
        problem = None
        try:
            result = json.loads(
                _strip_json_comments(text), parse_constant=_reject_json_constant
            )
            _reject_lone_surrogates(result)
            return result
        except json.JSONDecodeError as e:
            problem = "{} at line {} column {}".format(e.msg, e.lineno, e.colno)
        except ValueError as e:
            problem = str(e)
        # Outside the except block, matching YAMLBackend's chain-free style.
        raise BackendError(problem)

    def dumps(self, obj: _ty.Any, **kw: _ty.Any) -> str:
        """Render ``obj`` as JSON, with non-ASCII characters left as-is."""
        kw.setdefault("ensure_ascii", False)
        return json.dumps(obj, **kw)


_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _find_hocon_string_end(text: str, start: int) -> "tuple[int, str]":
    """``text[start]`` is a double quote. Return ``(end, content)`` for a
    plain or triple-quoted HOCON string: ``end`` is the index just past the
    closing quote(s), ``content`` is the (still escaped) text between them.
    """
    n = len(text)
    if text.startswith('"""', start):
        content_start = start + 3
        close = text.find('"""', content_start)
        if close == -1:
            return n, text[content_start:n]
        # HOCON's own triple-quoted string ends at the LAST quote of a
        # run of 3+ consecutive quotes, not the first matching triple --
        # ``"""x""""`` (4 trailing quotes) is the 1-character string "x"
        # followed by a stray closing quote that pyhocon folds into the
        # same terminator, not "x" followed by a bare `"` that starts a
        # new string. Extending over every extra trailing quote keeps our
        # notion of "end of string" in sync with pyhocon's, so scanning
        # resumes at the same place pyhocon would.
        end = close + 3
        while end < n and text[end] == '"':
            end += 1
        return end, text[content_start:close]
    i = start + 1
    while i < n and text[i] != '"':
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    content = text[start + 1 : i]
    return (i + 1 if i < n else n), content


def _skip_hocon_blanks(text: str, i: int) -> int:
    """Advance past spaces/tabs only (not newlines) from ``i``."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    return i


def _hocon_directive_follows(text: str, i: int) -> bool:
    """True if, starting at ``i``, the text looks like an attempted include
    argument -- a quoted string, or an identifier possibly followed by
    spaces/tabs and then ``(`` -- valid or not.
    """
    n = len(text)
    j = _skip_hocon_blanks(text, i)
    if j < n and text[j] == '"':
        return True
    if j < n and text[j].isalpha():
        k = j
        while k < n and (text[k].isalnum() or text[k] == "_"):
            k += 1
        k = _skip_hocon_blanks(text, k)
        return k < n and text[k] == "("
    return False


def _preceded_by_odd_backslashes(text: str, i: int) -> bool:
    """True if an odd number of consecutive ``\\`` immediately precede
    ``text[i]`` -- i.e. ``text[i]`` is itself escaped (``\\"``: escaped,
    ``\\\\"``: not, the first backslash is what's escaped). Mirrors
    pyhocon's own unquoted-value regex (``(?:[^...]|\\.)+``), which accepts
    a backslash-escaped ``"``, ``#``/``//`` or ``${`` as an ordinary
    character rather than the start of a string/comment/substitution.
    """
    count = 0
    j = i - 1
    while j >= 0 and text[j] == "\\":
        count += 1
        j -= 1
    return count % 2 == 1


def _refuse_hocon_includes(text: str) -> str:
    """Blank the plain HOCON ``include "..."`` directives Puppet ignores,
    and raise :class:`BackendError` for every other include form --
    ``include file(...)``, ``url(...)``, ``classpath(...)``,
    ``required(...)``, ``package(...)``, any other ``name(...)``, a
    directive in value position (including inside a ``[...]`` array), a
    case-mismatched or code-point-mismatched keyword (pyhocon's own
    ``Keyword("include", caseless=True)`` matches by ``.upper()``, which
    also folds a dotless-i ``ınclude`` to ``INCLUDE``), or a bare
    ``include`` with nothing valid after it -- before pyhocon ever parses
    the text. This keeps pyhocon's own include machinery (which reads
    files off the process cwd and fetches ``http(s)``/``file`` URLs) from
    ever running.

    **This is no longer the default (2026-09-29).** It refuses more
    than Puppet's own ``hocon_data`` does (Puppet really does read
    ``file(...)`` and keeps a value-position directive as literal text --
    see :func:`_allow_hocon_includes`), so it is reachable only as the
    opt-in restriction (:class:`HOCONBackend`'s ``hocon_includes=False``).
    See ``AGENTS.md`` for the exact rule.

    A :func:`_install_hocon_include_guard`-installed backstop still applies
    even if this scanner has a gap: pyhocon's own include-resolution
    entry points raise unconditionally for the duration of
    :meth:`HOCONBackend.loads`, so a missed directive fails closed instead
    of silently reading a file or reaching the network.

    Blanked spans replace every non-newline character with a space, so
    line/column numbers in any later pyhocon parse error still line up
    with the original file.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    brackets = []  # stack of open '{'/'[' seen so far
    while i < n:
        c = text[i]
        if c in ('"', "#", "$") and _preceded_by_odd_backslashes(text, i):
            # An escaped quote/hash/substitution-start in unquoted text
            # (``x\"``, ``x\#``, ``x\${``) is an ordinary character to
            # pyhocon, not the start of a string, comment or substitution.
            # (``"``, ``#`` and ``$`` are all excluded from pyhocon's
            # unquoted-value character class, so unescaped they are always
            # significant, unlike a lone ``/`` below.)
            last_sig = c
            i += 1
            continue
        if c == '"':
            end, _content = _find_hocon_string_end(text, i)
            i = end
            last_sig = '"'
            continue
        if c == "#":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # Unlike ``#``, a lone ``/`` is NOT excluded from pyhocon's
            # unquoted-value character class, so ``//`` only starts a
            # comment at a token boundary (start of text, whitespace, or a
            # structural character) -- ``http://h`` and ``x//y`` are
            # ordinary unquoted text, since the value token already
            # in progress simply continues through the slashes and pyhocon
            # never gets a chance to try matching a comment there. An
            # escaped ``\//`` (odd backslashes) is never a comment either.
            # NOTE: ``:`` is deliberately not a boundary character here --
            # unlike every other separator below, a lone ``:`` is NOT
            # excluded from pyhocon's unquoted-value character class, so it
            # can appear literally inside a continuous token (``http://h``)
            # as well as as a key/value separator; treating it as always a
            # boundary would make ``//`` in ``http://h`` a comment again.
            prev = text[i - 1] if i > 0 else None
            at_boundary = prev is None or prev in ' \t\r\n{}[],="'
            if at_boundary and not _preceded_by_odd_backslashes(text, i):
                j = i
                while j < n and text[j] not in "\r\n":
                    j += 1
                i = j
                continue
            last_sig = c
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}", i + 2)
            i = (j + 1) if j != -1 else n
            last_sig = "}"
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\n" or c == "\r":
            # HOCON's own line-ending token is any run of ``\n``/``\r``
            # (pyhocon: ``eol = Word('\n\r')``) -- a lone ``\r`` (no ``\n``)
            # ends a ``#``/``//`` comment and starts a new key-position
            # line exactly as a real newline would.
            last_sig = "\n"
            i += 1
            continue
        if c in "{[":
            brackets.append(c)
            last_sig = c
            i += 1
            continue
        if c in "}]":
            if brackets:
                brackets.pop()
            last_sig = c
            i += 1
            continue
        if c.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.upper() != "INCLUDE":
                last_sig = word[-1]
                i = j
                continue

            # Inside a `[...]` array, every position is a value, never a
            # key -- Puppet keeps a value-position include as literal text,
            # and hyera (which always raises for value position) must not
            # blank it away into an empty/short array instead.
            in_array = bool(brackets) and brackets[-1] == "["
            key_position = (not in_array) and last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + text.count("\r", 0, i) + 1
            # A lone `\r` and a `\n` from the same CRLF pair would both be
            # counted above; CRLF is normalized to a single logical
            # newline everywhere else in this scanner, so undo the double
            # count for every CRLF pair before this position.
            line -= text.count("\r\n", 0, i)

            if key_position and word == "include":
                after = _skip_hocon_blanks(text, j)
                if after < n and text[after] == '"':
                    end, content = _find_hocon_string_end(text, after)
                    scheme = _URL_SCHEME_RE.match(content)
                    if scheme and scheme.group(0)[:-3].lower() != "file":
                        raise BackendError(
                            "HOCON include of a non-file URL is not "
                            "supported (line {})".format(line)
                        )
                    for k in range(i, end):
                        if out[k] not in ("\n", "\r"):
                            out[k] = " "
                    i = end
                    last_sig = " "
                    continue
                raise BackendError(
                    "HOCON include is only supported as a plain quoted "
                    "string; file()/url()/classpath()/required()/package() "
                    "and similar forms are not supported (line {})".format(line)
                )

            if _hocon_directive_follows(text, j):
                raise BackendError(
                    "HOCON include directive is not supported here "
                    "(line {})".format(line)
                )

            last_sig = word[-1]
            i = j
            continue

        last_sig = c
        i += 1

    return "".join(out)


def _allow_hocon_includes(text: str) -> str:
    """Scan HOCON ``include`` directives to match Puppet's own
    ``hocon_data`` (2026-09-29: the default; :func:`_refuse_hocon_includes`
    is the opt-in restriction). Oracle-measured against Ruby hocon 1.4.0
    (Puppet 8.10.0, WSL) for every form:

    - a plain quoted ``include "..."`` (relative, absolute, or a
      ``file://`` URL) always contributes nothing -- Puppet's own bare
      quoted-string form is never a real file/URL read, in either mode;
    - a plain quoted ``include "<scheme>://...">`` for any *other* scheme
      always raises -- Puppet's own ``include_url_without_fallback``/
      ``include_url`` methods are simply missing (measured: a real
      ``http://`` target is never even requested);
    - ``include file(...)`` (relative or absolute, also inside a nested
      object or after another key) is left untouched here, so pyhocon's
      own resolution -- which already reads the file relative to the
      process cwd or absolute, exactly as Ruby hocon does -- runs for
      real;
    - every other key-position directive (``url(...)``, ``classpath(...)``,
      ``required(...)`` whether or not its target exists, ``package(...)``,
      any other ``name(...)``, a space before the paren, or a bare
      ``include`` with nothing valid after it) raises, matching Puppet's
      own parse/method errors for those forms -- Ruby hocon implements
      none of them;
    - an occurrence in value position (including inside a ``[...]``
      array), of *any* spelling/case that looks like a directive, is
      defanged into an ordinary quoted token instead of raising or being
      resolved, so the concatenated result is the exact literal text
      Puppet keeps (measured: ``msg = please include file("x")`` gives
      the literal string ``"please include file(x)"``, quotes and all
      stripped exactly as HOCON string concatenation does for any other
      quoted segment) -- Puppet's ``include`` is a member-level statement
      only, never special in value position, in any spelling;
    - a key-position spelling/case other than the exact lowercase keyword
      ``include`` is never Puppet's own directive either (Ruby's keyword
      match is case-sensitive) and always raises when a directive-shaped
      argument follows -- refusing rather than letting pyhocon's own
      *caseless* keyword matching run a real file/URL read Puppet's own
      hocon_data never would.

    One measured, accepted divergence: Puppet's ``include file("*.conf")``
    never globs (documented "contributes nothing"); pyhocon's own
    resolution *does* glob and includes any match -- kept as a documented
    extension (hyera may do more than Puppet, never less), filed as
    ``hocon-file-include-globs-where-puppet-does-not``.

    A :func:`_install_hocon_include_guard`-installed backstop still
    applies for the forms this mode does not intend to resolve for real
    (``url``/``package`` resolution; see :meth:`HOCONBackend.loads`): a
    scanner gap in one of the forms above still fails closed instead of
    silently reaching the network. ``file(...)`` resolution is
    deliberately NOT guarded here -- letting it run for real is the
    entire point of this mode.

    Blanking and defanging both preserve the original text's length and
    line numbering exactly as :func:`_refuse_hocon_includes` does (see its
    docstring); a defanged value-position word grows by its two quote
    characters in its own first slot, offset by shrinking its remaining
    slots to the empty string, so nothing else in ``out`` shifts.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    brackets = []  # stack of open '{'/'[' seen so far
    while i < n:
        c = text[i]
        if c in ('"', "#", "$") and _preceded_by_odd_backslashes(text, i):
            last_sig = c
            i += 1
            continue
        if c == '"':
            end, _content = _find_hocon_string_end(text, i)
            i = end
            last_sig = '"'
            continue
        if c == "#":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            prev = text[i - 1] if i > 0 else None
            at_boundary = prev is None or prev in ' \t\r\n{}[],="'
            if at_boundary and not _preceded_by_odd_backslashes(text, i):
                j = i
                while j < n and text[j] not in "\r\n":
                    j += 1
                i = j
                continue
            last_sig = c
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}", i + 2)
            i = (j + 1) if j != -1 else n
            last_sig = "}"
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\n" or c == "\r":
            last_sig = "\n"
            i += 1
            continue
        if c in "{[":
            brackets.append(c)
            last_sig = c
            i += 1
            continue
        if c in "}]":
            if brackets:
                brackets.pop()
            last_sig = c
            i += 1
            continue
        if c.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.upper() != "INCLUDE":
                last_sig = word[-1]
                i = j
                continue

            in_array = bool(brackets) and brackets[-1] == "["
            key_position = (not in_array) and last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + text.count("\r", 0, i) + 1
            line -= text.count("\r\n", 0, i)

            if not key_position:
                # Defang rather than raise or resolve: Puppet keeps this
                # as literal text, in any spelling/case, since `include`
                # is never special outside statement position to Ruby.
                # pyhocon disagrees (its own `include_expr` grammar is
                # accepted in value position too, caselessly), so quote
                # the bareword so pyhocon's tokenizer sees an ordinary
                # string instead of a keyword.
                if _hocon_directive_follows(text, j):
                    out[i] = '"' + word + '"'
                    for k in range(i + 1, j):
                        out[k] = ""
                last_sig = word[-1]
                i = j
                continue

            if word != "include":
                # Any spelling/case other than the exact lowercase
                # keyword is never Puppet's own directive (Ruby's match
                # is case-sensitive); refuse outright rather than let
                # pyhocon's caseless grammar run a real read Puppet
                # itself would never attempt. A non-directive
                # continuation (`INCLUDE = 1`) is left alone -- Puppet
                # parses that as an ordinary key.
                if _hocon_directive_follows(text, j):
                    raise BackendError(
                        "HOCON include directive is not supported here "
                        "(line {})".format(line)
                    )
                last_sig = word[-1]
                i = j
                continue

            after = _skip_hocon_blanks(text, j)
            if after < n and text[after] == '"':
                end, content = _find_hocon_string_end(text, after)
                scheme = _URL_SCHEME_RE.match(content)
                if scheme and scheme.group(0)[:-3].lower() != "file":
                    raise BackendError(
                        "HOCON include of a non-file URL is not "
                        "supported (line {})".format(line)
                    )
                # Puppet's own hocon_data never implements the bare
                # quoted-string form as a real file/URL read -- it
                # always contributes nothing, whatever the target.
                for k in range(i, end):
                    if out[k] not in ("\n", "\r"):
                        out[k] = " "
                i = end
                last_sig = " "
                continue

            if text[after : after + 5] == "file(":
                # Puppet's `include file(...)` really does read the file
                # (relative to the process cwd, or absolute) -- leave the
                # text untouched so pyhocon's own resolution (which
                # already does the same cwd-relative/absolute lookup)
                # runs for real.
                last_sig = word[-1]
                i = j
                continue

            raise BackendError(
                "HOCON include is only supported as a plain quoted "
                "string or file(...); url()/classpath()/required()/"
                "package() and similar forms are not supported "
                "(line {})".format(line)
            )

        last_sig = c
        i += 1

    return "".join(out)


#: Set (only for the duration of a `HOCONBackend.loads` call) to the labels
#: (see `_guarded_hocon_classmethod`) that must raise instead of running for
#: that call, so the `_install_hocon_include_guard`-wrapped pyhocon entry
#: points fail closed only for the forms the active mode does not intend to
#: resolve for real -- `"file include"` is a member only when
#: `HOCONBackend.hocon_includes` is False (the opt-in restriction);
#: `"URL include"`/`"package include"` are always members, in either mode,
#: since Puppet's own hocon_data can never resolve those either. Empty
#: outside a `loads()` call. Context-local (per thread/task), so a
#: concurrent `loads()` on another thread and every other pyhocon caller in
#: the process, at any point in time, are unaffected -- only the call(s)
#: that set it see it fire.
_HOCON_INCLUDE_GUARD: "contextvars.ContextVar[frozenset]" = contextvars.ContextVar(
    "_hocon_include_guard", default=frozenset()
)

_HOCON_GUARD_INSTALLED = False


def _guarded_hocon_classmethod(original, label):
    """Wrap a pyhocon include-resolution classmethod's underlying function
    (``original``, still taking ``cls`` first) so it raises
    :class:`BackendError` while ``label`` is a member of
    :data:`_HOCON_INCLUDE_GUARD`'s current value, and behaves exactly as
    pyhocon shipped it otherwise.
    """

    def _guarded(cls, *args, **kwargs):
        if label in _HOCON_INCLUDE_GUARD.get():
            raise BackendError(
                "HOCON include resolution ({}) ran despite the text "
                "scanner having sanitized the input first; refusing to "
                "read a file or fetch a URL".format(label)
            )
        return original(cls, *args, **kwargs)

    return classmethod(_guarded)


def _install_hocon_include_guard(module=None) -> None:
    """Make an include-resolving module's own entry points --
    ``ConfigFactory.parse_file``, ``ConfigFactory.parse_URL`` and
    ``ConfigParser.resolve_package_path``, the three methods its
    ``include`` machinery actually calls to read a file or fetch a URL --
    raise :class:`BackendError` while their label is a member of
    :data:`_HOCON_INCLUDE_GUARD`'s current value, and run exactly as
    pyhocon shipped them at every other time.

    ``module=None`` (the default) targets the **shared** ``pyhocon``
    package (``import pyhocon.config_parser``) -- the only place a caller
    who imports ``pyhocon`` directly (not through :class:`HOCONBackend`)
    reaches its include machinery from. Installed once: eagerly at import
    time if pyhocon is already importable (before any other code -- a
    test fixture included -- gets a chance to monkeypatch these same
    three methods first), and again, idempotently, from
    :meth:`HOCONBackend.loads` for the rarer case where pyhocon only
    becomes importable afterwards.

    A caller may also pass the **private** ``_hocon_parser()`` module
    copy explicitly. This matters: that copy's ``exec_module`` gives it
    its own, distinct ``ConfigFactory``/``ConfigParser`` classes -- not
    the shared module's (verified 2026-09-29:
    ``_hocon_parser().ConfigFactory is not
    pyhocon.config_parser.ConfigFactory``) -- and :meth:`HOCONBackend.loads`
    parses through that private copy, not the shared one. Patching only
    the shared module (as this function used to) left the
    fail-closed backstop below installed but inert for every real
    :class:`HOCONBackend` call: the wrapped shared-module methods were
    simply never on the call path. :func:`_hocon_parser` now calls this
    a second time, with its own ``mod``, to close that gap -- pre-existing
    (since `json_hocon_loaders` added the private copy), found and fixed
    while hardening the more permissive default, which makes the
    backstop's guarantee matter more, not less.

    The wrapping itself is a permanent, process-wide monkeypatch on
    whichever module is targeted -- there is no per-call hook to attach to
    instead -- but the raising it adds is gated by a
    :class:`contextvars.ContextVar`, which is context-local (per
    thread/task): only the ``loads()`` call that set the guard ever sees it
    fire, and every other pyhocon caller in the same process, at any point
    before, during or after that call, keeps pyhocon's normal behavior.

    This is a fail-closed backstop for :func:`_allow_hocon_includes`/
    :func:`_refuse_hocon_includes`: if that text scanner ever has a gap
    (misses a directive form pyhocon's own grammar accepts), the include
    still cannot read a file or reach the network for a form the active
    mode does not intend to resolve for real -- it raises instead.
    """
    global _HOCON_GUARD_INSTALLED
    if module is None:
        if _HOCON_GUARD_INSTALLED:
            return
        from pyhocon.config_parser import ConfigFactory, ConfigParser
    else:
        if getattr(module, "_hocon_include_guard_installed", False):
            return
        ConfigFactory, ConfigParser = module.ConfigFactory, module.ConfigParser

    for cls, attr, label in (
        (ConfigFactory, "parse_file", "file include"),
        (ConfigFactory, "parse_URL", "URL include"),
        (ConfigParser, "resolve_package_path", "package include"),
    ):
        current = cls.__dict__.get(attr)
        # Ordinarily a `classmethod` object (pyhocon's own definition);
        # tolerate anything else (e.g. a test's own monkeypatch installed
        # ahead of this call) by wrapping it as-is instead of unwrapping a
        # `.__func__` that may not exist, so installation never crashes on
        # already-patched state -- it just guards whatever is there.
        original = current.__func__ if hasattr(current, "__func__") else current
        setattr(cls, attr, _guarded_hocon_classmethod(original, label))

    if module is None:
        _HOCON_GUARD_INSTALLED = True
    else:
        module._hocon_include_guard_installed = True
    _HOCON_GUARD_INSTALLED = True


# Installed eagerly, at import time, if pyhocon is already importable -- so
# the guard is in place before any test fixture (or other code) gets a
# chance to monkeypatch these same three methods for its own purposes.
# Harmless no-op if pyhocon is missing or broken: HOCONBackend.check_available
# (and loads) cover that case, and loads() also calls this (idempotent) for
# the rarer case where pyhocon becomes importable only after this module was
# first imported.
try:
    _install_hocon_include_guard()
except Exception:  # pragma: no cover - optional dependency, best-effort
    pass


def _as_plain(obj):
    """Recursively convert pyhocon's ``ConfigTree``/``ConfigList`` (both
    ``dict``/``list`` subclasses) into plain ``dict``/``list``."""
    if isinstance(obj, dict):
        return {k: _as_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_as_plain(v) for v in obj]
    return obj


_HOCON_PARSER_LOCK = threading.Lock()
_HOCON_PARSER_MODULE = None


class _HoconFileCodecsShim:
    """Drop-in for the module-global ``codecs`` name our private
    ``config_parser`` copy resolves ``codecs.open(filename, 'r',
    encoding=encoding)`` against (``ConfigFactory.parse_file``, the one
    call the default mode lets run for real -- see :func:`_allow_hocon_includes`).

    ``codecs.open`` is deprecated since Python 3.13 and raises on 3.14+
    (``DeprecationWarning: codecs.open() is deprecated. Use open()
    instead.``); this project's ``filterwarnings = ["error"]`` turns that
    into a fatal exception the moment a real ``include file(...)``
    resolves. Builtin ``open`` in text mode with the same ``encoding``
    reads identically for pyhocon's own use here (a whole-file text read,
    no codec-specific behavior pyhocon relies on).
    """

    @staticmethod
    def open(filename, mode="r", encoding=None, **kwargs):
        return open(filename, mode, encoding=encoding, **kwargs)


class _HoconLoggerShim:
    """Drop-in for the module-global ``logger`` name our private
    ``config_parser`` copy calls ``.warn(...)`` through -- two call sites
    inside ``ConfigFactory.parse_file``/``parse_URL``, on the non-required,
    unreachable-target branch a real ``include file(...)``/``include
    url(...)`` (now reachable by default) can now actually reach.

    ``Logger.warn`` is deprecated (use ``.warning``) and raises under this
    project's ``filterwarnings = ["error"]`` the moment either branch
    runs. Delegates everything else to the real logger unchanged -- the
    real logger is shared (``logging.getLogger(__name__)`` inside our
    private copy resolves to the SAME registry entry as the shared
    module's, since both share the module name ``pyhocon.config_parser``),
    so this only intercepts the one deprecated spelling.
    """

    def __init__(self, real):
        self._real = real

    def warn(self, *args, **kwargs):
        self._real.warning(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _hocon_parser():
    """A private copy of the ``pyhocon.config_parser`` module, with its
    ``get_period_expr`` grammar replaced by one that never matches, so a
    HOCON duration (``10s``, ``5 minutes``) stays literal text -- matching
    real Ruby hocon 1.4.0, which has no duration type at all -- instead of
    becoming a ``datetime.timedelta`` (which crashes ``-o yaml``); and its
    ``codecs`` name replaced by :class:`_HoconFileCodecsShim` so a real
    ``include file(...)`` resolution (now the default) never hits the
    ``codecs.open`` deprecation above.

    Built once, under a lock, and cached. The *shared* ``pyhocon`` module
    (``import pyhocon; pyhocon.ConfigFactory...``) is never touched --
    every other caller of pyhocon in the process keeps the real duration
    behavior and the real ``codecs`` module. ``get_period_expr`` has
    existed since pyhocon 0.3.60; the ``hocon`` extra's floor is pinned
    there (or higher) for exactly this.
    """
    global _HOCON_PARSER_MODULE
    if _HOCON_PARSER_MODULE is not None:
        return _HOCON_PARSER_MODULE
    with _HOCON_PARSER_LOCK:
        if _HOCON_PARSER_MODULE is None:
            import pyparsing

            spec = importlib.util.find_spec("pyhocon.config_parser")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "get_period_expr"):
                raise BackendError(
                    "hocon_data requires pyhocon>=0.3.60 (get_period_expr, "
                    "used to keep durations as text): "
                    'pip install "hyera[hocon]"'
                )
            mod.get_period_expr = lambda: pyparsing.NoMatch()
            mod.codecs = _HoconFileCodecsShim()
            mod.logger = _HoconLoggerShim(mod.logger)
            _install_hocon_include_guard(mod)
            _HOCON_PARSER_MODULE = mod
    return _HOCON_PARSER_MODULE


_HOCON_KIND_NAMES = {"ConfigList": "LIST"}


def _hocon_root_kind(value) -> str:
    return _HOCON_KIND_NAMES.get(type(value).__name__, type(value).__name__.upper())


class HOCONBackend(Backend):
    """HOCON (``.conf``) data via the optional ``pyhocon`` package.

    Always registered: a missing/broken ``pyhocon`` fails at
    :meth:`check_available` (backend/level construction) and again in
    :meth:`loads`, both naming the ``hyera[hocon]`` extra -- so the failure
    is always reachable instead of silently disappearing from
    :func:`default_backends`.

    ``include`` directives resolve exactly as Puppet's own ``hocon_data``
    does by default (2026-09-29 -- see :func:`_allow_hocon_includes`
    for the oracle-measured rule): a plain quoted include contributes
    nothing, ``include file(...)`` really reads the file (relative to the
    process cwd, or absolute), and every other form (``url(...)``,
    ``classpath(...)``, ``required(...)``, ``package(...)``, a
    case-mismatched keyword, a bare ``include`` with nothing valid after
    it) raises, matching Puppet's own parse/method errors for those forms.
    A value-position directive (including inside a ``[...]`` array) is
    kept as literal text, as Puppet keeps it.

    Passing ``hocon_includes=False`` (to the constructor directly, or via
    a ``hocon_includes: false`` key on the hierarchy entry/``defaults`` --
    hyera's own extension, not Puppet vocabulary) restores the
    pre-fidelity refusal instead (see :func:`_refuse_hocon_includes`): every
    directive form other than a plain quoted include raises, including
    ``file(...)``.

    In either mode, pyhocon's own include entry points are also wrapped
    (see :func:`_install_hocon_include_guard`) as a fail-closed backstop
    for the forms that mode does not intend to resolve for real, so a gap
    in the text scanner still fails closed instead of silently reading a
    file or reaching the network. Durations are parsed via a private
    module copy (see :func:`_hocon_parser`) so they stay text.
    """

    NAMES: _ty.ClassVar[_Names] = {"function": ("hocon_data",), "format": ("hocon",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".conf",)

    _MISSING_DEP_MESSAGE = (
        "hocon_data requires the optional 'pyhocon' package: "
        'pip install "hyera[hocon]"'
    )

    def __init__(
        self,
        conf: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        *,
        strict: _ty.Optional[str] = None,
        hocon_includes: _ty.Optional[bool] = None,
    ) -> None:
        super().__init__(conf, strict=strict)
        # No `Hiera(backend_options=...)` plumbing exists
        # yet, so the opt-in reads from the level's own `conf` (its
        # hiera.yaml hierarchy-entry/`defaults` mapping) when not passed
        # directly.
        if hocon_includes is None:
            hocon_includes = self.conf.get("hocon_includes", True)
        self.hocon_includes: bool = bool(hocon_includes)

    @classmethod
    def check_available(cls) -> None:
        if not has_hocon():
            raise BackendError(cls._MISSING_DEP_MESSAGE)

    def loads(self, text: str) -> _ty.Any:
        """Parse HOCON the way Puppet's ``hocon_data`` does: ``include
        file(...)`` really reads the file, ``include url(...)``/
        ``classpath(...)``/``required(...)`` and durations raise/stay text
        (see the class docstring for the full fidelity rule)."""
        try:
            from pyhocon import ConfigTree
        except ImportError:
            raise BackendError(self._MISSING_DEP_MESSAGE) from None
        except Exception as e:
            raise BackendError(
                "hocon_data backend could not import 'pyhocon' ({}: {}); "
                'pip install "hyera[hocon]"'.format(type(e).__name__, e)
            ) from None
        _install_hocon_include_guard()
        if self.hocon_includes:
            text = _allow_hocon_includes(text)
            # `file(...)` is deliberately NOT guarded here -- letting it
            # run for real (matching Puppet) is the entire point of this
            # mode; `url`/`package` resolution stays backstopped since
            # Puppet's own hocon_data can never resolve those either.
            guarded = frozenset({"URL include", "package include"})
        else:
            text = _refuse_hocon_includes(text)
            guarded = frozenset({"file include", "URL include", "package include"})
        token = _HOCON_INCLUDE_GUARD.set(guarded)
        try:
            mod = _hocon_parser()
            parsed = mod.ConfigFactory.parse_string(text)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(_one_line(str(e))) from None
        finally:
            _HOCON_INCLUDE_GUARD.reset(token)
        if not isinstance(parsed, ConfigTree):
            raise BackendError(
                "hocon_data: has type {} rather than object at file "
                "root".format(_hocon_root_kind(parsed))
            )
        return _as_plain(parsed)


class DotenvBackend(Backend):
    """dotenv, in exactly the shape the ``sops`` CLI's writer emits it
    (``stores/dotenv/store.go``) -- reachable only through
    :class:`SopsBackend`.
    """

    NAMES: _ty.ClassVar[_Names] = {"format": ("dotenv",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".env",)

    def loads(self, text: str) -> _ty.Dict[str, str]:
        """Parse dotenv the way sops's own writer emits it: ``KEY=value``
        lines, ``#`` comments, blank lines skipped, ``\\n`` unescaped."""
        result: _ty.Dict[str, str] = {}
        for lineno, line in enumerate(text.split("\n"), start=1):
            if line == "" or line.startswith("#"):
                continue
            if "=" not in line:
                raise BackendError("invalid dotenv line {}".format(lineno))
            key, _sep, value = line.partition("=")
            result[key] = value.replace("\\n", "\n")
        return result


def _refuse_batch_shim(exe: str) -> None:
    """Raise if *exe* is a ``.bat``/``.cmd`` shim, in any letter case."""
    if os.path.splitext(exe)[1].lower() in (".bat", ".cmd"):
        raise BackendError(
            "refusing to run sops batch shim {}: cmd.exe re-parses its own "
            "argument line, which is unsafe for a data-derived path".format(exe)
        )


def _run_sops(path, input_type: str, output_type: str = None) -> bytes:
    """Run ``sops -d`` on ``path`` and return its decrypted stdout.

    Hardened for unattended use: the resolved executable is run by its
    absolute path (never a bare name re-resolved by the child), a batch
    shim (``.bat``/``.cmd``) is refused outright (``cmd.exe`` re-parses its
    own argument line, which a data path containing shell metacharacters
    could abuse), and the data path is always passed absolute and after a
    literal ``--`` so a path/scope value starting with ``-`` can never be
    read as a sops option.

    ``output_type`` defaults to ``input_type`` (the rule for
    yaml/json/dotenv, keeping YAML on the Psych-compatible loader).
    :class:`SopsBackend` passes a different value only for ``ini``:
    sops's own INI *writer* is ambiguous, so INI is always decrypted as
    ``--output-type=json`` and parsed as JSON instead.
    """
    if output_type is None:
        output_type = input_type
    exe = shutil.which("sops")
    if exe is None:
        raise BackendError(
            "sops executable not found on PATH; cannot decrypt {}".format(path)
        )
    # The batch-shim refusal runs before the relative-path check (it holds
    # whatever the path looks like, and a Windows-style path is never
    # absolute on POSIX) and again after abspath, which normalizes forms
    # such as ``sops.bat.`` into ``.bat``.
    _refuse_batch_shim(exe)
    if not (os.path.isabs(exe) or exe.startswith(("/", "\\"))):
        # Python's ``shutil.which`` does not consistently honour the
        # Windows implicit-current-directory opt-out (NoDefaultCurrentDirectoryInExePath):
        # on 3.9 it can still return a path relative to the cwd (or a
        # relative PATH entry) even when the caller has opted out of that
        # behavior. Running whatever that happens to resolve to would be
        # exactly the implicit-cwd exposure the absolute-path handling here
        # is meant to close, so refuse it outright instead of silently
        # trusting a relative result. A path that is merely drive-less but
        # still rooted (``/usr/bin/sops``, e.g. a POSIX-style test double)
        # is not this exposure -- Windows resolves it against the current
        # drive's root, never against an attacker-influenced cwd -- so only
        # a path with no leading separator at all (genuinely relative) is
        # refused here.
        raise BackendError(
            "refusing to run sops resolved to a relative path {!r} (from "
            "the current directory or a relative PATH entry); put an "
            "absolute sops on PATH instead".format(exe)
        )
    exe = os.path.abspath(exe)
    _refuse_batch_shim(exe)
    abs_path = os.path.abspath(os.fspath(path))
    timed_out = False
    try:
        proc = subprocess.run(
            [
                exe,
                "--input-type={}".format(input_type),
                "--output-type={}".format(output_type),
                "-d",
                "--",
                abs_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SOPS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Recorded, not re-raised, inside the except (R4b): a
        # TimeoutExpired carries the subprocess's partial stdout (possibly
        # partially-decrypted plaintext) as an attribute, and raising
        # *inside* an active except block sets it as `__context__` even
        # under `from None` -- `from None` only sets
        # `__suppress_context__`, so the attribute stays reachable via
        # `__context__.stdout`/`.output` on the raised BackendError. The
        # BackendError is raised below instead, once this except block has
        # finished and Python has cleared the handled exception, so
        # `__context__` is genuinely `None`, not just suppressed.
        timed_out = True
    except OSError as e:
        raise BackendError("Failed to run sops on {}: {}".format(path, e)) from e

    if timed_out:
        raise BackendError(
            "sops timed out after {}s decrypting {}".format(SOPS_TIMEOUT, path)
        ) from None

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise BackendError(
            "sops failed (exit {}) decrypting {}: {}".format(
                proc.returncode, path, detail or "<no stderr>"
            )
        )
    return proc.stdout


#: sops's own ``FormatForPath`` rule (``cmd/sops/formats/formats.go``,
#: verified against the real v3.13.3 binary and source, 2026-09-29):
#: case-sensitive ``strings.HasSuffix``, checked in this order. Anything
#: else is binary to sops -- not a data hash.
_SOPS_SUFFIXES = (
    (".yaml", "yaml"),
    (".yml", "yaml"),
    (".json", "json"),
    (".env", "dotenv"),
    (".ini", "ini"),
)


def _sops_format(path_str: str):
    for suffix, fmt in _SOPS_SUFFIXES:
        if path_str.endswith(suffix):
            return fmt
    return None


#: S7: fixed prefixes of the two ``_yaml_loader`` messages that quote the
#: offending scalar verbatim (``invalid value for Float()/Integer():
#: "<data>"``). Matched as a plain prefix, never against the tail: the
#: quoted scalar can itself embed a literal newline (a ``!!float |\n
#: HUNTER2`` block scalar), which would otherwise make a naive
#: ``.*$``-style regex fail to match the whole message and leak it.
_SOPS_YAML_QUOTED_PREFIXES = (
    "invalid value for Float(): ",
    "invalid value for Integer(): ",
)

#: The third leaking shape's fixed prefix, ``Tried to load unspecified
#: class: <name>`` (``_yaml_loader._disallowed``); ``<name>`` is
#: attacker-controlled text for every ``!ruby/...`` tag except the fixed
#: names below.
_SOPS_YAML_CLASS_PREFIX = "Tried to load unspecified class: "

#: Names ``_yaml_loader`` itself raises unconditionally for a known YAML
#: shape (an implicit timestamp/date, `!!set`, a bare/nameless
#: ``!ruby/object``) -- never text lifted from the decrypted document, so
#: these stay visible. Everything else after "unspecified class: " comes
#: from the tag's own (attacker-controlled) suffix text.
_SOPS_YAML_CLASS_ALLOW = frozenset({"Time", "Date", "Object", "Psych::Set"})


def _sops_redact_yaml_problem(problem: str) -> str:
    """On the sops decrypt path only, blank a YAML loader message's
    quoted scalar or attacker-suppliable class name. Plain ``yaml_data``
    (no sops involved) keeps Puppet's full text -- :meth:`Backend.load`
    never calls this. A decrypted value shaped like ``!!float HUNTER2`` or
    ``!ruby/object:HUNTER2 {}`` would otherwise echo ``HUNTER2`` verbatim
    into the raised error, the log, and an MCP result (the CLI's optional
    MCP server serves this same error text over ``tools/call``).
    """
    for prefix in _SOPS_YAML_QUOTED_PREFIXES:
        if problem.startswith(prefix):
            return prefix + "<redacted>"
    if problem.startswith(_SOPS_YAML_CLASS_PREFIX):
        name = problem[len(_SOPS_YAML_CLASS_PREFIX) :]
        if name not in _SOPS_YAML_CLASS_ALLOW:
            return _SOPS_YAML_CLASS_PREFIX + "<redacted>"
    return problem


class SopsBackend(Backend):
    """Decrypted on the fly via the ``sops`` CLI (``sops_data``; the
    one kept non-Puppet deviation).

    Hardened for unattended use: the subprocess has a finite timeout, its
    stderr is captured and surfaced, a missing ``sops`` binary raises a
    clear :class:`BackendError` instead of an opaque ``FileNotFoundError``,
    and a decrypted file that fails to parse reports only the problem and
    its line/column -- never the decrypted plaintext.

    ``sops_data`` and ``sops`` infer the format from the file extension
    with sops's own rule (:data:`_SOPS_SUFFIXES`); the ``sops_<format>``
    name (a :class:`NamePattern`, added in a later commit alongside the
    ``sops`` alias) forces one regardless of extension via the ``format``
    constructor keyword.
    """

    NAMES: _ty.ClassVar[_Names] = {
        "function": (
            "sops_data",
            "sops",
            NamePattern(
                "sops_<yaml|json|ini|dotenv>",
                re.compile(r"sops_(?P<format>yaml|json|ini|dotenv)"),
            ),
        )
    }

    def __init__(
        self,
        conf: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        *,
        strict: _ty.Optional[str] = None,
        format: _ty.Optional[str] = None,
    ) -> None:
        super().__init__(conf, strict=strict)
        self._format = format

    def data_hash(
        self, path: "Path", options: _ty.Mapping[str, _ty.Any]
    ) -> _ty.Dict[str, _ty.Any]:
        """Decrypt ``path`` with the ``sops`` CLI and parse the plaintext
        in the format sops itself reports for it (or the ``format``
        constructor keyword, when given)."""
        self._require_path_only(path, options)
        fmt = self._format or _sops_format(str(path))
        if fmt is None:
            raise ConfigError(
                "sops_data: '{}' has no .yaml/.yml/.json/.env/.ini suffix, "
                "so sops reads it as binary, which is not a data hash; use "
                "data_hash: sops_<yaml|json|ini|dotenv> to choose a format"
                "".format(path)
            )
        # sops's own INI *writer* is ambiguous -- a decrypted value
        # containing `"""` plus a newline can inject a key or replace a
        # whole other section, and no INI parser (including hyera's former
        # one) can tell those bytes apart from a genuine file. INI is
        # therefore always decrypted as sops's own JSON view instead
        # (confirmed against real sops 3.13.3: it is exactly
        # `{"DEFAULT": {...}, section: {...}}`) and parsed with
        # JSONBackend; yaml/json/dotenv keep output-type equal to
        # input-type.
        parse_fmt = "json" if fmt == "ini" else fmt
        raw = _run_sops(path, fmt, output_type=parse_fmt)
        format_backend = Backend.new(parse_fmt, kind="format", strict=self.strict)
        problem = None
        text = None
        try:
            text = raw.decode("utf-8")
            parsed = format_backend.loads(text)
        except UnicodeDecodeError as e:
            # Optional hardening: the byte offset only, never the
            # offending byte value or the surrounding text the stock
            # codec message quotes.
            problem = "invalid UTF-8 at byte offset {}".format(e.start)
        except BackendError as e:
            problem = _sops_redact_yaml_problem(str(e))
        else:
            return format_backend._as_data_hash(parsed, path)
        finally:
            # Optional hardening: drop the plaintext locals before the
            # raise below, so a frame-capturing error reporter (e.g.
            # Sentry's default) does not also collect them.
            del raw, text
        raise BackendError(
            "Unable to parse ({}): {}".format(path, problem), path=str(path)
        )


class EyamlBackend(Backend):
    """Puppet's hiera-eyaml ``lookup_key`` function, PKCS7 only (behind the
    optional ``hyera[eyaml]`` extra). Ports ``functions/eyaml_lookup_key.
    rb:25-79``: the raw ``.eyaml`` file loads once per location (through
    :meth:`~hyera._function_provider.LookupContext.cached_file_data`, its
    *raw* parse only -- caching the non-Hash rule's strict-sensitive result
    would freeze whichever strictness read it first, exactly the trap
    ``Hiera._load_file`` guards against for ``data_hash``), then each
    requested key's value is decrypted (:func:`hyera._eyaml.decrypt_string`)
    and cached; the raw hash is never returned to the engine, and its
    values are never interpolated except through
    :func:`~hyera._eyaml.decrypt_string`'s own trailing ``context.
    interpolate`` call.
    """

    NAMES: _ty.ClassVar[_Names] = {"function": ("eyaml_lookup_key",)}

    @classmethod
    def check_available(cls) -> None:
        from ._eyaml import check_cryptography

        check_cryptography()

    def lookup_key(
        self,
        key: str,
        options: _ty.Mapping[str, _ty.Any],
        context: LookupContext,
    ) -> _ty.Any:
        """Decrypt ``key``'s PKCS7 ``ENC[...]`` value from the ``.eyaml``
        file named by the hierarchy location, matching Puppet's
        ``eyaml_lookup_key``."""
        if context.cache_has_key(key):
            return context.cached_value(key)
        if "path" not in options:
            raise ConfigError(
                "'eyaml_lookup_key': one of 'path', 'paths' 'glob', 'globs' "
                "or 'mapped_paths' must be declared in hiera.yaml when "
                "using this lookup_key function"
            )
        path = options["path"]
        if context.cache_has_key(None):
            parsed = context.cached_value(None)
        else:
            yaml_backend = YAMLBackend()
            parsed = context.cached_file_data(path, parse=yaml_backend.loads)
            context.cache(None, parsed)

        # The non-Hash rule reads `self.strict` at call time, same as
        # `yaml_data`'s own -- applied fresh on every read (never cached),
        # so a later call under different strictness sees its own rule.
        raw = YAMLBackend(strict=self.strict)._as_data_hash(parsed, path)
        if key not in raw:
            context.not_found()
        value = raw[key]
        decrypted = self._decrypt(value, options, context, key, path)
        return context.cache(key, decrypted)

    def _decrypt(self, value, options, context, key, path):
        """Recurse into ``value`` decrypting every string
        (``eyaml_lookup_key.rb:66-79``): a Hash's keys are interpolated but
        never decrypted, a List/Hash's elements/values recurse, and every
        other type (int/float/bool/None) passes through unchanged. Only the
        decrypted string leaves through ``context.interpolate`` -- a value
        with no ``ENC[...]`` token is interpolated too (mirroring Puppet's
        own unconditional call)."""
        from ._eyaml import decrypt_string

        if isinstance(value, str):
            return context.interpolate(decrypt_string(value, options, key, path))
        if isinstance(value, dict):
            return {
                context.interpolate(k): self._decrypt(v, options, context, key, path)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._decrypt(v, options, context, key, path) for v in value]
        return value


def default_backends() -> "_ty.List[_ty.Type[Backend]]":
    """The distinct backend classes registered in the ``function``
    namespace, in definition order (YAML, JSON, HOCON, sops)."""
    registry = Backend._REGISTRY.get("function", {"exact": {}, "patterns": []})
    seen = []
    for cls in registry["exact"].values():
        if cls not in seen:
            seen.append(cls)
    for _pattern, cls in registry["patterns"]:
        if cls not in seen:
            seen.append(cls)
    return seen


def has_hocon() -> bool:
    """True iff the optional ``pyhocon`` dependency imports without error.

    Any import-time exception (not just ``ImportError`` -- an installed but
    broken ``pyhocon`` can raise something else entirely, e.g.
    ``AttributeError`` against a too-new stdlib) is caught and logged at
    debug, so a broken optional dependency never breaks every ``Hiera()``.
    """
    try:
        import pyhocon  # noqa: F401

        return True
    except Exception as e:
        _LOGGER.debug("pyhocon is not usable: %s: %s", type(e).__name__, e)
        return False
