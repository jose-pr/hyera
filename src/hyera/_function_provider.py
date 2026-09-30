"""Function providers: dispatching a hierarchy level's ``data_hash``,
``lookup_key`` or ``data_dig`` function per key and per location, with a
:class:`LookupContext` for the ``lookup_key``/``data_dig`` cases.

Original code (no phiera lineage); ports the *design* of Puppet's
``pops/lookup/function_provider.rb``, ``data_hash_function_provider.rb``,
``lookup_key_function_provider.rb``, ``data_dig_function_provider.rb`` and
``context.rb`` -- not translated line by line, so no "Ported from ..."
header.
"""

import os
import typing as _ty

from ._interpolation import interpolate, unshare
from ._lookup_adapter import validate_data_value
from ._navigation import _MISSING, key_to_a, undig
from .exceptions import BackendError

__all__ = ["LookupContext", "PROVIDER_CLASSES"]


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

    def __init__(self):
        self._cache: dict = {}

    def clear(self) -> None:
        """Drop every cached file (``Hiera.clear_cache()``)."""
        self._cache.clear()

    def cached_file_data(self, path, parse=None):
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
        environment_name,
        module_name=None,
    ):
        self.environment_context = environment_context
        self.environment_name = environment_name
        self.module_name = module_name
        #: Filled once per location by a ``data_hash`` provider.
        self.data_hash = None
        #: The location label a ``data_hash`` provider validated ``data_hash``
        #: with -- ``None`` for a location-less entry, else ``str(location)``.
        self.label = None
        self._cache: dict = {}

    def has_cached(self, key) -> bool:
        return key in self._cache


class LookupContext:
    """The ``context`` argument handed to a ``lookup_key``/``data_dig``
    backend hook (Puppet's public ``Context`` API, ``context.rb:126-206``).
    """

    def __init__(self, function_context: _FunctionContext, invocation):
        self._fc = function_context
        self._invocation = invocation

    def interpolate(self, value):
        """Interpolate ``value`` (methods allowed) against the current
        lookup's scope -- a backend calls this itself; the engine never
        interpolates a ``lookup_key``/``data_dig`` result on its own."""
        return interpolate(value, self._invocation, allow_methods=True)

    def not_found(self) -> "_ty.NoReturn":
        """Signal a miss for this location -- Puppet's ``throw :no_such_key``."""
        raise _NotFound()

    def explain(self, producer) -> None:
        self._invocation.report_text(producer)

    def cache(self, key, value):
        self._fc._cache[key] = value
        return value

    def cache_all(self, mapping) -> None:
        self._fc._cache.update(mapping)

    def cache_has_key(self, key) -> bool:
        return key in self._fc._cache

    def cached_value(self, key):
        return self._fc._cache.get(key)

    def cached_entries(self):
        return iter(list(self._fc._cache.items()))

    def cached_file_data(self, path, parse=None):
        return self._fc.environment_context.cached_file_data(path, parse)

    @property
    def environment_name(self):
        return self._fc.environment_name

    @property
    def module_name(self):
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
    ):
        self.name = name
        self.backend = backend
        self.options = options or {}
        self.locations = locations
        self._environment_context = environment_context
        self._environment_name = environment_name
        self._load_file = load_file
        #: ``(data, function_name, location) -> data``, set only for a level
        #: owned by a module (``core.Hiera._build_provider``): Puppet's
        #: module-data namespace rule
        #: (:func:`~hyera._data_provider.prune_module_data`) applied to a
        #: ``data_hash`` result only -- a ``lookup_key``/``data_dig`` value
        #: is never pruned (``data_hash_function_provider.rb:72``).
        self._prune = prune
        self._contexts: dict = {}

    @property
    def full_name(self) -> str:
        return "{} function '{}'".format(self.kind, self.backend.name)

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
            if location is not None and not location.exist:
                return _MISSING
            ctx = self._context(location)
            if ctx.data_hash is None:
                options = self.options_for(location)
                if location is None:
                    data = self.backend.data_hash(None, options)
                    label = None
                elif location.is_uri:
                    data = self.backend.data_hash(None, options)
                    label = str(location.location)
                else:
                    path = str(location.location)
                    data = self._load_file(path, self.backend, options)
                    label = path
                _validate_data_hash(data, self.backend.name, label)
                if self._prune is not None:
                    data = self._prune(data, self.backend.name, label)
                ctx.data_hash = data
                ctx.label = label
            data = ctx.data_hash
            if root not in data:
                return _MISSING
            value = data[root]
            validate_data_value(value, self.backend.name, ctx.label, root)
            return interpolate(value, invocation, allow_methods=True)

        return merge.lookup(locations, at_location)


class _LookupKeyProvider(_FunctionProvider):
    kind = "lookup_key"

    def key_lookup(self, root, segments, invocation, merge):
        locations = self.locations if self.locations is not None else [None]

        def at_location(location):
            if location is not None and not location.exist:
                return _MISSING
            ctx = self._context(location)
            if ctx.has_cached(root):
                return unshare(ctx._cache[root])
            options = self.options_for(location)
            label = None if location is None else str(location.location)
            context = LookupContext(ctx, invocation)
            try:
                value = self.backend.lookup_key(root, options, context)
            except _NotFound:
                return _MISSING
            _validate_provider_value(value, "lookup_key", self.backend.name, label)
            ctx._cache[root] = value
            return unshare(value)

        return merge.lookup(locations, at_location)


class _DataDigProvider(_FunctionProvider):
    kind = "data_dig"

    def key_lookup(self, root, segments, invocation, merge):
        locations = self.locations if self.locations is not None else [None]
        full_key = key_to_a(root, segments)
        cache_key = str(full_key)

        def at_location(location):
            if location is not None and not location.exist:
                return _MISSING
            ctx = self._context(location)
            if ctx.has_cached(cache_key):
                return unshare(ctx._cache[cache_key])
            options = self.options_for(location)
            label = None if location is None else str(location.location)
            context = LookupContext(ctx, invocation)
            try:
                value = self.backend.data_dig(list(full_key), options, context)
            except _NotFound:
                return _MISSING
            _validate_provider_value(value, "data_dig", self.backend.name, label)
            wrapped = undig(segments, value)
            ctx._cache[cache_key] = wrapped
            return unshare(wrapped)

        return merge.lookup(locations, at_location)


PROVIDER_CLASSES = {
    "data_hash": _DataHashProvider,
    "lookup_key": _LookupKeyProvider,
    "data_dig": _DataDigProvider,
}
