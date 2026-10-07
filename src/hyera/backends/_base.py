# Ported from Puppet 8 lib/puppet/functions/yaml_data.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The :class:`Backend` contract and the registry that keys it."""

from __future__ import annotations

import logging
import os
import re
import typing as _ty
from typing import NamedTuple

from pathlib_next import Path

from ..exceptions import BackendError, ConfigError
from .._limits import _LIMITS, Limits
from .._lookup.function_provider import LookupContext
from .._scope.scope import Strict
from .._enums import _StrEnum, _plain
from ._entry_points import load as _load_entry_points

_LOGGER = logging.getLogger(__name__)

#: Plain strings, not :class:`Strict` members: interpolated into the
#: ``ValueError`` below, whose text must stay exactly what it was before
#: this enum existed.
_STRICT_VALUES = ("error", "warning", "off")


class BackendKind(_StrEnum):
    """Which of :attr:`Backend.KINDS` a registered name belongs to: the
    ``kind=`` argument of :meth:`Backend.find`/:meth:`Backend.get`/
    :meth:`Backend.new`/:meth:`Backend.names`. A name is only ever
    registered, and looked up, within one namespace."""

    FUNCTION = "function"
    """A Hiera 5 hierarchy entry's ``data_hash``/``lookup_key``/
    ``data_dig`` function name (``yaml_data``, ``json_data``, ...)."""

    V3 = "v3"
    """A Hiera 3/``hiera3_backend`` backend name (``yaml``, ``json``, ...)."""

    FORMAT = "format"
    """A plain data-format name, also matched by :meth:`Backend.for_path`'s
    file-extension lookup (``yaml``, ``json``, ...)."""

    RENDER = "render"
    """A ``puppet lookup --render-as`` output format (``s``, ``json``,
    ``yaml``)."""


