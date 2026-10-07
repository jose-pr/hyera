# Ported from Puppet 8 lib/puppet/pops/lookup/{function_provider,
# data_hash_function_provider,lookup_key_function_provider,data_dig_function_provider,
# configured_data_provider}.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""The provider classes that call a hierarchy level's ``data_hash``,
``lookup_key`` or ``data_dig`` function, and ``PROVIDER_CLASSES``, the table
that picks one per function kind.

Follows Puppet's ``pops/lookup/function_provider.rb`` and its three
``*_function_provider.rb`` subclasses.
"""

from __future__ import annotations

from .function_provider import (
    LookupContext,
    _EnvironmentContext,
    _FunctionContext,
    _NotFound,
    _Result,
    _check_kind_implemented,
    _data_hash_not_found,
    _recording_location,
    _tuples_to_lists,
    _validate_data_hash,
    _validate_provider_value,
)
from .interpolation import interpolate, unshare
from .lookup_adapter import validate_data_value
from .navigation import _MISSING, key_to_a, undig


class _FunctionProvider:
    """One hierarchy level's function, bound to one scope (one ``Hiera``/
    ``h.scoped(...)`` view) -- built once per ``(tag, level index)`` and
    never shared with another view (``function_provider.rb`` plus
    ``configured_data_provider.rb``'s per-level half).

    ``locations`` is ``None`` for a location-less entry (the function is
    called once, with no location) or a list of
    :class:`~hyera._config.location_resolver.ResolvedLocation` (possibly empty,
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
        module_name=None,
        prune=None,
        revalidate=True,
        generation=0,
    ):
        self.name = name
        self.backend = backend
        self.options = options or {}
        self.locations = locations
        self._environment_context = environment_context
        self._environment_name = environment_name
        self._load_file = load_file
        #: Mirrors the owning ``Hiera``'s ``revalidate``: :class:`_DataHashProvider`
        #: reads it to decide whether a cached location goes through ``load_file`` again
        #: (probe-checked) or is reused outright (:meth:`_DataHashProvider.key_lookup`).
        self._revalidate = revalidate
        #: Module name and ``(module_name, data, function_name, location) -> data`` for
        #: a module-owned level: Puppet's namespace rule (``prune_module_data``),
        #: ``data_hash`` only (``data_hash_function_provider.rb:72``).
        self._module_name = module_name
        self._prune = prune
        #: ``Hiera.clear_cache()``'s counter when this provider was built.
        self.generation = generation
        self._contexts: dict = {}
        self._kind_ok = False

    def _require_kind_implemented(self) -> None:
        """:func:`_check_kind_implemented` for this provider's backend, run
        until it passes once (the answer never changes afterwards)."""
        if not self._kind_ok:
            _check_kind_implemented(self.backend, self.kind)
            self._kind_ok = True

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
            fc = _FunctionContext(
                self._environment_context, self._environment_name, self._module_name
            )
            self._contexts[key] = fc
        return fc

    def _kept(self, ctx, key, invocation):
        """The engine's :class:`_Result` for ``key`` at this location while
        it still holds, else ``None``.

        With ``revalidate=False`` a result lives until ``clear_cache()``.
        Otherwise it holds while every file the hook read through
        ``cached_file_data`` still has the stamp it had then; a result whose
        call read no file holds only within the lookup that produced it,
        since nothing says when the hook's source changed.
        """
        result = ctx.results.get(key)
        if result is None or not self._revalidate:
            return result
        if result.deps:
            for path, stamp in result.deps:
                if invocation._memo_probe(path).sig != stamp:
                    return None
            return result
        return result if result.serial == invocation._state.serial else None

    def _keep(self, ctx, key, value, context, invocation):
        ctx.results[key] = _Result(
            value, tuple(context._deps), invocation._state.serial
        )

    def key_lookup(self, root, segments, invocation, merge):
        """Reduce this level's locations for ``root`` (plus, for a
        ``data_dig`` provider only, its sub-navigation ``segments``) with
        ``merge`` (an already-resolved
        :class:`~hyera._lookup.merge_strategy.MergeStrategy`), returning the merged
        root value or :data:`~hyera._lookup.navigation._MISSING`.

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
            if not self._kind_ok:
                self._require_kind_implemented()
            ctx = self._context(location)
            if location is not None and not location.is_uri:
                # While `self._revalidate`, `_LocationStore.load_file` owns the content
                # cache and its revalidation (one probe per lookup) and runs every call;
                # otherwise repeat calls skip it: no options merge, key or lock.
                path = str(location.location)
                if self._revalidate or ctx.data_hash is None:
                    options = self.options_for(location)
                    data = self._load_file(
                        path,
                        self.backend,
                        options,
                        invocation,
                        LookupContext(ctx, invocation),
                    )
                    if data is _MISSING:
                        return _MISSING
                    label = path
                    _validate_data_hash(data, self.backend.name, label)
                    if self._prune is not None:
                        data = self._prune(
                            self._module_name, data, self.backend.name, label
                        )
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
                    try:
                        raw = self.backend.data_hash(
                            None, options, LookupContext(ctx, invocation)
                        )
                    except _NotFound:
                        raise _data_hash_not_found(self.backend.name, label) from None
                    _validate_data_hash(raw, self.backend.name, label)
                    if self._prune is not None:
                        raw = self._prune(
                            self._module_name, raw, self.backend.name, label
                        )
                    ctx.data_hash = raw
                    ctx.label = label
                data = ctx.data_hash
                label = ctx.label
            if root not in data:
                if invocation.explainer is not None:
                    invocation.report_not_found(root)
                return _MISSING
            value = data[root]
            validate_data_value(value, self.backend.name, label, root)
            value = _tuples_to_lists(value)
            result = interpolate(value, invocation, allow_methods=True)
            return invocation.report_found(root, result)

        # Without an explainer there is nothing to record per location, so
        # the per-location wrapper is skipped.
        visit = _at_location if invocation.explainer is None else at_location
        if merge.first_found:
            for location in locations:
                found = visit(location)
                if found is not _MISSING:
                    return found
            return _MISSING
        return merge.lookup(locations, visit, invocation)


class _LookupKeyProvider(_FunctionProvider):
    kind = "lookup_key"

    def key_lookup(self, root, segments, invocation, merge):
        locations = self.locations if self.locations is not None else [None]

        def at_location(location):
            with _recording_location(invocation, location):
                if location is not None and not location.exist:
                    invocation.report_location_not_found()
                    return _MISSING
                self._require_kind_implemented()
                ctx = self._context(location)
                kept = self._kept(ctx, root, invocation)
                if kept is not None:
                    return invocation.report_found(root, unshare(kept.value))
                options = self.options_for(location)
                label = None if location is None else str(location.location)
                context = LookupContext(ctx, invocation)
                try:
                    value = self.backend.lookup_key(root, options, context)
                except _NotFound:
                    invocation.report_not_found(root)
                    return _MISSING
                value = _validate_provider_value(
                    value, "lookup_key", self.backend.name, label
                )
                self._keep(ctx, root, value, context, invocation)
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
                self._require_kind_implemented()
                ctx = self._context(location)
                kept = self._kept(ctx, cache_key, invocation)
                if kept is not None:
                    return invocation.report_found(root, unshare(kept.value))
                options = self.options_for(location)
                label = None if location is None else str(location.location)
                context = LookupContext(ctx, invocation)
                try:
                    value = self.backend.data_dig(list(full_key), options, context)
                except _NotFound:
                    invocation.report_not_found(root)
                    return _MISSING
                value = _validate_provider_value(
                    value, "data_dig", self.backend.name, label
                )
                wrapped = undig(segments, value)
                if wrapped is _MISSING:
                    invocation.report_not_found(root)
                    return _MISSING
                self._keep(ctx, cache_key, wrapped, context, invocation)
                return invocation.report_found(root, unshare(wrapped))

        return merge.lookup(locations, at_location, invocation)


PROVIDER_CLASSES = {
    "data_hash": _DataHashProvider,
    "lookup_key": _LookupKeyProvider,
    "data_dig": _DataDigProvider,
}
