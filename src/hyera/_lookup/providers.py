# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""One function provider per hierarchy level, built for one scope (one
``Hiera`` or ``scoped()`` view) and refreshed on every lookup while
``revalidate=True``."""

from __future__ import annotations

import functools

from .provider_classes import PROVIDER_CLASSES
from .interpolation import interpolate
from .invocation import Invocation
from .._config.data_provider import prune_module_data


def _no_option_lookup(key, invocation):
    """The ``lookup`` callable for a hierarchy level's ``options``
    :class:`~hyera._lookup.invocation.Invocation`. Unreachable in practice: options
    interpolate with ``allow_methods=False``, which rejects every method
    call (``%{hiera()}``/``%{lookup()}``/``%{alias()}``) -- the only way a
    sub-lookup would ever be attempted -- before it could reach this
    callable."""
    raise RuntimeError("hierarchy options never perform a sub-lookup")


def resolved_locations_for(hiera, hierarchy, index, base_path, scope, tag, invocation):
    """The current, materialized locations for one hierarchy level
    (``hierarchy[index]``), or ``None`` for a location-less entry --
    used by both :func:`build_provider` (the first build) and
    :func:`provider_for` (a ``revalidate=True`` refresh of an
    already-cached provider)."""
    store = hiera._store
    entry = store.location_entry_for(hierarchy, base_path, scope, tag, invocation)
    resolved = store.materialize(entry, invocation)[index]
    return None if resolved is None else list(resolved)


def provider_for(
    hiera, tag, base_path, index, hierarchy, scope, invocation, module_name=None
):
    """The :class:`~hyera._lookup.provider_classes._FunctionProvider` for one
    hierarchy level, bound to ``scope`` -- built once per ``(tag,
    base_path, index)`` on this instance/view and cached in
    ``hiera._providers`` (never shared with another view; see
    :meth:`Hiera._view <hyera.core.Hiera._view>`). While ``revalidate=True``,
    an already-cached provider has its ``.locations`` refreshed in place
    (:func:`resolved_locations_for`) on every call, so a repeated
    lookup on the same view/scope still sees a changed, added or
    removed location -- rebuilding the whole provider (re-interpolating
    its ``options``) costs more than revalidating the locations
    alone.

    ``base_path`` -- the owning layer's own root -- disambiguates a
    level index across layers (the global hierarchy and every
    environment's/module's own each start indexing from 0) the same way
    :meth:`~hyera._lookup.locations._LocationStore.location_entry_for`'s own
    cache key already does; ``tag`` additionally tells a module's
    ``default_hierarchy`` apart from its main one, since both share the same
    root. ``module_name`` -- set only for a level in a module's own hierarchy
    -- makes a ``data_hash`` result go through
    :func:`~hyera._config.data_provider.prune_module_data` (Puppet's
    module-data namespace rule); it plays no part in the cache key, since a
    level's owning module never changes once built.
    """
    key = (tag, base_path, id(hierarchy), index)
    provider = hiera._providers.get(key)
    generation = hiera._generation[0]
    if provider is not None and provider.generation != generation:
        provider = None
    if provider is None:
        provider = build_provider(
            hiera,
            hierarchy,
            index,
            scope,
            base_path,
            tag,
            invocation,
            module_name,
            generation,
        )
        hiera._providers[key] = provider
    elif hiera._revalidate:
        provider.locations = resolved_locations_for(
            hiera, hierarchy, index, base_path, scope, tag, invocation
        )
    return provider


def build_provider(
    hiera,
    hierarchy,
    index,
    scope,
    base_path,
    tag,
    invocation,
    module_name=None,
    generation=0,
):
    """Build one level's provider for ``scope``: interpolate its
    ``options`` (strict mode, no method calls -- ``hiera_config.rb:691``,
    the same call ``_location_resolver`` makes for ``datadir``) and take
    its resolved locations from :func:`resolved_locations_for` (shared,
    referenced-variable-keyed, resolved for the whole hierarchy at
    once -- Puppet's own single ``scope_interpolations_stable?`` check
    per rebuild). Raises, and caches nothing, on a failure in either
    step.
    """
    level = hierarchy[index]
    raw_options = level.options or {}
    options = (
        interpolate(
            raw_options,
            Invocation(scope, _no_option_lookup, lenient=False),
            allow_methods=False,
        )
        if raw_options
        else {}
    )
    locations = resolved_locations_for(
        hiera, hierarchy, index, base_path, scope, tag, invocation
    )
    provider_cls = PROVIDER_CLASSES[level.kind]
    return provider_cls(
        level.name,
        level.backend,
        options,
        locations,
        hiera._environment_context,
        scope.environment,
        load_file=hiera._store.load_file,
        module_name=module_name,
        prune=(
            functools.partial(pruned_module_data, hiera)
            if module_name is not None
            else None
        ),
        revalidate=hiera._revalidate,
        generation=generation,
    )


def pruned_module_data(hiera, module_name, data, function_name, path):
    if path is None:
        return prune_module_data(data, module_name, function_name, path)
    key = (module_name, function_name, path)
    cached = hiera._pruned_cache.get(key)
    # The pruned hash is valid for the very parsed hash it came from: a
    # re-read produces a new object.
    if cached is not None and cached[0] is data:
        return cached[1]
    pruned = prune_module_data(data, module_name, function_name, path)
    hiera._pruned_cache[key] = (data, pruned)
    return pruned


def files_for(hiera, hierarchy, base_path, scope, tag, invocation=None):
    """The ordered list of existing, successfully loaded ``data_hash``
    file paths ``hierarchy`` visits for ``scope`` -- what ``sources()``
    shows.

    Only a ``data_hash`` level's *path* locations are ever loaded here
    (through :meth:`~hyera._lookup.locations._LocationStore.load_file`, so they
    land in its file cache exactly as a real lookup would find them): a
    ``lookup_key``/``data_dig`` function is never called without a real key,
    and a ``uri`` location is never fetched or stat'ed -- ``sources()`` keeps
    its documented meaning, "the files a lookup may read".

    Re-derived on every call, never cached as its own flattened list:
    the expensive part -- resolving/materializing locations, and
    reading each file -- is already cached the referenced-variable/
    ``(path, strict, options)`` way
    (:meth:`~hyera._lookup.locations._LocationStore.location_entry_for`/
    :meth:`~hyera._lookup.locations._LocationStore.load_file`), both shared
    across every view of this instance, so re-walking an already-cached
    level/location list here costs no repeated filesystem access beyond what
    ``revalidate=True`` itself asks for.
    """
    paths = []
    for index, level in enumerate(hierarchy):
        if level.kind != "data_hash":
            continue
        provider = provider_for(
            hiera, tag, base_path, index, hierarchy, scope, invocation
        )
        locations = provider.locations
        if locations is None:
            continue
        for loc in locations:
            if loc.is_uri or not loc.exist:
                continue
            path = loc.location
            hiera._store.load_file(
                path, level.backend, provider.options_for(loc), invocation
            )
            if path in hiera._store._loaded_paths:
                paths.append(path)
    return tuple(paths)
