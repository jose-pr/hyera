# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Building HieraLevel objects from a validated config's hierarchy entries.

Ports the function-kind resolution and the provider construction of Puppet's
``pops/lookup/hiera_config.rb``.
"""

from __future__ import annotations

from ..backends import Backend
from ..exceptions import ConfigError
from .config_source import _ConfigSource, _config_error
from .config_v5 import (
    _ALL_FUNCTION_KEYS,
    _FUNCTION_KEYS,
    _LOCATION_KEYS,
    _config_line,
)
from .hiera_config import HieraLevel


def _v3_backend_class(name: str, source: "_ConfigSource", line=None):
    """Resolve a Hiera 3 backend name (a v3 ``backends:`` entry, or a v5
    ``hiera3_backend:``) against the registry's ``v3`` namespace: empty for
    every built-in -- ``yaml``/``json``/``hocon``/``eyaml`` map to the v5
    ``*_data``/``eyaml_lookup_key`` functions inside :func:`_v3_level_specs`
    itself and never reach here. Only a third-party backend registered
    under this name resolves; anything else raises, since a Ruby Hiera 3
    backend cannot run here (recorded as the ``v3-ruby-backend-unavailable``
    deviation in the conformance suite).
    """
    cls = Backend.find(name, kind="v3")
    if cls is None:
        raise _config_error(
            source,
            "Hiera 3 backend '{}' is not available: Ruby Hiera 3 backends "
            "cannot run here, and no Backend is registered under the v3 "
            "name '{}'".format(name, name),
            line=line,
        )
    if not cls.implements("data_hash"):
        raise _config_error(
            source,
            "Hiera 3 backend '{}' ({}) must implement data_hash".format(
                name, cls.__name__
            ),
            line=line,
        )
    return cls


def _function_of(entry: dict, defaults: dict):
    """Puppet's function-kind resolution (``hiera_config.rb:656-662``):
    returns ``(kind, name)``. The entry's own function key wins; ``defaults``
    is consulted only when the entry has none, and then only for
    ``_FUNCTION_KEYS`` (``defaults`` never carries ``v4_data_hash``).
    Unreachable with a ``None`` result once :func:`_validate_v5` has run
    (it guarantees exactly one function key, on the entry or in
    ``defaults``); kept total rather than assuming that here too.

    The ``defaults`` loop's own ``hiera3_backend`` case can never match:
    ``defaults`` only ever reaches here after ``_check_defaults_type``
    rejected any key outside ``_DEFAULTS_KEYS``, which excludes
    ``hiera3_backend`` -- the same restriction Puppet's own ``defaults``
    struct type places on it (``hiera_config.rb``'s ``@@CONFIG_TYPE``;
    conformance case ``config-defaults-hiera3-backend-key``). Puppet's own
    ``function_kind = FUNCTION_KEYS.find { |key| defaults.include?(key) }``
    has the identical shape, so this stays a literal port rather than a
    narrower, hand-trimmed key list.
    """
    for key in _ALL_FUNCTION_KEYS:
        if key in entry:
            return key, entry[key]
    for key in _FUNCTION_KEYS:
        if key in defaults:
            return key, defaults[key]
    return None, None


def _build_hierarchies(base, backends, source: "_ConfigSource", *, scope=None):
    """Build ``hierarchy`` and ``default_hierarchy`` from base config.

    Returns ``(hierarchy_levels, default_hierarchy_levels)``. Assumes
    :func:`_config_version`, :func:`_fill_v5_defaults` and
    :func:`_validate_v5` already ran, so ``defaults``/``hierarchy`` are
    present and every entry has exactly one function key (its own, or one
    from ``defaults``), at most one location key, and well-typed values.
    ``scope`` is only consulted for a ``hiera3_backend`` entry's own
    backend construction (its ``strict``); a caller that can never reach
    one (a version-3/missing-version layer, already rejected by
    :func:`_validate_v5`'s global-only rule before this runs) may omit it.
    """
    hierarchy = base.get("hierarchy")
    defaults = base.get("defaults") or {}

    backend_levels = _build_levels(
        hierarchy, defaults, backends, source, scope=scope, area="hierarchy"
    )
    default_levels = _build_levels(
        base.get("default_hierarchy") or [],
        defaults,
        backends,
        source,
        scope=scope,
        area="default_hierarchy",
    )

    return backend_levels, default_levels


def _function_name(name: str) -> str:
    """A function name as Puppet's loader resolves it: a leading ``::`` is
    dropped and case does not matter (registered names are lower case)."""
    if name.startswith("::"):
        name = name[2:]
    return name if Backend.find(name, "function") is not None else name.lower()


class _UnknownFunction(Backend):
    """The stand-in backend of a level whose function name is unknown or not
    allowed: it implements nothing and carries the error the level raises
    when a lookup first calls it."""

    def __init__(self, error: ConfigError) -> None:
        super().__init__()
        self.name = None
        self.unknown_function_error = error


def _unknown_function_error(source, kind, function, backends) -> ConfigError:
    if kind == "data_hash":
        allowed_names = [
            n
            for n in Backend.names("function")
            if Backend.find(n, "function") in backends
        ]
        return _config_error(
            source,
            "Unable to find 'data_hash' function named '{}'; "
            "known: {}".format(function, ", ".join(allowed_names)),
        )
    return _config_error(
        source, "Unable to find '{}' function named '{}'".format(kind, function)
    )


def _build_level(
    conf: dict,
    kind: str,
    function: str,
    backends,
    source: "_ConfigSource",
    scope,
    *,
    area: str = "hierarchy",
    index: int = 0,
    extension=None,
    datadir_base=None,
    datadir_literal=False,
    backend_cls=None,
    lenient_locations=True,
) -> HieraLevel:
    """Resolve one hierarchy entry's backend and build its
    :class:`HieraLevel`.

    With ``backend_cls`` given (a v3 backend, resolved by the caller
    against the ``v3`` registry namespace -- :func:`_v3_backend_class`),
    ``function`` is that v3 name and the function-namespace lookup below is
    skipped entirely. Otherwise this is the v5 ``data_hash``/``lookup_key``/
    ``data_dig`` path: ``backends`` is an allow-list of
    :class:`~hyera.backends.Backend` subclasses -- ``function`` is resolved
    against the process-global registry (:meth:`Backend.find`, the one
    namespace all three share), then checked against this allow-list, so a
    name registered by a third party but not passed to
    ``Hiera(backends=...)`` is refused exactly like an unknown one.
    """
    name = conf.get("name")
    if backend_cls is not None:
        backend = Backend.new(
            function, conf, kind="v3", strict=getattr(scope, "strict", None)
        )
    elif kind in ("data_hash", "lookup_key", "data_dig"):
        # Puppet resolves the function only when it calls it for an existing location, so a
        # level naming an unknown function is built anyway and raises on its first such call
        # (see `_lookup.function_provider`); every other level keeps answering.
        function = _function_name(function)
        resolved_cls = Backend.find(function, kind="function")
        if resolved_cls is None or resolved_cls not in backends:
            backend = _UnknownFunction(
                _unknown_function_error(source, kind, function, backends)
            )
        else:
            conf = dict(conf)
            conf[kind] = function
            backend = Backend.new(function, conf, kind="function")
    else:
        # Unreachable via the v5 path: _validate_v5 guarantees a function
        # key exists. _build_levels handles hiera3_backend/v4_data_hash
        # itself before ever calling this.
        raise _config_error(
            source, "Hierarchy level {!r} is missing a function key".format(name)
        )
    return HieraLevel.new(
        conf,
        backend,
        kind,
        extension=extension,
        datadir_base=datadir_base,
        datadir_literal=datadir_literal,
        lenient_locations=lenient_locations,
    )


def _build_levels(
    hierarchy,
    defaults,
    backends,
    source: "_ConfigSource",
    *,
    scope=None,
    area: str = "hierarchy",
):
    """Build HieraLevel instances from hierarchy configuration.

    Each entry's conf is built explicitly -- ``name``, its one location
    key (if any), ``datadir``, ``options`` (the entry's own, else
    ``defaults``'s, never merged -- ``hiera_config.rb:690``)
    and, for a ``data_hash`` level, ``data_hash: <name>`` -- rather than
    merging every ``defaults`` key in wholesale.
    """
    levels: "list[HieraLevel]" = []
    for i, level in enumerate(hierarchy):
        name = level.get("name")
        kind, func_name = _function_of(level, defaults)
        datadir = level.get("datadir") or defaults.get("datadir") or "data"
        options = level.get("options", defaults.get("options"))

        conf = {"name": name, "datadir": datadir}
        for loc_key in _LOCATION_KEYS:
            if loc_key in level:
                conf[loc_key] = level[loc_key]
        if options is not None:
            conf["options"] = options

        if kind == "hiera3_backend":
            # Global-only (_validate_v5 rejects it elsewhere): replaces a v5 data_hash function with
            # a registered v3-namespace backend, with the same "append unless already present"
            # extension a v3 entry gets (hiera_config.rb:692-714, hiera/backend.rb:57-58).
            line = _config_line(source.text, (area, i, "hiera3_backend"), key=True)
            backend_cls = _v3_backend_class(func_name, source, line)
            levels.append(
                _build_level(
                    conf,
                    "data_hash",
                    func_name,
                    backends,
                    source,
                    scope,
                    area=area,
                    index=i,
                    extension="." + func_name,
                    backend_cls=backend_cls,
                )
            )
            continue
        if kind == "v4_data_hash":
            raise _config_error(
                source,
                "Unable to find 'v4_data_hash' function named '{}'".format(func_name),
            ) from None

        levels.append(
            _build_level(
                conf, kind, func_name, backends, source, scope, area=area, index=i
            )
        )
    return levels
