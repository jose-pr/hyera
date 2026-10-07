"""Enumeration of the top-level keys a hierarchy can answer, and the dict of
their values: the engine behind :meth:`Hiera.keys <hyera.core.Hiera.keys>` and
:meth:`Hiera.to_dict <hyera.core.Hiera.to_dict>`.

A key is listed when a ``data_hash`` level holds it in a hash a lookup would
read: the hash comes from the same provider, location store and validation a
lookup uses, so revalidation, module-data pruning and the warnings of both are the
lookup's own.
"""

from __future__ import annotations

from .._config.data_provider import (
    environment_for,
    module_provider_for,
    usable_provider,
)
from ..exceptions import KeyNotFoundError
from .invocation import Invocation
from .lookup_function import recursion_bound
from .merge_strategy import MergeStrategy
from .navigation import LOOKUP_OPTIONS
from .providers import provider_for

#: A root no data file can hold: reducing a level with it loads, validates and
#: prunes every location's hash and finds nothing.
_ABSENT_ROOT = object()


def _level_hashes(hiera, hierarchy, base_path, tag, module_name, invocation):
    """The validated hash of every existing location of every ``data_hash``
    level of ``hierarchy``, in search order."""
    first_found = MergeStrategy.strategy(None)
    for index, level in enumerate(hierarchy):
        if level.kind != "data_hash":
            continue
        provider = provider_for(
            hiera,
            tag,
            base_path,
            index,
            hierarchy,
            invocation.scope,
            invocation,
            module_name,
        )
        provider.key_lookup(_ABSENT_ROOT, (), invocation, first_found)
        locations = provider.locations if provider.locations is not None else [None]
        for location in locations:
            if location is not None and not location.exist:
                continue
            data = provider._context(location).data_hash
            if data is not None:
                yield data


def _walks(hiera, invocation):
    """``(hierarchy, root, tag, module_name)`` for every hierarchy a lookup of
    some key can reach, in precedence order: the global layer, the
    environment, each module's own, then each module's ``default_hierarchy``
    (the only layer that accepts one)."""
    yield hiera._global.hierarchy, hiera._global.root, "main", None
    state = environment_for(hiera, invocation.scope.environment)
    provider = usable_provider(hiera, state.provider, invocation)
    if provider is not None:
        yield provider.hierarchy, provider.root, "main", None
    modules = []
    for name in sorted(state.modules()):
        provider = usable_provider(
            hiera, module_provider_for(hiera, state, name), invocation
        )
        if provider is not None:
            modules.append(provider)
    for provider in modules:
        yield provider.hierarchy, provider.root, "main", provider.module_name
    for provider in modules:
        yield provider.default_hierarchy, provider.root, "default", provider.module_name


def keys(hiera):
    """The top-level keys of ``hiera``'s ``data_hash`` levels for its scope, in
    precedence order, each once; ``lookup_options`` is never listed."""
    invocation = Invocation(hiera._scope, hiera._sub_lookup)
    found = {}
    with recursion_bound():
        for hierarchy, root, tag, module_name in _walks(hiera, invocation):
            for data in _level_hashes(
                hiera, hierarchy, root, tag, module_name, invocation
            ):
                for key in data:
                    if isinstance(key, str) and key != LOOKUP_OPTIONS:
                        found[key] = None
    return list(found)


def to_dict(hiera, merge):
    """``{key: hiera.lookup((key,), merge=merge)}`` for every key of
    :func:`keys`, leaving out a key whose lookup misses."""
    result = {}
    for key in keys(hiera):
        try:
            result[key] = hiera.lookup((key,), merge=merge)
        except KeyNotFoundError:
            continue
    return result
