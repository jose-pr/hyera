"""Function providers: dispatching a hierarchy level's ``data_hash``,
``lookup_key`` or ``data_dig`` function per key and per location, with a
:class:`LookupContext` for the ``lookup_key``/``data_dig`` cases.

Original code (no phiera lineage); ports the *design* of Puppet's
``pops/lookup/function_provider.rb``, ``data_hash_function_provider.rb``,
``lookup_key_function_provider.rb``, ``data_dig_function_provider.rb`` and
``context.rb`` -- not translated line by line, so no "Ported from ..."
header.
"""

import contextlib
import os
import typing as _ty

from pathlib_next import Path

from ._explain import _LocationRef
from ._interpolation import interpolate, unshare
from ._invocation import Invocation
from ._lookup_adapter import validate_data_value
from ._navigation import _MISSING, key_to_a, undig
from .exceptions import BackendError

__all__ = ["LookupContext", "PROVIDER_CLASSES"]

#: The shared no-op context manager every ``at_location`` closure below uses
#: for a location-less entry (``locations is None`` -> a single ``None``
#: "location"): Puppet's own function providers call their function with no
#: location at all there, so nothing about a location is ever pushed.
_NULL_CONTEXT = contextlib.nullcontext()


def _location_ref(location) -> _LocationRef:
    """A :class:`~hyera._explain._LocationRef` for one resolved location
    (``core._Location``/``_location_resolver.ResolvedLocation`` -- both
    ``(original, location, is_uri, exist)``-shaped). The path form always
    renders POSIX (``Path.as_posix()``), matching what Ruby's ``Pathname``
    prints on every OS, regardless of this interned string's own separator.
    """
    if location.is_uri:
        return _LocationRef(location.original, location.location, "uri")
    return _LocationRef(location.original, Path(location.location).as_posix(), "path")


def _recording_location(invocation, location):
    """``invocation.recording("location", ...)`` for a real location, else
    the shared no-op -- factored out since all three provider kinds need it
    identically."""
    if location is None:
        return _NULL_CONTEXT
    return invocation.recording("location", _location_ref(location))


class _NotFound(BaseException):
    """Puppet's ``throw :no_such_key`` (``context.rb``'s ``not_found``): a
    control-flow signal, not an application error. Deriving from
    :class:`BaseException` instead of :class:`Exception` means a backend's
    own ``except Exception`` (or bare ``except:`` written as one) does not
    accidentally swallow it, mirroring Ruby ``throw``/``catch``, which a
    plain ``rescue => e`` never catches either.
    """


def _puppet_type_label(value) -> str:
    if isinstance(value, bool):
        return "Boolean"
    if value is None:
        return "Undef"
    if isinstance(value, str):
        return "String"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, list):
        return "Tuple" if value else "Array"
    return type(value).__name__


def _validate_data_hash(data, name, location) -> None:
    if isinstance(data, dict):
        return
    if location is None:
        raise BackendError(
            "Value returned from data_hash function '{}' has wrong type, "
            "expects a Hash value, got {}".format(name, _puppet_type_label(data))
        )
    raise BackendError(
        "Value returned from data_hash function '{}', when using location "
        "'{}', has wrong type, expects a Hash value, got {}".format(
            name, location, _puppet_type_label(data)
        ),
        path=str(location),
    )


def _validate_provider_value(value, kind, name, location) -> None:
    """The ``lookup_key``/``data_dig`` value check
    (``{lookup_key,data_dig}_function_provider.rb``'s own ``assert_value_type``,
    the same RichData rule as :func:`~hyera._lookup_adapter.validate_data_value`
    but worded for a scalar return rather than a hash entry)."""
    from ._lookup_adapter import _lookup_value_type
    from ._types import infer

    t = _lookup_value_type()
    if t.instance(value):
        return
    if location is None:
        raise BackendError(
            "Value returned from {} function '{}' has wrong type, expects "
            "Puppet::LookupValue, got {}".format(kind, name, infer(value))
        )
    raise BackendError(
        "Value returned from {} function '{}', when using location '{}', "
        "has wrong type, expects Puppet::LookupValue, got {}".format(
            kind, name, location, infer(value)
        ),
        path=str(location),
    )


