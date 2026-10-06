# Ported from Puppet 8 lib/puppet/pops/lookup/lookup_adapter.rb,
# module_data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""How an instance gathers ``lookup_options`` from its global, environment and
module layers and composes them for a key (``lookup_adapter.rb:236-380``),
and the per-``explain()`` memo that keeps a name-list lookup from searching
a layer twice."""

from __future__ import annotations

from .invocation import Invocation
from .lookup_adapter import (
    compile_patterns,
    extract_lookup_options_for_key,
    validate_lookup_options,
)
from .merge_strategy import MergeStrategy
from .navigation import LOOKUP_OPTIONS, _MISSING
from .._config.data_provider import (
    environment_for,
    module_provider_for,
    usable_provider,
)
from .._output.explain_refs import _provider_ref

#: Sentinel for "no location in this layer's hierarchy declares ``lookup_options``", as opposed
#: to an explicit ``lookup_options: ~`` (``None``); ``_ScopeKeyedCache`` already uses
#: :data:`~hyera._lookup.navigation._MISSING` for "not cached yet" (:func:`layer_options_cached`).
_LO_ABSENT = object()


class _ExplainOptionsMemo:
    """``Hiera.explain()``'s own per-call ``lookup_options`` memo (Design
    decision: a fresh one per ``explain()`` call, discarded when it
    returns, never touching the instance's own caches).

    ``layer_cache`` is what :func:`layer_options_cached`'s own
    ``cache`` override replaces ``self._lookup_options_cache`` with,
    exactly as an ordinary lookup's finer, referenced-variable-keyed
    caching works. ``compiled`` is coarser -- Puppet's own
    ``LookupAdapter`` memoizes a *whole* compiled ``lookup_options``
    mapping per module name (``lookup_adapter.rb:236-247``), which is what
    makes a name-list lookup search the global+environment layer, and each
    module, at most once -- so :meth:`Hiera._search_and_merge`/
    :func:`lookup_default_in_module` only push a ``meta``/nested
    ``lookup_options`` scope node the first time a given ``(tag,
    module_name)`` combination is actually searched during this call.
    """

    __slots__ = ("layer_cache", "compiled")

    def __init__(self, layer_cache) -> None:
        self.layer_cache = layer_cache
        self.compiled: dict = {}


def layer_options_cached(hiera, hierarchy, base_path, tag, module_name, invocation):
    """The raw ``lookup_options`` value gathered from one layer's own
    hierarchy alone (a HASH-strategy gather over its locations/levels,
    never across layers) -- cached the same referenced-variable way as
    :meth:`_LocationStore.location_entry_for`, plus the location entry's own key and,
    with ``revalidate=True``, the current signature of every location it
    materializes to (a rebuilt hierarchy, or any of its files changing,
    invalidates any ``lookup_options`` gathered against the old state)
    -- and never cached at all when gathering it makes a sub-lookup (a
    sub-lookup can reach data outside this entry, such as the default
    hierarchy or another layer, so keying on referenced *variables*
    alone would not be sound). Gathered through the location/level
    nesting only, never the layer stack (``lookup_adapter.rb:241,
    346-380``); callers compose the layers and validate/compile the
    result (:func:`~hyera._lookup.lookup_adapter.validate_lookup_options`/
    ``compile_patterns``).

    ``invocation``, the caller's own top-level one, passed straight
    through (never a ``.derive()`` -- only its ``.scope``/``._fs_memo``
    are ever read here, so deriving one first would cost an allocation
    for nothing), shares its filesystem probe memo with the location
    build, the materialization below, and the gather's own invocation
    (built fresh, around ``hiera._sub_lookup``, only if the gather
    itself actually runs), so this whole gather costs
    at most one real probe per location for the caller's top-level
    lookup.

    Returns :data:`_LO_ABSENT` for "no location in this hierarchy
    declares ``lookup_options`` at all" -- distinct from an explicit
    ``lookup_options: ~`` (``None``), which a caller (Puppet's own
    ``if``/``elsif`` with no ``else``, ``lookup_adapter.rb:358-365``)
    treats differently.

    ``invocation._state`` guards a value inside ``lookup_options``
    that itself runs a full sub-lookup (:meth:`_sub_lookup`) asking this
    same method for its own key's options while this gather is still
    running (a ``merge:`` spec interpolated through a nested
    ``%{lookup(...)}``): marking ``(scope, tag, base_path)``
    pending before the gather starts means that nested lookup sees no
    options at all (:data:`_LO_ABSENT`), instead of re-entering this
    gather and recursing forever (``lookup_adapter.rb:376-378``). The
    guard belongs to one top-level lookup, so another thread's lookup
    never sees it; the marker comes off in ``finally``, so a gather that
    raises does not wedge a later lookup.

    Under ``Hiera.explain()`` (``invocation._lo_cache`` set) that call's
    own memo replaces ``hiera._lookup_options_cache`` outright: explain
    always re-walks and never reuses the instance's real cache. The
    gather's own explain hooks come from ``invocation.explainer`` alone,
    threaded onto the fresh ``gather_invocation`` below.
    """
    memo = invocation._lo_cache
    real_cache = hiera._lookup_options_cache if memo is None else memo.layer_cache
    scope = invocation.scope
    store = hiera._store
    entry = store.location_entry_for(hierarchy, base_path, scope, tag, invocation)
    materialized = store.materialize(entry, invocation)
    kind = ("lookup_options", tag, base_path, id(hierarchy))
    if hiera._revalidate:
        versions = tuple(
            (
                loc.location,
                invocation._memo_probe(loc.location).sig,
            )
            for locations in materialized
            if locations is not None
            for loc in locations
            if not loc.is_uri and loc.exist
        )
    else:
        versions = ()
    extra = (entry.key, versions)
    cached = real_cache.get(kind, scope, extra)
    if cached is not _MISSING:
        return cached

    guard = invocation._state
    pending_key = (scope, tag, base_path, id(hierarchy))
    if pending_key in guard.pending:
        guard.hits += 1
        return _LO_ABSENT
    guard.pending.add(pending_key)
    try:
        lo_refs = []
        made_sub_lookup = False

        def counting_lookup(key, inv):
            nonlocal made_sub_lookup
            made_sub_lookup = True
            return hiera._sub_lookup(key, inv)

        fs_memo = invocation._fs_memo if invocation is not None else None
        gather_invocation = Invocation(
            scope,
            counting_lookup,
            scope_interpolations=lo_refs,
            explainer=invocation.explainer,
            _fs_memo=fs_memo,
            _lo_cache=invocation._lo_cache,
            _state=guard,
        )
        with gather_invocation.check(LOOKUP_OPTIONS):
            raw = hiera._lookup_levels(
                LOOKUP_OPTIONS,
                hierarchy,
                base_path,
                tag,
                scope,
                gather_invocation,
                MergeStrategy.strategy("hash"),
                module_name=module_name,
            )
    finally:
        guard.pending.discard(pending_key)

    result = _LO_ABSENT if raw is _MISSING else raw
    if not made_sub_lookup:
        put_key = real_cache.key_for(kind, lo_refs, extra)
        real_cache.put(put_key, result)
    return result


def global_lookup_options(hiera, invocation):
    """The global layer's own validated ``lookup_options``, or ``None``.

    Passes ``invocation`` straight through, never a ``.derive()`` of it:
    :func:`layer_options_cached` only ever reads its own ``.scope``/
    ``._fs_memo`` from what it is handed here (the gather itself builds
    its own fresh ``Invocation`` around ``hiera._sub_lookup``), so
    deriving one first would only add an allocation this hot path (every
    lookup runs it) does not need.
    """
    with invocation.recording("data_provider", _provider_ref(hiera._global)):
        raw = layer_options_cached(
            hiera,
            hiera._global.hierarchy,
            hiera._global.root,
            "main",
            None,
            invocation,
        )
    return validate_lookup_options(None if raw is _LO_ABSENT else raw, None)


def environment_lookup_options(hiera, state, invocation):
    """The global and environment layers' ``lookup_options`` HASH-merged
    (global wins), or ``None`` (``lookup_adapter.rb:375-380``). See
    :func:`global_lookup_options` for why ``invocation`` is passed
    through unchanged rather than derived.
    """
    g = global_lookup_options(hiera, invocation)
    provider = usable_provider(hiera, state.provider, invocation)
    e = None
    if provider is not None:
        with invocation.recording("data_provider", _provider_ref(provider)):
            raw = layer_options_cached(
                hiera, provider.hierarchy, provider.root, "main", None, invocation
            )
        e = validate_lookup_options(None if raw is _LO_ABSENT else raw, None)
    if g is None:
        return e
    if e is None:
        return g
    return MergeStrategy.strategy("hash").merge(g, e)


def retrieve_lookup_options(hiera, module_name, invocation):
    """The compiled ``lookup_options`` mapping for ``module_name`` (or
    just the global/environment options, when ``module_name`` is
    ``None``), a port of ``lookup_adapter.rb:346-372``.

    A module's own options are qualified against its name
    (:func:`~hyera._lookup.lookup_adapter.validate_lookup_options`) and
    gathered from its pruned data (which keeps ``lookup_options``),
    never merged with the global/environment options wholesale --
    module wins per key, through the same HASH strategy, but only when
    the module actually declares a real (non-``None``) mapping; a
    module walk that finds nothing at all (:data:`_LO_ABSENT`) leaves
    the environment options untouched, while one that finds an explicit
    ``lookup_options: ~`` discards them (Puppet's own ``if``/``elsif``
    with no ``else``, ``lookup_adapter.rb:358-365``).

    With ``revalidate=False``, the final composed-and-compiled result is
    itself memoized in ``hiera._compiled_options_cache`` by an identity
    check on ``scope`` (see its own comment): a repeat call for the
    exact same scope and ``module_name`` skips recomposing the layers
    and recompiling every ``^``-prefixed pattern's regex, both of which
    this method would otherwise redo on every single lookup even though
    none of the underlying per-layer gathers changed. With
    ``revalidate=True`` this fast path is skipped entirely: the
    per-layer gathers below revalidate against an on-disk change
    (``_layer_options_cached``'s own ``versions`` key), and this method
    composing/compiling their result on every call is what lets that
    revalidation actually reach a caller -- short-circuiting here on
    scope identity alone, the same as ``revalidate=False`` safely does,
    would silently ignore a changed ``lookup_options`` value for as
    long as the same scope object keeps being used.

    Under ``Hiera.explain()`` (``invocation._lo_cache`` set; see
    :func:`layer_options_cached`) both the ``_compiled_options_cache``
    read and its write are skipped outright, so a call under explain
    always re-walks and re-explains every layer, even right after an
    ordinary lookup already cached everything.
    """
    scope = invocation.scope
    explaining = invocation._lo_cache is not None
    if not hiera._revalidate and not explaining:
        cached = hiera._compiled_options_cache.get(module_name)
        if cached is not None and cached[0] is scope:
            return cached[1]
    guard_hits = invocation._state.hits

    state = environment_for(hiera, scope.environment)
    opts = memoized_options(
        invocation,
        "environment",
        None,
        lambda: environment_lookup_options(hiera, state, invocation),
    )
    if module_name is not None:
        raw_provider = module_provider_for(hiera, state, module_name)
        if raw_provider is None:
            if module_name in state.modules():
                invocation.report_module_provider_not_found(module_name)
            else:
                invocation.report_module_not_found(module_name)
        mprovider = usable_provider(hiera, raw_provider, invocation)
        if mprovider is not None:
            with invocation.recording("data_provider", _provider_ref(mprovider)):
                raw = layer_options_cached(
                    hiera,
                    mprovider.hierarchy,
                    mprovider.root,
                    "main",
                    module_name,
                    invocation,
                )
            if raw is not _LO_ABSENT:
                m = validate_lookup_options(raw, module_name)
                if opts is None:
                    opts = m
                elif m is not None:
                    opts = _merge_options_report(opts, m, module_name, invocation)
                else:
                    opts = None
    compiled = compile_patterns(opts)
    # A result composed while the re-entrancy guard refused a layer lacks
    # that layer's options: it answers this lookup only.
    if (
        not hiera._revalidate
        and not explaining
        and invocation._state.hits == guard_hits
    ):
        hiera._compiled_options_cache[module_name] = (scope, compiled)
    return compiled


def _merge_options_report(env_opts, module_opts, module_name, invocation):
    """The global/environment options merged with a module's own
    (``lookup_adapter.rb:358-364``): reported as a HASH merge over a
    ``Global and Environment`` scope and a ``Module NAME`` scope."""
    names = ("Global and Environment", "Module {}".format(module_name))

    def found(name):
        with invocation.recording("scope", name):
            return invocation.report_found(
                LOOKUP_OPTIONS, env_opts if name == names[0] else module_opts
            )

    return MergeStrategy.strategy("hash").lookup(names, found, invocation)


def module_default_lookup_options(hiera, provider, invocation):
    """The compiled ``lookup_options`` mapping gathered from
    ``provider``'s own ``default_hierarchy`` data only -- never merged
    with the global/environment/module options
    (:func:`retrieve_lookup_options`), as Puppet's
    ``module_data_provider.rb:26-40``'s ``key_lookup_in_default``
    never touches the main options.
    """
    with invocation.recording("data_provider", _provider_ref(provider)):
        raw = layer_options_cached(
            hiera,
            provider.default_hierarchy,
            provider.root,
            "default",
            provider.module_name,
            invocation,
        )
    opts = validate_lookup_options(
        None if raw is _LO_ABSENT else raw, provider.module_name
    )
    return compile_patterns(opts)


def memoized_options(invocation, tag, module_name, gather):
    """Run ``gather()`` (a zero-argument callable that both performs
    and explain-records one ``lookup_options`` search) once per
    ``(tag, module_name)`` for the lifetime of one ``explain()`` call --
    Puppet's own ``LookupAdapter`` memoizes a whole compiled mapping per
    module name (``lookup_adapter.rb:236-247``), which is what keeps a
    name-list lookup from searching, and re-recording, the same section
    once per name. A no-op passthrough for an ordinary lookup
    (``invocation._lo_cache`` is ``None`` there).
    """
    memo = invocation._lo_cache
    if memo is None:
        return gather()
    key = (tag, module_name)
    if key in memo.compiled:
        return memo.compiled[key]
    result = gather()
    memo.compiled[key] = result
    return result


def lookup_default_in_module(hiera, key, root, segments, module_name, invocation):
    """Puppet's ``lookup_default_in_module``
    (``module_data_provider.rb:26-40``, ``lookup_adapter.rb:180-217``):
    a module's own ``default_hierarchy``, consulted only after the main
    stack (and its dig) misses.

    :data:`~hyera._lookup.navigation._MISSING` when ``module_name`` is
    ``None`` (an unqualified key never reaches a module's default
    hierarchy either), the module has no usable provider, or its
    ``default_hierarchy`` is empty. The merge strategy comes only from
    the default hierarchy's own ``lookup_options``
    (:func:`module_default_lookup_options`) -- never the caller's
    ``merge=`` or the main hierarchy's options, which
    :meth:`Hiera._search_and_merge <hyera.core.Hiera._search_and_merge>` still applies its ``convert_to`` from,
    regardless of which walk actually found the value.
    """
    if module_name is None:
        return _MISSING
    state = environment_for(hiera, invocation.scope.environment)
    provider = usable_provider(
        hiera, module_provider_for(hiera, state, module_name), invocation
    )
    if provider is None or not provider.default_hierarchy:
        return _MISSING
    with invocation.recording(
        "scope", 'Searching default_hierarchy of module "{}"'.format(module_name)
    ):

        def gather_default():
            with invocation.recording("scope", 'Searching for "lookup_options"'):
                return module_default_lookup_options(hiera, provider, invocation)

        compiled = memoized_options(invocation, "default", module_name, gather_default)
        entry = extract_lookup_options_for_key(root, compiled) or {}
        strategy = MergeStrategy.strategy(entry.get("merge"))
        with invocation.recording("scope", 'Searching for "{}"'.format(key)):
            with invocation.recording("data_provider", _provider_ref(provider)):
                with invocation.check(key):
                    result = hiera._lookup_levels(
                        root,
                        provider.default_hierarchy,
                        provider.root,
                        "default",
                        invocation.scope,
                        invocation,
                        strategy,
                        segments,
                        module_name=module_name,
                    )
            return result
