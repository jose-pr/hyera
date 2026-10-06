# Ported from Puppet 8 lib/puppet/pops/lookup/lookup_adapter.rb,
# interpolation.rb, hiera_config.rb, function_provider.rb,
# data_hash_function_provider.rb, configured_data_provider.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The lookup walk: locations within a level, levels within a hierarchy,
the global, environment and module layers, then lookup_options, the merge
and the dotted sub-key.

Each function takes the :class:`~hyera.core.Hiera` instance as ``self``: the
class binds them as its private methods. Follows the provider stack of
Puppet's ``pops/lookup/lookup_adapter.rb``.
"""

from __future__ import annotations

from .._config.data_provider import (
    environment_for,
    global_only_for,
    module_name_of,
    module_provider_for,
    usable_provider,
)
from .._output.explain import _debug_preamble
from .._output.explain_refs import _ProviderRef, _provider_ref
from ..exceptions import HieraLookupError, _escapes
from .lookup_adapter import convert_result, extract_lookup_options_for_key
from .lookup_options import (
    lookup_default_in_module,
    memoized_options,
    retrieve_lookup_options,
)
from .merge_strategy import MergeStrategy
from .navigation import LOOKUP_OPTIONS, _MISSING, join_key, parse_lookup_key, sub_lookup
from .providers import provider_for

#: Puppet's provider stack (``lookup_adapter.rb:296``): a key is looked up
#: through each layer in turn, merged the same way as levels/locations
#: within a layer. All three layers are wired through
#: :meth:`Hiera._lookup_layers`; ``environment``/``module`` contribute
#: :data:`~hyera._lookup.navigation._MISSING` when no usable config exists there.
_LAYERS = ("global", "environment", "module")


def _lookup_levels(
    self,
    root,
    hierarchy,
    base_path,
    tag,
    scope,
    invocation,
    strategy,
    segments=(),
    module_name=None,
):
    """Puppet's per-location/per-level reduce (``function_provider.rb``
    over a level's locations, ``configured_data_provider.rb:49-61`` over
    the hierarchy's levels) on the bare root key only -- never a dotted
    key. Digging a dotted key's segments out of the result is the
    caller's job (:meth:`_search_and_merge`), done exactly once, after
    this merge completes, never per location or per level (a
    ``data_dig`` provider is the one exception: it receives ``segments``
    directly, since Puppet's own ``data_dig`` functions are handed the
    full key).

    ``hierarchy``/``base_path`` are one layer's own hierarchy and root
    (``self._hierarchy``/``self._base_path`` for the global layer, or a
    :class:`~hyera._config.data_provider._Provider`'s own ``hierarchy``/
    ``root``); ``tag`` names which of that layer's hierarchies (its
    main one, or -- for a module -- its ``default_hierarchy``), for
    provider caching (:meth:`_provider_for`). ``strategy`` is an
    already resolved :class:`~hyera._lookup.merge_strategy.MergeStrategy`.
    Returns the merged root value, or :data:`~hyera._lookup.navigation._MISSING`
    on a miss.

    ``module_name``, when given, is the level's owning module: every
    ``data_hash`` result is read through :func:`~hyera._config.data_provider.
    prune_module_data` first (Puppet's module-data namespace rule),
    cached per ``(module_name, path)`` apart from the store's own file cache's
    unpruned entry -- a file shared with the global layer stays unpruned
    there. A ``lookup_key``/``data_dig`` result is never pruned (Puppet
    prunes only a ``data_hash`` function's return value,
    ``data_hash_function_provider.rb:72``).

    A found value that is ``None`` (data explicitly set to ``~``) is a
    genuine value, not a miss: only an absent root key is.
    """

    def at_level(entry):
        index, level = entry
        provider = provider_for(
            self, tag, base_path, index, hierarchy, scope, invocation, module_name
        )
        if (
            self._is_default_config
            and provider.locations is not None
            and not any(loc.exist for loc in provider.locations)
        ):
            # hiera_config.rb:688: Puppet's own built-in default config
            # drops an entry left with no existing candidate at all --
            # never shown, not even as "Path not found" -- functionally
            # identical to letting it run (every location misses either
            # way), so this only changes what explain() displays.
            return _MISSING
        ref = _ProviderRef('Hierarchy entry "{}"'.format(level.name))
        with invocation.recording("data_provider", ref):
            return provider.key_lookup(root, segments, invocation, strategy)

    variants = list(enumerate(hierarchy))
    if strategy.first_found:
        # The reduce is a plain first-found loop; running it here saves
        # a stack frame per hierarchy level of a nested lookup.
        for variant in variants:
            value = at_level(variant)
            if value is not _MISSING:
                return value
        return _MISSING
    return strategy.lookup(variants, at_level, invocation)


def _lookup_layers(self, root, module_name, invocation, strategy, segments=()):
    """Puppet's provider stack (``lookup_adapter.rb:332-340``): reduce
    ``_LAYERS``, one provider per layer.

    The global layer always runs. The environment layer runs the usable
    provider (if any) of ``invocation.scope.environment``. The module
    layer runs only for a qualified key (``module_name`` set), the
    usable provider (if any) of that module in the same environment.
    A layer with no usable provider contributes
    :data:`~hyera._lookup.navigation._MISSING`, so every layer is always tried
    in order, as Puppet's own multi-variant reduce does.

    ``invocation.global_only`` (already set, inherited from an outer
    nested lookup reached while interpolating a version 3 global
    value) skips the environment and module layers outright for this
    walk too (``lookup_adapter.rb:332-339``) -- a global_only lookup
    never leaves the global layer. Otherwise, when the global layer
    itself is version 3 and no version 5 environment provider exists
    for this scope, the global layer's own walk uses a *derived*
    invocation with ``global_only=True`` (``global_data_provider.
    rb:21-24``), so any ``%{lookup()}``/``%{hiera()}``/``%{alias()}``
    reached while interpolating a value it finds inherits the same
    confinement -- the environment/module walk for *this* key is
    unaffected, since that derived invocation is used only inside the
    global layer's own :meth:`_lookup_levels` call.
    """
    scope = invocation.scope

    def at_layer(layer):
        if layer == "global":
            provider = self._global
            inv = invocation
            if not invocation.global_only and global_only_for(self, scope):
                inv = invocation.derive(invocation._lookup, global_only=True)
        elif invocation.global_only:
            return _MISSING
        elif layer == "environment":
            state = environment_for(self, scope.environment)
            provider = usable_provider(self, state.provider, invocation)
            inv = invocation
        else:
            # `layer` is always one of `_LAYERS` (the only caller,
            # `strategy.lookup(_LAYERS, at_layer, invocation)` below,
            # never passes anything else); "global" and "environment"
            # are already handled above, so reaching here always means
            # "module" -- never a fourth, unhandled name to check for.
            if module_name is None:
                return _MISSING
            state = environment_for(self, scope.environment)
            raw = module_provider_for(self, state, module_name)
            if raw is None:
                if module_name in state.modules():
                    invocation.report_module_provider_not_found(module_name)
                else:
                    invocation.report_module_not_found(module_name)
                return _MISSING
            provider = usable_provider(self, raw, invocation)
            inv = invocation
        if provider is None:
            return _MISSING
        mod = provider.module_name if provider.place == "Module" else None
        ref = _provider_ref(provider)
        with invocation.recording("data_provider", ref):
            if not provider.hierarchy:
                invocation.report_not_found(root)
                return _MISSING
            try:
                result = self._lookup_levels(
                    root,
                    provider.hierarchy,
                    provider.root,
                    "main",
                    scope,
                    inv,
                    strategy,
                    segments,
                    module_name=mod,
                )
            except HieraLookupError as e:
                # lookup_adapter.rb:148-153: only the GLOBAL layer turns
                # a code-less LookupError into one that escapes
                # explain() outright; environment/module data reports
                # the same error as the report's own last line instead.
                # (A BackendError needs no marking here: Hiera.explain()
                # always re-raises it unconditionally, wherever it came
                # from -- a data/infrastructure problem, never one of
                # Puppet's own reportable LookupErrors.)
                if layer == "global" and not getattr(e, "_explain_issue", False):
                    raise _escapes(e)
                raise
            if result is _MISSING and self._is_default_config:
                # The built-in default config's own "Common" entry can
                # be pruned away entirely (above), leaving this layer's
                # own node with no nested location to report a miss at
                # all -- reported directly on it only in that one case.
                invocation.report_not_found(root)
            return result

    if strategy.first_found:
        for layer in _LAYERS:
            value = at_layer(layer)
            if value is not _MISSING:
                return value
        return _MISSING

    def in_layer(layer):
        try:
            return at_layer(layer)
        except HieraLookupError as e:
            e._in_layer = True
            raise

    try:
        return strategy.lookup(_LAYERS, in_layer, invocation)
    except HieraLookupError as e:
        # A merge across layers failing is not an error in any layer's
        # own data: it always escapes explain() instead of ending it.
        if getattr(e, "_in_layer", False):
            raise
        raise _escapes(e)


def _search_and_merge(self, key, invocation, merge, parsed=None):
    """Resolve ``key`` in full: the port of ``LookupAdapter#lookup``
    plus ``do_lookup`` (``lookup_adapter.rb:46-82,332-340``).

    ``lookup_options`` and a ``"lookup_options."``-prefixed key always
    miss without reaching any data (``lookup_adapter.rb:48-52``) -- the
    one place that rule is enforced (:class:`~hyera._lookup.invocation.
    Invocation` no longer duplicates it). Otherwise: ``lookup_options``
    for the key's root is fetched *always*, even when ``merge`` is
    given explicitly -- only the *merge* it names is then skipped,
    never its ``convert_to`` (``lookup_adapter.rb:65-72``). The main
    hierarchy is walked through the provider-layer stack (global, then
    the key's environment, then -- for a qualified key -- its module) on
    the bare root key; a dotted key's segments are dug out of the merged
    root value exactly once (never per location, per level or per
    layer -- except a ``data_dig`` provider, which is handed them
    directly; see :meth:`_lookup_levels`). On a miss -- including a hit
    whose *dig* misses -- and only for a qualified key whose own module
    has a ``default_hierarchy``, :func:`~hyera._lookup.lookup_options.lookup_default_in_module` is
    consulted the same way (``lookup_adapter.rb:73-79``). A final miss
    returns :data:`~hyera._lookup.navigation._MISSING`; a found value has
    ``convert_to`` applied, if the options set one -- from the main
    hierarchy's ``lookup_options`` either way, even when the value came
    from the default hierarchy fallback.

    ``parsed`` lets a caller that already split ``key`` into
    ``(root, segments)`` skip re-parsing it.

    ``key`` is a tuple key path equally: element 0 is the root,
    taken verbatim (never dot-split), the rest are dig segments taken
    verbatim too -- ``parse_lookup_key``/``split_key`` never run for
    one. Every place ``key`` reaches text below (explain/debug
    output, a sub-lookup type-mismatch message, a ``convert_to``
    error) uses ``text_key``, :func:`~hyera._lookup.navigation.join_key`'s
    rendering for a tuple -- the same text the equivalent quoted
    dotted string would produce -- so a tuple path and that string
    report byte-identical messages (``key`` itself unchanged, for the
    ``str`` case that already *is* the message text).
    """
    if isinstance(key, tuple):
        root = key[0]
        if root == LOOKUP_OPTIONS:
            with invocation.recording("invalid_key", LOOKUP_OPTIONS):
                pass
            return _MISSING
        segments = tuple(key[1:])
        text_key = join_key(key)
    else:
        if key == LOOKUP_OPTIONS or key.startswith(LOOKUP_OPTIONS + "."):
            with invocation.recording("invalid_key", LOOKUP_OPTIONS):
                pass
            return _MISSING
        root, segments = parsed if parsed is not None else parse_lookup_key(key)
        text_key = key
    module_name = module_name_of(root)

    def gather_main():
        with invocation.recording("meta", LOOKUP_OPTIONS):
            return retrieve_lookup_options(self, module_name, invocation)

    compiled_options = memoized_options(invocation, "main", module_name, gather_main)
    options = extract_lookup_options_for_key(root, compiled_options) or {}
    explicit_merge = merge is not None
    if not explicit_merge and options.get("merge") is not None:
        invocation.report_merge_source(LOOKUP_OPTIONS)
    strategy = MergeStrategy.strategy(merge if explicit_merge else options.get("merge"))

    with invocation.recording("data", text_key):
        with invocation.check(text_key):
            value = self._lookup_layers(
                root, module_name, invocation, strategy, segments
            )
        if value is not _MISSING and segments:
            value = sub_lookup(text_key, segments, value, invocation)

        if value is _MISSING and not invocation.global_only:
            # A global_only lookup never reaches a module's own
            # default_hierarchy (`lookup_adapter.rb:76`).
            value = lookup_default_in_module(
                self, text_key, root, segments, module_name, invocation
            )
            if value is not _MISSING and segments:
                value = sub_lookup(text_key, segments, value, invocation)

    if value is _MISSING:
        return _MISSING
    convert_to = options.get("convert_to")
    if convert_to is not None:
        value = convert_result(text_key, convert_to, value, invocation)
    return value


def _sub_lookup(self, key, invocation):
    """The host callable behind an :class:`~hyera._lookup.invocation.Invocation`
    (``%{hiera()}``/``%{lookup()}``/``%{alias()}``): a full lookup of
    ``key`` -- its own ``lookup_options``, ``default_hierarchy``
    fallback and ``convert_to`` all apply exactly as a top-level
    lookup -- with ``merge`` always ``None``: the caller's own
    accumulated merge is never carried over into a sub-lookup
    (``interpolation.rb:84``, which always passes a ``nil`` merge).
    The override hash and the default values hash still apply, exactly
    as a top-level lookup of the same key would see them
    (``interpolation.rb:77-86``). Returns
    :data:`~hyera._lookup.navigation._MISSING` on a miss instead of raising.
    """
    # Inline rather than a helper call: each nested hop costs stack
    # frames, and this one is on every ``%{lookup()}`` chain.
    if key in invocation.override_values:
        invocation.emit_debug_info(_debug_preamble((key,)))
        return invocation.override_values[key]
    value = self._search_and_merge(key, invocation, None)
    if value is _MISSING:
        if key in invocation.default_values:
            value = invocation.default_values[key]
        else:
            invocation.emit_debug_info(_debug_preamble((key,)))
            return _MISSING
    invocation.emit_debug_info(_debug_preamble((key,)))
    return value