class _EnvironmentContext:
    """Per-``Hiera``-instance file cache, shared by every view
    (``context.rb:48-58``'s ``cached_file_data``): revalidated by
    ``(inode, mtime_ns, size)``, not by content.
    """

    def __init__(self) -> None:
        self._cache: _ty.Dict[str, _ty.Tuple[_ty.Any, _ty.Any]] = {}

    def clear(self) -> None:
        """Drop every cached file (``Hiera.clear_cache()``)."""
        self._cache.clear()

    def cached_file_data(
        self,
        path: _ty.Union[str, "os.PathLike[str]"],
        parse: _ty.Optional[_ty.Callable[[str], _ty.Any]] = None,
    ) -> _ty.Any:
        """The cached result of ``parse(text)`` (or the raw text when
        ``parse`` is ``None``) for the file at ``path``, revalidated by
        ``(inode, mtime_ns, size)``, not by content."""
        path = os.fspath(path)
        try:
            st = os.stat(path)
        except OSError as e:
            raise BackendError(
                "Unable to read ({}): {}".format(path, e.strerror or e), path=path
            ) from e
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        with open(path, "rb") as fh:
            raw = fh.read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BackendError(
                "Unable to parse ({}): {}".format(path, e), path=path
            ) from e
        try:
            value = parse(text) if parse is not None else text
        except BackendError as e:
            if e.path is None:
                raise BackendError(
                    "Unable to parse ({}): {}".format(path, e), path=path
                ) from e
            raise
        self._cache[path] = (stamp, value)
        return value


class _FunctionContext:
    """Puppet's per-hierarchy-entry ``FunctionContext``
    (``context.rb:61-117``), scoped to one scope-binding object (one
    ``Hiera``/``h.scoped(...)`` view) and one location within its level."""

    def __init__(
        self,
        environment_context: _EnvironmentContext,
        environment_name: _ty.Optional[str],
        module_name: _ty.Optional[str] = None,
    ) -> None:
        self.environment_context = environment_context
        self.environment_name = environment_name
        self.module_name = module_name
        #: Filled once per location by a ``data_hash`` provider.
        self.data_hash: _ty.Optional[_ty.Dict[str, _ty.Any]] = None
        #: The location label a ``data_hash`` provider validated ``data_hash``
        #: with -- ``None`` for a location-less entry, else ``str(location)``.
        self.label: _ty.Optional[str] = None
        self._cache: _ty.Dict[_ty.Any, _ty.Any] = {}

    def has_cached(self, key: _ty.Any) -> bool:
        """Whether ``key`` was already cached for this location (by a
        ``lookup_key``/``data_dig`` provider's own ``context.cache(...)``
        call on a previous invocation)."""
        return key in self._cache


class LookupContext:
    """The ``context`` argument handed to a ``lookup_key``/``data_dig``
    backend hook (Puppet's public ``Context`` API, ``context.rb:126-206``).

    Built by the engine for each call; a backend never constructs one
    itself.

    :param function_context: the per-location state to read/write through.
    :param invocation: the current lookup's per-lookup state.
    """

    def __init__(
        self, function_context: _FunctionContext, invocation: Invocation
    ) -> None:
        self._fc = function_context
        self._invocation = invocation

    def interpolate(self, value: _ty.Any) -> _ty.Any:
        """Interpolate ``value`` (methods allowed) against the current
        lookup's scope -- a backend calls this itself; the engine never
        interpolates a ``lookup_key``/``data_dig`` result on its own.

        :param value: the value (or nested structure) to interpolate.
        :returns: the interpolated result.
        """
        return interpolate(value, self._invocation, allow_methods=True)

    def not_found(self) -> "_ty.NoReturn":
        """Signal a miss for this location -- Puppet's ``throw :no_such_key``.

        :raises Exception: always; the raised object is an internal
            control-flow signal, not a documented public exception.
        """
        raise _NotFound()

    def explain(self, producer: _ty.Callable[[], str]) -> None:
        """Add ``producer``'s text to this lookup's ``explain()`` report.

        :param producer: a zero-argument callable producing the text.
        """
        self._invocation.report_text(producer)

    def cache(self, key: _ty.Any, value: _ty.Any) -> _ty.Any:
        """Cache ``value`` under ``key`` for this location, for the life of
        the owning ``Hiera``/``h.scoped(...)`` view. Returns ``value``.

        :param key: the cache key.
        :param value: the value to cache.
        :returns: ``value``, unchanged.
        """
        self._fc._cache[key] = value
        return value

    def cache_all(self, mapping: _ty.Mapping[_ty.Any, _ty.Any]) -> None:
        """:meth:`cache` every key/value pair of ``mapping``.

        :param mapping: the key/value pairs to cache.
        """
        self._fc._cache.update(mapping)

    def cache_has_key(self, key: _ty.Any) -> bool:
        """Whether ``key`` was already :meth:`cache`\\ d for this location.

        :param key: the cache key.
        :returns: whether ``key`` is cached.
        """
        return key in self._fc._cache

    def cached_value(self, key: _ty.Any) -> _ty.Any:
        """The value :meth:`cache`\\ d under ``key``, or ``None``.

        :param key: the cache key.
        :returns: the cached value, or ``None``.
        """
        return self._fc._cache.get(key)

    def cached_entries(self) -> "_ty.Iterator[_ty.Tuple[_ty.Any, _ty.Any]]":
        """An iterator over every ``(key, value)`` pair :meth:`cache`\\ d
        for this location.

        :returns: an iterator of ``(key, value)`` pairs.
        """
        return iter(list(self._fc._cache.items()))

    def cached_file_data(
        self,
        path: _ty.Union[str, "os.PathLike[str]"],
        parse: _ty.Optional[_ty.Callable[[str], _ty.Any]] = None,
    ) -> _ty.Any:
        """The cached result of ``parse(text)`` (or the raw text when
        ``parse`` is ``None``) for the file at ``path``, shared with every
        other location of this ``Hiera`` instance (see
        :class:`_EnvironmentContext`).

        :param path: the file to read.
        :param parse: applied to the file's text; identity when omitted.
        :returns: the (cached) parsed result, or raw text.
        :raises BackendError: ``path`` could not be read, decoded as UTF-8,
            or ``parse`` raised a :class:`BackendError` of its own.
        """
        return self._fc.environment_context.cached_file_data(path, parse)

    @property
    def environment_name(self) -> _ty.Optional[str]:
        """The current lookup's environment name, or ``None``."""
        return self._fc.environment_name

    @property
    def module_name(self) -> _ty.Optional[str]:
        """The current hierarchy entry's module name, or ``None`` at the
        global/environment layer."""
        return self._fc.module_name


