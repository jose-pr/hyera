"""Data backends: a self-registering ``Backend`` registry.

Every format or provider is a :class:`Backend` subclass. Registration is by
subclassing (``NAMES``, keyed by namespace) rather than an explicit call;
lookup goes through :meth:`Backend.find`/:meth:`Backend.get`/:meth:`Backend.new`.
"""

import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
from typing import NamedTuple

import yaml

from .exceptions import BackendError, _one_line
from ._yaml_loader import RubySymbol, safe_load, symkeys_to_string

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "Backend",
    "NamePattern",
    "YAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "SopsBackend",
    "BackendError",
    "RubySymbol",
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

    Returns ``"warning"`` until ``interpolation_engine/
    single_pass_engine`` points this at the ``_invocation`` ContextVar set
    from ``Scope.strict`` (program Design Q4). There is no ``Hiera(strict=)``:
    a level's backend is shared across scopes, so it never stores a scope's
    strictness itself.
    """
    return "warning"


class NamePattern(NamedTuple):
    """A registered name that matches by regex instead of exact string.

    ``regex`` is matched with :meth:`re.Pattern.fullmatch`; its named groups
    are passed as keyword arguments to the backend's constructor.
    """

    display: str
    regex: "re.Pattern[str]"


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

    KINDS = ("function", "v3", "format", "render")

    #: ``{kind: (name | NamePattern, ...)}``. Read only from the defining
    #: class's own ``__dict__`` at subclass time, so a subclass never
    #: re-registers its parent's names.
    NAMES: "dict" = {}

    #: File extensions (with leading dot) this backend's format answers to,
    #: longest-suffix-match, used by :meth:`for_path`.
    EXTENSIONS: "tuple" = ()

    _REGISTRY = {kind: {"exact": {}, "patterns": []} for kind in KINDS}

    def __init__(self, conf: dict = None, *, strict: str = None):
        self.conf = conf or {}
        # Accept either ``datadir`` or Hiera-5's ``data_dir`` spelling; the
        # loader normalizes to one of these. Missing/None -> "" (relative).
        # (`config_loading_and_validation` removes this fallback later.)
        datadir = self.conf.get("datadir")
        if datadir is None:
            datadir = self.conf.get("data_dir")
        self.datadir: str = datadir or ""
        if strict is not None and strict not in _STRICT_VALUES:
            raise ValueError(
                "strict must be one of {!r}, not {!r}".format(_STRICT_VALUES, strict)
            )
        self._strict = strict
        self.name = type(self)._default_name()

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

    def __init_subclass__(cls, **kwargs):
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
    def find(cls, name, kind="function"):
        """The registered class for ``name`` in ``kind``, or ``None``."""
        found, _captures = cls._match(name, kind)
        return found

    @classmethod
    def get(cls, name, kind="function"):
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
    def new(cls, name, conf=None, *, kind="function", strict=None):
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
    def names(cls, kind="function"):
        """Registered names in ``kind``: exact names in registration order,
        then patterns by their :attr:`NamePattern.display`."""
        registry = cls._REGISTRY.get(kind, {"exact": {}, "patterns": []})
        return list(registry["exact"].keys()) + [
            pattern.display for pattern, _klass in registry["patterns"]
        ]

    @classmethod
    def for_path(cls, path):
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
    def implements(cls, op):
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
    def check_available(cls):
        """Raise :class:`BackendError` if this backend cannot be used (e.g.
        a missing optional dependency). A no-op by default."""

    # -- serialization (json-module shaped) --------------------------------

    def loads(self, text):
        """Parse ``text`` (a ``str``). Raises path-free problem text."""
        raise NotImplementedError(
            "{} does not implement .loads()".format(type(self).__name__)
        )

    def dumps(self, obj, **kw):
        raise NotImplementedError(
            "{} does not implement .dumps()".format(type(self).__name__)
        )

    def load(self, source):
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
                data = (
                    reader()
                    if reader is not None
                    else open(os.fspath(source), "rb").read()
                )
                text = data.decode("utf-8")
            return self.loads(text)
        except UnicodeDecodeError as e:
            problem = str(e)
        except BackendError as e:
            problem = str(e)
        raise BackendError(
            "Unable to parse ({}): {}".format(path, problem), path=str(path)
        )

    def dump(self, obj, fp, **kw):
        fp.write(self.dumps(obj, **kw))

    # -- Hiera 5 provider hooks ---------------------------------------------

    def data_hash(self, path, options):
        """The ``data_hash`` provider hook: parse the whole file at
        ``path`` and adapt it into hiera data (see :meth:`_as_data_hash`)."""
        return self._as_data_hash(self.load(path), path)

    def _as_data_hash(self, parsed, path):
        """Adapt a parsed document into hiera data. The base class is the
        identity; :class:`YAMLBackend` overrides it for ``yaml_data``'s
        non-Hash rule (``yaml_data.rb:27-35``)."""
        return parsed

    def lookup_key(self, key, options, context):
        raise NotImplementedError(
            "{} does not implement .lookup_key()".format(type(self).__name__)
        )

    def data_dig(self, key_segments, options, context):
        raise NotImplementedError(
            "{} does not implement .data_dig()".format(type(self).__name__)
        )


class YAMLBackend(Backend):
    NAMES = {"function": ("yaml_data",), "format": ("yaml",), "render": ("yaml",)}
    EXTENSIONS = (".yaml", ".yml")

    def loads(self, text):
        # Psych's rules (types, BOM, one-document, symbol keys/values),
        # ported in ``_yaml_loader``: numbers/booleans/dates/symbols per
        # Ruby's ScalarScanner, not PyYAML's own Python-flavored resolver.
        return safe_load(text)

    def dumps(self, obj, **kw):
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


class JSONBackend(Backend):
    NAMES = {"function": ("json_data",), "format": ("json",), "render": ("json",)}
    EXTENSIONS = (".json",)

    def loads(self, text):
        problem = None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            problem = "{} at line {} column {}".format(e.msg, e.lineno, e.colno)
        # Outside the except block, matching YAMLBackend's chain-free style.
        raise BackendError(problem)

    def dumps(self, obj, **kw):
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


def _strip_hocon_includes(text: str) -> str:
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
    ever running. See ``AGENTS.md`` for the exact rule and its two
    deliberate divergences from Puppet (``file()``, value position).

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
            # and pyera (which always raises for value position) must not
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


#: Set (only for the duration of a `HOCONBackend.loads` call) so the
#: `_install_hocon_include_guard`-wrapped pyhocon entry points raise instead
#: of running. Context-local (per thread/task), so a concurrent `loads()` on
#: another thread and every other pyhocon caller in the process, at any
#: point in time, are unaffected -- only the call(s) that set it see it fire.
_HOCON_INCLUDE_GUARD: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "_hocon_include_guard", default=False
)

_HOCON_GUARD_INSTALLED = False


def _guarded_hocon_classmethod(original, label):
    """Wrap a pyhocon include-resolution classmethod's underlying function
    (``original``, still taking ``cls`` first) so it raises
    :class:`BackendError` while :data:`_HOCON_INCLUDE_GUARD` is set, and
    behaves exactly as pyhocon shipped it otherwise.
    """

    def _guarded(cls, *args, **kwargs):
        if _HOCON_INCLUDE_GUARD.get():
            raise BackendError(
                "HOCON include resolution ({}) ran despite the text "
                "scanner having sanitized the input first; refusing to "
                "read a file or fetch a URL".format(label)
            )
        return original(cls, *args, **kwargs)

    return classmethod(_guarded)


def _install_hocon_include_guard() -> None:
    """Make pyhocon's own include-resolution entry points --
    ``ConfigFactory.parse_file``, ``ConfigFactory.parse_URL`` and
    ``ConfigParser.resolve_package_path``, the three methods its
    ``include`` machinery actually calls to read a file or fetch a URL --
    raise :class:`BackendError` for the duration of a
    :meth:`HOCONBackend.loads` call, and run exactly as pyhocon shipped them
    at every other time.

    Installed once: eagerly at import time if pyhocon is already
    importable (before any other code -- a test fixture included -- gets a
    chance to monkeypatch these same three methods first), and again,
    idempotently, from :meth:`HOCONBackend.loads` for the rarer case where
    pyhocon only becomes importable afterwards. The wrapping itself is a
    permanent, process-wide monkeypatch -- there is no per-call hook to
    attach to instead -- but the raising it adds is gated by a
    :class:`contextvars.ContextVar`, which is context-local (per
    thread/task): only the ``loads()`` call that set the guard ever sees it
    fire, and every other pyhocon caller in the same process, at any point
    before, during or after that call, keeps pyhocon's normal behavior.

    This is a fail-closed backstop for :func:`_strip_hocon_includes`: if
    that text scanner ever has a gap (misses a directive form pyhocon's own
    grammar accepts), the include still cannot read a file or reach the
    network -- it raises instead.
    """
    global _HOCON_GUARD_INSTALLED
    if _HOCON_GUARD_INSTALLED:
        return
    from pyhocon.config_parser import ConfigFactory, ConfigParser

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


class HOCONBackend(Backend):
    """HOCON (``.conf``) data via the optional ``pyhocon`` package.

    Always registered (Design Q5): a missing/broken ``pyhocon`` fails at
    :meth:`check_available` (backend/level construction) and again in
    :meth:`loads`, both naming the ``pyera[hocon]`` extra -- so the failure
    is always reachable instead of silently disappearing from
    :func:`default_backends`.

    ``include`` directives are sanitized before pyhocon ever sees the text
    (see :func:`_strip_hocon_includes`): pyhocon's own include machinery
    (file reads relative to the process cwd, ``http(s)``/``file`` URL
    fetches) never runs. As a fail-closed backstop, pyhocon's own include
    entry points are also wrapped (see :func:`_install_hocon_include_guard`)
    to raise if the scanner ever has a gap.
    """

    NAMES = {"function": ("hocon_data",), "format": ("hocon",)}
    EXTENSIONS = (".conf",)

    _MISSING_DEP_MESSAGE = (
        "hocon_data requires the optional 'pyhocon' package: "
        'pip install "pyera[hocon]"'
    )

    @classmethod
    def check_available(cls):
        if not has_hocon():
            raise BackendError(cls._MISSING_DEP_MESSAGE)

    def loads(self, text):
        try:
            from pyhocon import ConfigFactory
        except ImportError:
            raise BackendError(self._MISSING_DEP_MESSAGE) from None
        except Exception as e:
            raise BackendError(
                "hocon_data backend could not import 'pyhocon' ({}: {}); "
                'pip install "pyera[hocon]"'.format(type(e).__name__, e)
            ) from None
        _install_hocon_include_guard()
        text = _strip_hocon_includes(text)
        token = _HOCON_INCLUDE_GUARD.set(True)
        try:
            parsed = ConfigFactory.parse_string(text)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(_one_line(str(e))) from None
        finally:
            _HOCON_INCLUDE_GUARD.reset(token)
        return _as_plain(parsed)


def _refuse_batch_shim(exe: str) -> None:
    """Raise if *exe* is a ``.bat``/``.cmd`` shim, in any letter case."""
    if os.path.splitext(exe)[1].lower() in (".bat", ".cmd"):
        raise BackendError(
            "refusing to run sops batch shim {}: cmd.exe re-parses its own "
            "argument line, which is unsafe for a data-derived path".format(exe)
        )


def _run_sops(path, input_type: str) -> bytes:
    """Run ``sops -d`` on ``path`` and return its decrypted stdout.

    Hardened for unattended use: the resolved executable is run by its
    absolute path (never a bare name re-resolved by the child), a batch
    shim (``.bat``/``.cmd``) is refused outright (``cmd.exe`` re-parses its
    own argument line, which a data path containing shell metacharacters
    could abuse), and the data path is always passed absolute and after a
    literal ``--`` so a path/scope value starting with ``-`` can never be
    read as a sops option.
    """
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
    try:
        proc = subprocess.run(
            [
                exe,
                "--input-type={}".format(input_type),
                "--output-type={}".format(input_type),
                "-d",
                "--",
                abs_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SOPS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        # `from None`, not `from e`: a TimeoutExpired carries the
        # subprocess's partial stdout (possibly partially-decrypted
        # plaintext) as an attribute, which chaining would keep reachable
        # via `__cause__.stdout` on the raised BackendError.
        raise BackendError(
            "sops timed out after {}s decrypting {}".format(SOPS_TIMEOUT, path)
        ) from None
    except OSError as e:
        raise BackendError("Failed to run sops on {}: {}".format(path, e)) from e

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise BackendError(
            "sops failed (exit {}) decrypting {}: {}".format(
                proc.returncode, path, detail or "<no stderr>"
            )
        )
    return proc.stdout


class SopsBackend(Backend):
    """YAML decrypted on the fly via the ``sops`` CLI (``sops_data``).

    Hardened for unattended use: the subprocess has a finite timeout, its
    stderr is captured and surfaced, a missing ``sops`` binary raises a
    clear :class:`BackendError` instead of an opaque ``FileNotFoundError``,
    and a decrypted file that fails to parse reports only the problem and
    its line/column -- never the decrypted plaintext.

    Only YAML in this phase; ``sops_formats`` generalizes it to sops's own
    format inference (JSON/INI/dotenv) and the ``sops``/``sops_<format>``
    names).
    """

    NAMES = {"function": ("sops_data",)}

    def data_hash(self, path, options):
        raw = _run_sops(path, "yaml")
        yaml_backend = YAMLBackend(strict=self.strict)
        problem = None
        try:
            text = raw.decode("utf-8")
            parsed = yaml_backend.loads(text)
        except UnicodeDecodeError as e:
            problem = str(e)
        except BackendError as e:
            problem = str(e)
        else:
            return yaml_backend._as_data_hash(parsed, path)
        raise BackendError(
            "Unable to parse ({}): {}".format(path, problem), path=str(path)
        )


def default_backends():
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
