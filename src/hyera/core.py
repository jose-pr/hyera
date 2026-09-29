# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/pops/lookup/data_hash_function_provider.rb,
# data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging

from ._hiera_config import (
    HieraLevel,
    _build_hierarchies,
    _fill_v5_defaults,
    _read_base_config,
    _select_version,
    _validate_v5,
)
from ._interpolation import Interpolation, _format_source, _normalize_source
from ._location_resolver import _resolve_level_paths
from ._lookup_adapter import _extract_lookup_options_for_key, convert_result
from ._merge_strategy import MergeStrategy
from ._navigation import _MISSING, parse_lookup_key, sub_lookup
from ._scope import Scope
from .backends import default_backends

__all__ = ["Hiera", "ScopedHiera"]

_LOGGER = logging.getLogger(__name__)

#: Puppet's provider stack (``lookup_adapter.rb:296``): a key is looked up
#: through each layer in turn, merged the same way as levels/locations
#: within a layer. Only ``"global"`` is populated until
#: ``config_layers_global_env_module`` fills ``environment``/``module``.
_LAYERS = ("global", "environment", "module")


def _puppet_type_label(value) -> str:
    """The Puppet type name ``data_provider.rb``'s Hash check would report
    for a non-Hash ``data_hash`` result (measured against Puppet 8.10.0,
    ``--strict warning``, on JSON's seven possible top-level shapes; the
    same labels apply to any backend's non-dict result)."""
    if isinstance(value, bool):  # bool before int: bool is an int subclass.
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


def _validate_data_hash(data, name, path) -> None:
    """Puppet's Hash check on a ``data_hash`` result
    (``data_hash_function_provider.rb:56-76`` + ``data_provider.rb:76-91``),
    applied here so every backend -- third-party ones included -- gets it."""
    if isinstance(data, dict):
        return
    raise BackendError(
        "Value returned from data_hash function '{}', when using location "
        "'{}', has wrong type, expects a Hash value, got {}".format(
            name, path, _puppet_type_label(data)
        ),
        path=str(path),
    )


class ScopedHiera:
    """A ``Hiera`` with a bound (derived) :class:`~hyera.Scope`.

    Every method has ``Hiera``'s own signature and uses this scope instead
    of ``hiera.scope``. Unknown attributes proxy to the wrapped ``Hiera``.
    """

    def __init__(self, hiera, scope: Scope):
        self.hiera = hiera
        self.scope = scope

    def get(self, key: str, default=None, merge=None, throw=False):
        return self.hiera._get(key, default, merge, throw, self.scope)

    def has(self, key: str) -> bool:
        return self.hiera._has(key, self.scope)

    def sources(self):
        return self.hiera._sources(self.scope)

    def format(self, text: str) -> str:
        return self.hiera._format(text, self.scope)

    def scoped(
        self,
        *,
        variables=None,
        facts=None,
        trusted=None,
        server_facts=None,
        environment=None,
        strict=None,
        node_name=None,
    ) -> "ScopedHiera":
        return ScopedHiera(
            self.hiera,
            self.scope.derive(
                variables=variables,
                facts=facts,
                trusted=trusted,
                server_facts=server_facts,
                environment=environment,
                strict=strict,
                node_name=node_name,
            ),
        )

    def __getattr__(self, name):
        # Copying/pickling rebuilds the instance without __init__ and then
        # probes it for state/dunder methods; without this guard that probe
        # reaches ``self.hiera`` -- itself an attribute lookup on the same
        # not-yet-initialized instance -- recursing until the stack
        # overflows. ``hiera``/``scope`` and any dunder name are never
        # proxied.
        if name in ("hiera", "scope") or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.hiera, name)