class _FunctionProvider:
    """One hierarchy level's function, bound to one scope (one ``Hiera``/
    ``h.scoped(...)`` view) -- built once per ``(tag, level index)`` and
    never shared with another view (``function_provider.rb`` plus
    ``configured_data_provider.rb``'s per-level half).

    ``locations`` is ``None`` for a location-less entry (the function is
    called once, with no location) or a list of
    :class:`~hyera._location_resolver.ResolvedLocation` (possibly empty,
    meaning the function is never called at all).
    """

    kind: str = None

    def __init__(
        self,
        name,
        backend,
        options,
        locations,
        environment_context: _EnvironmentContext,
        environment_name,
        load_file=None,
        prune=None,
        revalidate=True,
    ):
        self.name = name
        self.backend = backend
        self.options = options or {}
        self.locations = locations
        self._environment_context = environment_context
        self._environment_name = environment_name
        self._load_file = load_file
        #: Mirrors the owning ``Hiera``'s own ``revalidate`` -- read by
        #: :class:`_DataHashProvider` to decide whether a location already
        #: cached on this provider's own ``_FunctionContext`` needs to go
        #: through ``load_file`` again (probe-checked) or can be reused
        #: outright (:meth:`_DataHashProvider.key_lookup`).
        self._revalidate = revalidate
        #: ``(data, function_name, location) -> data``, set only for a level
        #: owned by a module (``core.Hiera._build_provider``): Puppet's
        #: module-data namespace rule
        #: (:func:`~hyera._data_provider.prune_module_data`) applied to a
        #: ``data_hash`` result only -- a ``lookup_key``/``data_dig`` value
        #: is never pruned (``data_hash_function_provider.rb:72``).
        self._prune = prune
        self._contexts: dict = {}

    def options_for(self, location) -> dict:
        """Puppet's ``options.merge('path'/'uri' => ...)``
        (``function_provider.rb:62-72``)."""
        opts = dict(self.options)
        if location is None:
            return opts
        if location.is_uri:
            opts["uri"] = location.location
        else:
            opts["path"] = str(location.location)
        return opts

    def _context(self, location) -> _FunctionContext:
        key = location.location if location is not None else None
        fc = self._contexts.get(key)
        if fc is None:
            fc = _FunctionContext(self._environment_context, self._environment_name)
            self._contexts[key] = fc
        return fc

    def key_lookup(self, root, segments, invocation, merge):
        """Reduce this level's locations for ``root`` (plus, for a
        ``data_dig`` provider only, its sub-navigation ``segments``) with
        ``merge`` (an already-resolved
        :class:`~hyera._merge_strategy.MergeStrategy`), returning the merged
        root value or :data:`~hyera._navigation._MISSING`.

        ``segments`` is accepted by every kind so the level loop can call
        them uniformly, but only :class:`_DataDigProvider` reads it --
        ``data_hash``/``lookup_key`` never interpolate or dig past the root
        key themselves; the caller digs the merged root value afterwards
        (once, not per location or per level).
        """
        raise NotImplementedError