def _default_strict() -> str:
    """The call-time default for :attr:`Backend.strict` when not set
    explicitly on the instance -- mirrors how ``yaml_data.rb`` reads
    ``Puppet[:strict]`` at call time rather than at construction.

    Reads ``hyera._lookup.invocation._STRICT`` (a ``ContextVar``, default
    ``"warning"``), set from ``invocation.scope.strict`` around the
    top-level lookup entry (``core.Hiera._get``) and reset in ``finally``.
    A level's backend is shared across scopes, so it never stores a scope's
    strictness itself; imported lazily to avoid a hard import-time
    dependency from this module onto ``_invocation``.
    """
    from .._lookup.invocation import _STRICT

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
    """A data format and/or Hiera 5 provider, registered by name.

    A subclass registers itself by declaring ``NAMES``, a mapping of
    namespace (one of :attr:`KINDS`) to the names (plain strings and/or
    :class:`NamePattern`) it answers to in that namespace. Puppet function
    names (``data_hash``/``lookup_key``/``data_dig`` values in a v5
    hierarchy), v3 backend names, plain format names and CLI/render names
    are separate namespaces -- a name is only ever looked up within one.

    Anything a backend does not implement raises :class:`NotImplementedError`
    from the methods below (the base class's defaults); callers turn that
    into a specific, user-facing error.

    :param conf: the hierarchy entry's/``defaults``'s own mapping (never
        used by the base class; a subclass may read its own keys from it).
    :param strict: overrides the call-time default (see :attr:`strict`).
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
        strict: _ty.Optional[_ty.Union[Strict, str]] = None,
    ) -> None:
        self.conf: _ty.Mapping[str, _ty.Any] = conf or {}
        if strict is not None and strict not in _STRICT_VALUES:
            raise ValueError(
                "strict must be one of {!r}, not {!r}".format(_STRICT_VALUES, strict)
            )
        self._strict = _plain(strict)
        self.name: _ty.Optional[str] = type(self)._default_name()

    @property
    def strict(self) -> str:
        """This backend's effective strictness: an explicit constructor
        value, else the call-time default (see :func:`_default_strict`).
        Never cache a result that depends on this -- it can change call to
        call once the ContextVar-backed default lands.
        """
        return self._strict if self._strict is not None else _default_strict()

    @property
    def limits(self) -> "_ty.Optional[Limits]":
        """The :class:`~hyera.Limits` of the read in progress, or ``None``
        when nothing is bounded. A backend that parses untrusted text honours
        the fields it can.
        """
        return _LIMITS.get()

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
                    for exact, other in registry["exact"].items():
                        if entry.regex.fullmatch(exact):
                            raise ValueError(
                                "pattern {!r} ({} kind) would take over the "
                                "name {!r} registered to {}; {} cannot "
                                "register it".format(
                                    entry.display,
                                    kind,
                                    exact,
                                    other.__name__,
                                    cls.__name__,
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
                    for pattern, other in registry["patterns"]:
                        if pattern.regex.fullmatch(entry):
                            raise ValueError(
                                "{!r} ({} kind) is already answered by the "
                                "pattern {!r} of {}; {} cannot register "
                                "it".format(
                                    entry,
                                    kind,
                                    pattern.display,
                                    other.__name__,
                                    cls.__name__,
                                )
                            )
                    registry["exact"][entry] = cls

    # -- lookup -----------------------------------------------------------

    @classmethod
    def _match(cls, name, kind="function"):
        _load_entry_points()
        kind = _plain(kind)
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
        cls, name: str, kind: _ty.Union[BackendKind, str] = "function"
    ) -> "_ty.Optional[_ty.Type[Backend]]":
        """The registered class for ``name`` in ``kind``, or ``None``.

        :param name: the registered name (or a matching pattern) to find.
        :param kind: the namespace to search.
        :returns: the class, or ``None`` when unregistered.
        """
        found, _captures = cls._match(name, kind)
        return found

    @classmethod
    def get(
        cls, name: str, kind: _ty.Union[BackendKind, str] = "function"
    ) -> "_ty.Type[Backend]":
        """The registered, available class for ``name`` in ``kind``.

        Raises :class:`BackendError` for an unknown name (listing the known
        names) or, via :meth:`check_available`, for a registered backend
        whose optional dependency is missing.

        :param name: the registered name (or a matching pattern) to get.
        :param kind: the namespace to search.
        :returns: the class.
        :raises BackendError: ``name`` is unregistered in ``kind``, or is
            registered but unusable (a missing optional dependency).
        """
        kind = _plain(kind)
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
        kind: _ty.Union[BackendKind, str] = "function",
        strict: _ty.Optional[_ty.Union[Strict, str]] = None,
    ) -> "Backend":
        """Instantiate the registered backend for ``name`` in ``kind``.

        Any named groups captured by a matching :class:`NamePattern` are
        passed as constructor keywords. ``.name`` is set to ``name`` (the
        name actually asked for, which may be a pattern instance such as
        ``sops_json``, not the class's default name).

        :param name: the registered name (or a matching pattern) to
            instantiate.
        :param conf: passed to the backend's constructor.
        :param kind: the namespace to search.
        :param strict: passed to the backend's constructor.
        :returns: the new instance.
        :raises BackendError: ``name`` is unregistered in ``kind``, or is
            registered but unusable (a missing optional dependency).
        """
        kind = _plain(kind)
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
    def names(cls, kind: _ty.Union[BackendKind, str] = "function") -> _ty.List[str]:
        """Registered names in ``kind``: exact names in registration order,
        then patterns by their :attr:`NamePattern.display`.

        :param kind: the namespace to list.
        :returns: the registered names.
        """
        _load_entry_points()
        kind = _plain(kind)
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
        ``None``.

        :param path: the file path to match.
        :returns: the class, or ``None`` when nothing matches.
        """
        _load_entry_points()
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

        :param op: one of ``"load"``, ``"dump"``, ``"data_hash"``,
            ``"loads"``, ``"dumps"``, ``"lookup_key"``, ``"data_dig"``.
        :returns: whether this class implements ``op``.
        :raises ValueError: ``op`` is not one of those names.
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
        """Parse ``text`` (a ``str``). Raises path-free problem text.

        :param text: the text to parse.
        :returns: the parsed value.
        :raises NotImplementedError: the base class; a subclass must
            override this to support ``format``/``function`` parsing.
        """
        raise NotImplementedError(
            "{} does not implement .loads()".format(type(self).__name__)
        )

    def dumps(self, obj: _ty.Any, **kw: _ty.Any) -> str:
        """Render ``obj`` back to text. Raises path-free problem text.

        :param obj: the value to render.
        :param kw: format-specific rendering options.
        :returns: the rendered text.
        :raises NotImplementedError: the base class; a subclass must
            override this to support ``render`` rendering.
        """
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
        Because ``.path`` is set here, a caller (``_LocationStore.load_file``) does
        not need to prefix it again.

        :param source: a path-like, or an already-open file object.
        :returns: the parsed value.
        :raises BackendError: ``source`` could not be decoded as UTF-8 or
            parsed by :meth:`loads`.
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
        """Render ``obj`` and write it to the open text file ``fp``.

        :param obj: the value to render.
        :param fp: the open text file to write to.
        :param kw: format-specific rendering options.
        """
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
        context: LookupContext,
    ) -> _ty.Dict[str, _ty.Any]:
        """The ``data_hash`` provider hook: parse the whole file at
        ``path`` and adapt it into hiera data (see :meth:`_as_data_hash`).

        The base implementation is a *file* function: it accepts only a
        single ``path`` location and no hierarchy ``options``
        (:meth:`_require_path_only`), Puppet's own ``yaml_data``/
        ``json_data``/``hocon_data`` contract.

        :param path: the location's file path.
        :param options: the hierarchy entry's ``options``.
        :param context: the per-location :class:`LookupContext`; calling its
            ``not_found()`` is an error here.
        :returns: the parsed data, adapted into a hash.
        :raises ConfigError: ``options`` carries anything besides ``path``.
        :raises BackendError: the file could not be read or parsed.
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
        overridden (see :class:`EyamlBackend`).

        :param key: the dotted key to resolve.
        :param options: the hierarchy entry's ``options``.
        :param context: the per-location :class:`LookupContext`.
        :returns: the found value.
        :raises NotImplementedError: the base class; a subclass must
            override this to support ``lookup_key``.
        """
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
        :class:`NotImplementedError` unless overridden.

        :param key_segments: the already-split dotted key path.
        :param options: the hierarchy entry's ``options``.
        :param context: the per-location :class:`LookupContext`.
        :returns: the found value.
        :raises NotImplementedError: the base class; a subclass must
            override this to support ``data_dig``.
        """
        raise NotImplementedError(
            "{} does not implement .data_dig()".format(type(self).__name__)
        )


def default_backends() -> "_ty.List[_ty.Type[Backend]]":
    """The distinct backend classes registered in the ``function``
    namespace, in definition order (YAML, JSON, HOCON, sops, eyaml).

    :returns: the default ``Hiera(backends=...)`` allow-list.
    """
    _load_entry_points()
    registry = Backend._REGISTRY.get("function", {"exact": {}, "patterns": []})
    seen = []
    for cls in registry["exact"].values():
        if cls not in seen:
            seen.append(cls)
    for _pattern, cls in registry["patterns"]:
        if cls not in seen:
            seen.append(cls)
    return seen