class Hiera(Interpolation):
    """A first-class Python interface to Hiera data.

    It takes a base hiera config (YAML file path, file-like object, or dict)
    and exposes methods to retrieve and fully resolve hiera values.

    :param base_config: hiera base configuration: file path, file-like object,
        or a pre-parsed ``dict``.
    :param backends: an allow-list of :class:`~hyera.backends.Backend`
        classes; defaults to :func:`default_backends` — every backend
        registered in the ``function`` namespace (``YAMLBackend``,
        ``JSONBackend``, ``HOCONBackend``, ``SopsBackend``).
    :param base_path: root that relative data dirs/paths resolve against.
    :param scope: the bound :class:`~hyera.Scope` for this instance's
        lifetime (facts, trusted data, variables, ``strict``). Defaults to
        ``Scope()`` (Puppet's defaults: no facts, environment
        ``"production"``, the local trusted hash). Anything other than a
        ``Scope`` (or ``None``) raises ``TypeError``.
    """

    def __init__(
        self, base_config, backends=None, base_path=None, *, scope: Scope = None
    ):
        self.base_config = base_config
        if scope is None:
            scope = Scope()
        elif not isinstance(scope, Scope):
            raise TypeError("scope must be a hyera.Scope")
        self.scope = scope

        self.hierarchy: "list[HieraLevel]" = []
        self.default_hierarchy: "list[HieraLevel]" = []
        self.cache: dict = {}
        #: Per-scope cache of resolved source path lists (see ``sources``).
        self._source_cache: dict = {}
        #: Per-scope cache of the merged ``lookup_options`` mapping.
        self._lookup_options_cache: dict = {}

        self._load_config(
            default_backends() if backends is None else backends, base_path
        )

    def format(self, text: str) -> str:
        """Resolve ``%{var}`` references in ``text`` against this instance's
        bound scope."""
        return self._format(text, self.scope)

    def _format(self, text: str, scope: Scope) -> str:
        return _format_source(_normalize_source(text), scope)

    def _load_config(self, backends, base_path=None):
        """Load and validate the base configuration, building hierarchy state.

        Raises :class:`ConfigError` on any invalid/missing configuration.
        """
        #: Allow-list of backend classes a hierarchy level's ``data_hash``
        #: may resolve to (the Backend registry, looked up by name in
        #: ``_hiera_config._build_levels``).
        self.backends: "list[type]" = list(backends)

        source, self.base = _read_base_config(self.base_config, base_path)
        self.base_path = source.root
        _select_version(self.base, source)
        _fill_v5_defaults(self.base)
        _validate_v5(self.base, source)

        if not self.backends:
            raise ConfigError("No backends could be loaded")

        try:
            self.hierarchy, self.default_hierarchy = _build_hierarchies(
                self.base, self.backends, source
            )
        except HieraError as e:  # keep the class and text, add the file
            e.path = e.path or source.path
            raise
        except Exception as e:
            raise ConfigError(
                "The Lookup Configuration at '{}' is invalid: {}: {}".format(
                    source.label, type(e).__name__, _one_line(e)
                ),
                path=source.path,
            ) from e

        # Pre-load/cache the bound scope's own data.
        self._prewarm()

    def _prewarm(self) -> None:
        """Load and cache the source files for the bound scope up front,
        same as ``sources()`` would.

        Mirrors the source-resolution side effects of a ``get(None)`` call
        without going through the public API's key-type check.

        A malformed dotted reference or a navigation type mismatch
        reachable while resolving a hierarchy path (``%{...}`` in a
        ``path``/``paths``/``glob``/``mapped_paths`` template) is swallowed
        here and logged at debug level, not raised out of the constructor:
        Puppet raises these at lookup time, never at construction, and a
        constructor should fail only for configuration errors. Swallowing it
        here also means the walk this aborts was never cached (``_files_for``
        only caches a *completed* walk), so the first real lookup retries it
        in full and raises the same error again, now at the right time.
        """
        try:
            self._sources(self.scope)
            if self.default_hierarchy:
                self._default_levels(self.scope)
        except (HieraLookupError, InterpolationError) as e:
            _LOGGER.debug("Pre-warm skipped after a lookup-time error: %s", e)

    def _load_file(self, path, backend):
        """Load ``path`` via ``backend.data_hash(...)``, caching the result.

        A read failure (``OSError``, e.g. the file vanished between the
        directory walk and here) becomes ``Unable to read (<path>): ...``; a
        parse failure (a :class:`BackendError` without ``.path`` set --
        ``Backend.load`` already sets it) becomes ``Unable to parse
        (<path>): ...``; any other non-:class:`HieraError` exception is
        wrapped the same way, naming its type. An already-pathed
        ``BackendError`` (or any other :class:`HieraError`) propagates
        unchanged.

        Puppet's own Hash check on the result
        (``data_hash_function_provider.rb:70-76``) runs here too, so every
        backend -- third-party ones included -- gets it.
        """
        if path not in self.cache:
            try:
                data = backend.data_hash(path, dict(backend.conf.get("options") or {}))
            except BackendError as e:
                if e.path is None:
                    raise BackendError(
                        "Unable to parse ({}): {}".format(path, e),
                        path=str(path),
                    ) from e
                raise
            except HieraError:
                raise
            except OSError as e:
                raise BackendError(
                    "Unable to read ({}): {}".format(path, e.strerror or e),
                    path=str(path),
                ) from e
            except Exception as e:
                raise BackendError(
                    "Unable to parse ({}): {}: {}".format(path, type(e).__name__, e),
                    path=str(path),
                ) from e

            _validate_data_hash(data, backend.name, path)
            self.cache[path] = data
        return path

    def _lookup_levels(self, key, levels, scope, strategy):
        """Puppet's per-location/per-level reduce (``data_hash_function_
        provider.rb:26-33`` over a level's locations,
        ``configured_data_provider.rb:49-61`` over the hierarchy's levels).

        ``levels`` is a tuple with one tuple of location entries per
        hierarchy level (:meth:`_levels_for`); ``strategy`` is an already
        resolved :class:`~hyera._merge_strategy.MergeStrategy`. Returns the
        merged value, or :data:`~hyera._navigation._MISSING` on a miss.

        ``key`` is parsed once, up front, into its root and Puppet sub-key
        segments (``_navigation.parse_lookup_key``); each location is then
        looked up by the plain root and, if there are segments, walked with
        ``sub_lookup``. A ``None`` result -- whether the root itself is
        absent/null or ``sub_lookup`` misses -- is a miss; a type-mismatch or
        malformed-key error from either helper propagates, never swallowed
        into a location/level skip (only a miss should be, per Puppet's
        ``lookup()``, which raises both even with a default value set).
        """
        root, segments = (None, ()) if key is None else parse_lookup_key(key)

        def at_location(entry):
            data = self.cache.get(entry, _MISSING)
            if data is _MISSING or not isinstance(data, dict) or root not in data:
                return _MISSING
            value = sub_lookup(key, segments, data[root]) if segments else data[root]
            if value is _MISSING or value is None:
                return _MISSING
            # A sub-lookup (``%{hiera()}``/``%{lookup()}``/``%{alias()}``)
            # never inherits the caller's merge strategy -- accumulation
            # happens exactly once, at the top level (``interpolation.rb:84``
            # passes merge ``nil``); ``levels`` is threaded through unchanged
            # so a sub-lookup still searches the full hierarchy.
            return self._resolve(value, levels, scope, None)

        def at_level(locations):
            return strategy.lookup(locations, at_location)

        return strategy.lookup(levels, at_level)

    def _lookup_layers(self, key, levels, scope, strategy):
        """Puppet's provider stack (``lookup_adapter.rb:332-340``): reduce
        ``_LAYERS`` the same way, with only ``"global"`` populated."""

        def at_layer(layer):
            if layer != "global":
                return _MISSING
            return self._lookup_levels(key, levels, scope, strategy)

        return strategy.lookup(_LAYERS, at_layer)

    def _get_key(self, key, levels, scope, merge):
        """Get the value of ``key``, resolving it against ``levels``.

        ``merge`` is a raw ``merge=`` spec (name, options hash, a
        :class:`~hyera._merge_strategy.MergeStrategy` instance, or ``None``
        for first-match), normalized here via
        :meth:`~hyera._merge_strategy.MergeStrategy.strategy`. Used for a
        function-call sub-lookup (``%{hiera()}``/``%{lookup()}``/
        ``%{alias()}``, always with ``merge=None``) and for merging
        ``lookup_options`` (``merge="hash"``, without the layer stack) --
        never for the main lookup, which goes through :meth:`_lookup_layers`
        directly (see ``.get``/``._get``).
        """
        strategy = MergeStrategy.strategy(merge)
        value = self._lookup_levels(key, levels, scope, strategy)
        if value is _MISSING:
            if key is not None and parse_lookup_key(key)[1]:
                _LOGGER.debug(
                    "Lookup key '%s' not found; ensure it is provided in the "
                    "hiera data.",
                    key,
                )
            raise KeyError(key)
        return value

    def scoped(
        self,
        *,
        variables=None,
        facts=None,
        trusted=None,
        server_facts=None,
        environment=None,
        strict=None,
        node_name=None,
    ) -> ScopedHiera:
        """A :class:`ScopedHiera` bound to ``self.scope.derive(...)``."""
        return ScopedHiera(
            self,
            self.scope.derive(
                variables=variables,
                facts=facts,
                trusted=trusted,
                server_facts=server_facts,
                environment=environment,
                strict=strict,
                node_name=node_name,
            ),
        )

    def has(self, key: str) -> bool:
        """Return True if ``key`` exists in hiera, False otherwise."""
        return self._has(key, self.scope)

    def _has(self, key, scope) -> bool:
        try:
            self._get(key, None, None, True, scope)
            return True
        except KeyNotFoundError:
            return False

    def sources(self):
        """Resolve the ordered list of source paths for this instance's
        bound scope.

        Existing files are parsed and cached and their cache-key paths
        returned.

        The filesystem walk (glob/iterdir/stat) is cached per scope value so
        a merge lookup across many keys does not re-walk the tree for each
        key. This shares the staleness assumption of the parsed-content
        cache: a single instance reflects the tree as first seen for a
        given scope.
        """
        return self._sources(self.scope)

    def _sources(self, scope):
        return self._files_for(self.hierarchy, scope, "main")

    def _levels_for(self, hierarchy, scope, tag):
        """Every location each hierarchy level visits, one tuple per level.

        A location entry is a loaded cache-key path (an existing file,
        already read through :meth:`_load_file`) or the plain
        (unloaded/non-existent) candidate path itself -- a "missing"
        location, still a location Puppet's own strategy reduce sees (see
        ``at_location`` in :meth:`_lookup_levels`, which maps anything not
        in ``self.cache`` to a miss). A directory location expands to its
        file children (non-file children are dropped outright, never
        represented at all, matching the old flattened-files behavior); a
        glob location's matches always exist.

        Cached per scope value (the filesystem walk -- glob/iterdir/stat --
        is what's expensive, not the reduce over the result), sharing
        ``_source_cache``/the same instance-lifetime staleness contract as
        the old flattened list. A Scope value always hashes (it is
        immutable by construction), so there is no "unhashable context
        value" fallback to skip caching.
        """
        cache_key = (tag, scope)
        cached = self._source_cache.get(cache_key)
        if cached is not None:
            return cached

        levels = []
        for level in hierarchy:
            locations = []
            for path in _resolve_level_paths(level, self.base_path, scope):
                if path.is_dir():
                    for child in path.iterdir():
                        if child.exists() and child.is_file():
                            locations.append(self._load_file(child, level.backend))
                elif path.exists() and path.is_file():
                    locations.append(self._load_file(path, level.backend))
                else:
                    locations.append(path)
            levels.append(tuple(locations))
        levels = tuple(levels)
        self._source_cache[cache_key] = levels
        return levels

    def _files_for(self, hierarchy, scope, tag):
        """The flattened, loaded-only view of :meth:`_levels_for` -- exactly
        what the old per-file walk returned, and what ``sources()`` shows."""
        return [
            path
            for locations in self._levels_for(hierarchy, scope, tag)
            for path in locations
            if path in self.cache
        ]

    def _default_levels(self, scope):
        return self._levels_for(self.default_hierarchy, scope, "default")

    def _lookup_options_map(self, levels, scope, tag="main"):
        """The merged ``lookup_options`` mapping for a scope, or ``None``.

        Merging it walks every location/level, and a default-merge ``get()``
        needs it for every key — so the result is cached per scope value
        alongside ``_source_cache``, sharing the same instance-lifetime
        staleness contract. Gathered through the location/level nesting
        only, never the layer stack (``lookup_adapter.rb:241,346-380``).
        """
        cache_key = (tag, scope)
        if cache_key in self._lookup_options_cache:
            return self._lookup_options_cache[cache_key]

        try:
            options = self._get_key("lookup_options", levels, scope, "hash")
        except KeyError:
            options = None
        if not isinstance(options, dict):
            options = None
        self._lookup_options_cache[cache_key] = options
        return options

    def _lookup_adapter(self, key, levels, scope):
        """Delegate to the lookup options adapter."""
        options = self._lookup_options_map(levels, scope)
        return _extract_lookup_options_for_key(key, options)

    def _lookup_options_for(self, key, levels, scope):
        """Return the merged ``lookup_options`` entry matching ``key``, or None.

        ``lookup_options`` is a reserved data key: ``{pattern: {merge, convert_to}}``.
        Higher-priority (earlier) levels win per pattern. An exact key match
        wins over a regex pattern match; the first regex match otherwise wins.
        """
        return self._lookup_adapter(key, levels, scope)

    def get(
        self,
        key: str,
        default=None,
        merge=None,
        throw=False,
    ):
        """Retrieve a hiera value by fully resolving its location, against
        this instance's bound scope.

        :param key: the hiera key to retrieve.
        :param default: returned when the key is missing (unless ``throw``).
        :param merge: Puppet's merge strategy names (``"first"``/``"default"``/
            ``"unique"``/``"hash"``/``"deep"``/``"reverse_deep"``/
            ``"unconstrained_deep"``) or a hash ``{"strategy": "deep",
            "knockout_prefix": "--", ...}``. When omitted, ``lookup_options``
            in the data (if any) decides; otherwise first-match wins. Invalid
            input (an unknown strategy, a hash with no ``strategy``, a
            mistyped option) raises ``hyera.MergeError``.
        :param throw: raise ``KeyError`` on a missing key instead of returning
            ``default``.
        """
        return self._get(key, default, merge, throw, self.scope)

    def _get(self, key, default, merge, throw, scope):
        if not isinstance(key, str):
            raise TypeError(
                "lookup key must be a str, not {}".format(type(key).__name__)
            )
        levels = self._levels_for(self.hierarchy, scope, "main")

        explicit = merge is not None
        strategy = MergeStrategy.strategy(merge)

        convert_to = None
        if not explicit and key is not None:
            opts = self._lookup_options_for(key, levels, scope)
            if opts is not None:
                if opts.get("merge") is not None:
                    strategy = MergeStrategy.strategy(opts["merge"])
                convert_to = opts.get("convert_to")

        # Main lookup: the full provider stack (only "global" populated).
        value = self._lookup_layers(key, levels, scope, strategy)
        if value is _MISSING and self.default_hierarchy:
            # default_hierarchy is consulted only on a main-hierarchy miss,
            # with the same strategy and no layer wrap
            # (``lookup_adapter.rb``'s ``lookup_default_in_module``); an
            # explicit caller ``merge=`` still applies even when
            # ``lookup_options`` picked the main strategy above.
            fallback = MergeStrategy.strategy(merge) if explicit else strategy
            value = self._lookup_levels(
                key, self._default_levels(scope), scope, fallback
            )

        if value is _MISSING:
            if throw:
                raise KeyNotFoundError(key) from None
            return default

        if convert_to is not None:
            value = convert_result(key, convert_to, value)
        return value


# Import after defining Hiera to avoid circular import
from .exceptions import (  # noqa: E402
    BackendError,
    ConfigError,
    HieraError,
    HieraLookupError,
    InterpolationError,
    KeyNotFoundError,
    _one_line,
)