class _DataHashProvider(_FunctionProvider):
    kind = "data_hash"

    def key_lookup(self, root, segments, invocation, merge):
        locations = self.locations if self.locations is not None else [None]

        def at_location(location):
            with _recording_location(invocation, location):
                return _at_location(location)

        def _at_location(location):
            if location is not None and not location.exist:
                invocation.report_location_not_found()
                return _MISSING
            ctx = self._context(location)
            if location is not None and not location.is_uri:
                # A real file. While `self._revalidate`, `Hiera._load_file`
                # owns both the parsed-content cache and its revalidation
                # (probed at most once per top-level lookup, through
                # `invocation`'s memo) and must run on every call -- gating
                # it behind `ctx.data_hash` would skip revalidation after
                # the first lookup this (view, provider) pair ever makes.
                # With revalidation off there is nothing left for a repeat
                # call to discover (the file is read at most once for the
                # instance's life either way), so a lookup after the first
                # skips `load_file` entirely -- no options merge, no cache
                # key, no lock -- the same fast path a location-less/``uri``
                # entry already gets below.
                path = str(location.location)
                if self._revalidate or ctx.data_hash is None:
                    options = self.options_for(location)
                    data = self._load_file(path, self.backend, options, invocation)
                    if data is _MISSING:
                        return _MISSING
                    label = path
                    _validate_data_hash(data, self.backend.name, label)
                    if self._prune is not None:
                        data = self._prune(data, self.backend.name, label)
                    ctx.data_hash = data
                    ctx.label = label
                data = ctx.data_hash
                label = ctx.label
            else:
                # No location, or a uri: no file-based staleness signal, so
                # cache the function's own result once per (view, provider,
                # location), the same way Puppet's `ctx.data_hash ||=` does.
                if ctx.data_hash is None:
                    options = self.options_for(location)
                    label = None if location is None else str(location.location)
                    raw = self.backend.data_hash(None, options)
                    _validate_data_hash(raw, self.backend.name, label)
                    if self._prune is not None:
                        raw = self._prune(raw, self.backend.name, label)
                    ctx.data_hash = raw
                    ctx.label = label
                data = ctx.data_hash
                label = ctx.label
            if root not in data:
                invocation.report_not_found(root)
                return _MISSING
            value = data[root]
            validate_data_value(value, self.backend.name, label, root)
            result = interpolate(value, invocation, allow_methods=True)
            return invocation.report_found(root, result)

        return merge.lookup(locations, at_location, invocation)


class _LookupKeyProvider(_FunctionProvider):
    kind = "lookup_key"

    def key_lookup(self, root, segments, invocation, merge):
        locations = self.locations if self.locations is not None else [None]

        def at_location(location):
            with _recording_location(invocation, location):
                if location is not None and not location.exist:
                    invocation.report_location_not_found()
                    return _MISSING
                ctx = self._context(location)
                if ctx.has_cached(root):
                    return invocation.report_found(root, unshare(ctx._cache[root]))
                options = self.options_for(location)
                label = None if location is None else str(location.location)
                context = LookupContext(ctx, invocation)
                try:
                    value = self.backend.lookup_key(root, options, context)
                except _NotFound:
                    invocation.report_not_found(root)
                    return _MISSING
                _validate_provider_value(value, "lookup_key", self.backend.name, label)
                ctx._cache[root] = value
                return invocation.report_found(root, unshare(value))

        return merge.lookup(locations, at_location, invocation)


class _DataDigProvider(_FunctionProvider):
    kind = "data_dig"

    def key_lookup(self, root, segments, invocation, merge):
        locations = self.locations if self.locations is not None else [None]
        full_key = key_to_a(root, segments)
        cache_key = str(full_key)

        def at_location(location):
            with _recording_location(invocation, location):
                if location is not None and not location.exist:
                    invocation.report_location_not_found()
                    return _MISSING
                ctx = self._context(location)
                if ctx.has_cached(cache_key):
                    return invocation.report_found(root, unshare(ctx._cache[cache_key]))
                options = self.options_for(location)
                label = None if location is None else str(location.location)
                context = LookupContext(ctx, invocation)
                try:
                    value = self.backend.data_dig(list(full_key), options, context)
                except _NotFound:
                    invocation.report_not_found(root)
                    return _MISSING
                _validate_provider_value(value, "data_dig", self.backend.name, label)
                wrapped = undig(segments, value)
                ctx._cache[cache_key] = wrapped
                return invocation.report_found(root, unshare(wrapped))

        return merge.lookup(locations, at_location, invocation)


PROVIDER_CLASSES = {
    "data_hash": _DataHashProvider,
    "lookup_key": _LookupKeyProvider,
    "data_dig": _DataDigProvider,
}
