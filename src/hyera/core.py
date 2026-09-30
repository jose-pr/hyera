# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/pops/lookup/data_hash_function_provider.rb,
# data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging
import os
from typing import Any

from . import _data_functions
from ._data_provider import (
    _EnvironmentState,
    _IgnoredConfig,
    _Provider,
    find_environment,
    load_layer_provider,
    module_name_of,
    prune_module_data,
    split_path_setting,
)
from ._hiera_config import (
    HieraLevel,
    _build_hierarchies,
    _config_error,
    _fill_v5_defaults,
    _read_base_config,
    _select_version,
    _validate_v5,
)
from ._interpolation import interpolate
from ._invocation import _STRICT, Invocation
from ._location_resolver import resolve_locations
from ._lookup_adapter import (
    LOOKUP_OPTIONS,
    compile_patterns,
    convert_result,
    extract_lookup_options_for_key,
    validate_data_value,
    validate_lookup_options,
)
from ._lookup_function import lookup as _lookup_call, nested_lookup, parse_call
from ._merge_strategy import MergeStrategy
from ._navigation import _MISSING, parse_lookup_key, split_key, sub_lookup
from ._scope import Scope
from ._type_mismatch import assert_instance_of
from ._type_parser import parse_type
from .backends import default_backends

__all__ = ["Hiera"]

_LOGGER = logging.getLogger(__name__)

#: Puppet's provider stack (``lookup_adapter.rb:296``): a key is looked up
#: through each layer in turn, merged the same way as levels/locations
#: within a layer. All three layers are wired through
#: :meth:`Hiera._lookup_layers`; ``environment``/``module`` contribute
#: :data:`~hyera._navigation._MISSING` when no usable config exists there.
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


class Hiera:
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
    :param environmentpath: directories to search for an environment named
        by ``scope.environment`` (Puppet's ``--environmentpath``/
        ``environmentpath`` setting): a single path, an iterable of paths, or
        a string of paths separated by :data:`os.pathsep`. ``None`` (the
        default) means no environment directories at all: every environment
        name then resolves with no environment layer and no error (a
        difference from Puppet, which always has an ``environmentpath``).
        When set, an environment name other than ``"production"`` that is
        not found raises :class:`~hyera.ConfigError`; a missing
        ``"production"`` directory is not an error.
    :param basemodulepath: module directories searched for every
        environment, after that environment's own ``modules`` directory
        (Puppet's ``--basemodulepath``/``basemodulepath``). Same path forms
        as ``environmentpath``.
    :param modulepath: when given, replaces the whole modulepath (the
        environment's own ``modules`` directory included) for every
        environment, exactly as Puppet's ``--modulepath`` does. ``None``
        (the default) means "use Puppet's own construction" (the
        environment's ``modules`` directory, if any, then
        ``basemodulepath``).
    """

    def __init__(
        self,
        base_config,
        backends=None,
        base_path=None,
        *,
        scope: Scope = None,
        environmentpath=None,
        basemodulepath=(),
        modulepath=None,
    ):
        self.base_config = base_config
        if scope is None:
            scope = Scope()
        elif not isinstance(scope, Scope):
            raise TypeError("scope must be a hyera.Scope")
        self.scope = scope

        #: Puppet's three layer-discovery settings (``_data_provider.
        #: split_path_setting``): ``environmentpath`` stays ``None`` when
        #: unset (meaningful: no environment directories at all);
        #: ``basemodulepath`` normalizes to ``()``; ``modulepath`` stays
        #: ``None`` only when the keyword itself was never given (Puppet's
        #: own per-environment construction), an explicit empty value
        #: normalizing to ``()`` instead (an explicit empty modulepath).
        self._environmentpath = split_path_setting(environmentpath, "environmentpath")
        self._basemodulepath = (
            split_path_setting(basemodulepath, "basemodulepath") or ()
        )
        self._modulepath_override = (
            None
            if modulepath is None
            else (split_path_setting(modulepath, "modulepath") or ())
        )
        #: name -> ``_EnvironmentState``, shared with every ``scoped()`` view.
        self._environments: dict = {}
        #: Paths already warned about for an ignored version-3 layer config
        #: (:meth:`_usable`), so the warning fires once per config file.
        self._v3_warned_paths: set = set()
        #: ``(module_name, path) -> pruned data``, apart from the unpruned
        #: ``self.cache`` a global/environment read of the same file uses.
        self._pruned_cache: dict = {}

        self.hierarchy: "list[HieraLevel]" = []
        self.default_hierarchy: "list[HieraLevel]" = []
        #: ``(path, backend.strict) -> loaded data``. See ``_load_file``.
        self.cache: dict = {}
        #: Every plain path ever loaded successfully into ``self.cache``,
        #: under any ``strict`` variant -- what ``_files_for`` consults to
        #: tell a loaded location from a missing/unattempted one.
        self._loaded_paths: set = set()
        #: Per-scope cache of resolved source path lists (see ``sources``).
        self._source_cache: dict = {}
        #: scope -> the global layer's own validated ``lookup_options``.
        self._global_lo_cache: dict = {}
        #: ``(environment, scope) -> the global+environment HASH-merged
        #: ``lookup_options``.
        self._environment_lo_cache: dict = {}
        #: ``(environment, module_name, scope) -> the final compiled
        #: ``lookup_options`` mapping (:meth:`_retrieve_lookup_options`).
        self._lookup_options_cache: dict = {}

        self._load_config(
            default_backends() if backends is None else backends, base_path
        )

    def format(self, text: str) -> Any:
        """Interpolate ``text`` against this instance's bound scope, exactly
        as a data value is interpolated (Puppet's ``Context#interpolate``)."""
        return self._format(text, self.scope)

    def _format(self, text, scope: Scope):
        if not isinstance(text, str):
            raise TypeError(
                "format() expects a str, not {}".format(type(text).__name__)
            )
        strict_token = _STRICT.set(scope.strict)
        try:
            inv = Invocation(scope, self._sub_lookup)
            return interpolate(text, inv)
        finally:
            _STRICT.reset(strict_token)

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

        #: The global layer, wrapped for the provider-aware stack walk
        #: (:meth:`_lookup_layers`) -- the same ``self.hierarchy``/
        #: ``self.base_path``/``self.default_hierarchy`` objects, not a copy.
        self._global = _Provider(
            "Global",
            None,
            self.base_path,
            source,
            self.hierarchy,
            self.default_hierarchy,
            5,
        )
        # Puppet fails every lookup on a broken environment config; loading
        # the construction scope's own environment now gives the same
        # failure at construction instead.
        self._environment(self.scope.environment)

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

        Runs under ``self.scope.strict`` (the ``_STRICT`` ContextVar, same as
        ``lookup``/``dig``/``get``): a genuinely non-hash data file under
        ``strict="error"`` raises here as :class:`~hyera.BackendError` and is
        NOT caught by the except clause above (matching the documented
        constructor contract -- a data file that cannot be read or parsed
        can fail construction itself). This also keeps ``self.cache``'s
        ``(path, strict)`` entries consistent with what a later ``.lookup()``
        call on the same, unscoped instance will look for: ``Hiera.lookup``/
        ``in``/``.sources`` all reuse this same ``self.scope`` object, whose
        resolved source-path list ``_levels_for`` caches per scope value --
        without this, that cache hit would skip ``_load_file`` entirely on a
        later call, leaving ``self.cache`` holding only the pre-warm's own
        strict variant.
        """
        strict_token = _STRICT.set(self.scope.strict)
        try:
            self._sources(self.scope)
            if self.default_hierarchy:
                self._default_levels(self.scope)
        except (HieraLookupError, InterpolationError) as e:
            _LOGGER.debug("Pre-warm skipped after a lookup-time error: %s", e)
        finally:
            _STRICT.reset(strict_token)

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

        Cached per ``(path, backend.strict)``, not per bare ``path``: a data
        file's own non-hash rule (``YAMLBackend._as_data_hash``'s
        ``strict``-sensitive raise-or-warn) must run again for a call whose
        effective ``strict`` differs from a previous one, never reuse a
        result computed under a different strictness. ``self._loaded_paths``
        separately tracks which plain paths were ever read successfully, for
        :meth:`_files_for`'s "was this location loaded" check, independent
        of which ``strict`` variant did the loading.
        """
        cache_key = (path, backend.strict)
        if cache_key not in self.cache:
            if os.path.isdir(path):
                # An explicit check, identical on every OS: a bare open()
                # of a directory raises PermissionError on Windows and
                # IsADirectoryError on POSIX, and Puppet's own message here
                # is "Is a directory" regardless (data_hash_function_
                # provider.rb's `read` -> `cached_file_data` -> Ruby's
                # `io_fread`).
                raise BackendError(
                    "Unable to read ({}): Is a directory".format(path),
                    path=str(path),
                )
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
            self.cache[cache_key] = data
            self._loaded_paths.add(path)
        return path

    def _environment(self, name):
        """The cached :class:`~hyera._data_provider._EnvironmentState` for
        environment ``name`` (``puppet.rb:213-233``): discovered on first
        use, then reused by every later lookup and by every
        :meth:`scoped` view (``self._environments`` is shared, since
        :meth:`_view` copies ``__dict__`` without deep-copying it).

        With no ``environmentpath`` configured, every name resolves with no
        environment root and no error (a documented difference from Puppet,
        which always has one). With one configured, a name other than
        ``"production"`` that is not found raises :class:`~hyera.ConfigError`
        with Puppet's own text; a missing ``"production"`` directory is not
        an error (Puppet's static default environment).
        """
        state = self._environments.get(name)
        if state is not None:
            return state

        root = None
        if self._environmentpath:
            root = find_environment(self._environmentpath, name)
            if root is None and name != "production":
                raise ConfigError(
                    "Could not find a directory environment named '{}' "
                    "anywhere in the path: {}. Does the directory exist?".format(
                        name,
                        os.pathsep.join(str(p) for p in self._environmentpath),
                    )
                )

        provider = (
            load_layer_provider("Environment", root, self.backends)
            if root is not None
            else None
        )
        if self._modulepath_override is not None:
            modulepath = self._modulepath_override
        elif root is not None:
            modulepath = (root / "modules",) + self._basemodulepath
        else:
            modulepath = self._basemodulepath

        state = _EnvironmentState(name, root, provider, modulepath)
        self._environments[name] = state
        return state

    def _module_provider(self, state, module_name):
        """The cached layer provider for ``module_name`` in environment
        ``state`` (``module.rb:303-312``): ``None`` when no module of that
        name is on the modulepath, or it has no ``hiera.yaml``."""
        cache = state.module_providers
        if module_name in cache:
            return cache[module_name]
        result = None
        module_dir = state.modules().get(module_name)
        if module_dir is not None:
            result = load_layer_provider(
                "Module", module_dir, self.backends, module_name=module_name
            )
        cache[module_name] = result
        return result

    def _usable(self, provider, invocation):
        """A layer provider ready to be walked, or ``None``.

        ``None``/a real :class:`~hyera._data_provider._Provider` pass
        through unchanged. An :class:`~hyera._data_provider._IgnoredConfig`
        (a version-3, or missing-version, config outside the global layer)
        is Puppet's own per-use decision (``environment_data_provider.
        rb:15-26``/``module_data_provider.rb:64-75``): under
        ``strict="error"`` it raises; otherwise it warns once per config
        path and the layer contributes nothing.
        """
        if provider is None or isinstance(provider, _Provider):
            return provider
        if provider.place == "Environment":
            noun, warn_text = "an environment", "the environment root"
        else:
            noun, warn_text = "a module", "module root"
        if invocation.scope.strict == "error":
            raise _config_error(
                provider.source,
                "hiera.yaml version 3 cannot be used in {}".format(noun),
            )
        path = provider.source.path
        if path not in self._v3_warned_paths:
            self._v3_warned_paths.add(path)
            _LOGGER.warning("hiera.yaml version 3 found at %s was ignored", warn_text)
        return None

    def _lookup_levels(self, root, levels, invocation, strategy, module_name=None):
        """Puppet's per-location/per-level reduce (``data_hash_function_
        provider.rb:26-33`` over a level's locations,
        ``configured_data_provider.rb:49-61`` over the hierarchy's levels)
        on the bare root key only -- never a dotted key. Digging a dotted
        key's segments out of the result is the caller's job
        (:meth:`_search_and_merge`), done exactly once, after this merge
        completes, never per location or per level.

        ``levels`` is a tuple of ``(function_name, locations)`` pairs, one
        per hierarchy level (:meth:`_levels_for`); ``strategy`` is an
        already resolved :class:`~hyera._merge_strategy.MergeStrategy`.
        Returns the merged root value, or :data:`~hyera._navigation._MISSING`
        on a miss.

        ``module_name``, when given, reads every location through
        :func:`~hyera._data_provider.prune_module_data` first (Puppet's
        module-data namespace rule), cached per ``(module_name, path)``
        apart from ``self.cache``'s own unpruned entry -- a file shared with
        the global layer stays unpruned there.

        A found value that is ``None`` (data explicitly set to ``~``) is a
        genuine value, not a miss (``data_hash_function_provider.rb:58-62``):
        only an absent root key is. Every found root value is checked
        against Puppet's ``LookupValue`` RichData rule
        (:func:`~hyera._lookup_adapter.validate_data_value`) before it is
        interpolated, so a bad *sibling* key in the same file never breaks
        this lookup.
        """

        def at_location(entry):
            function_name, path = entry
            data = self.cache.get((path, _STRICT.get()), _MISSING)
            if data is _MISSING:
                return _MISSING
            if module_name is not None:
                data = self._pruned_module_data(data, module_name, function_name, path)
            if not isinstance(data, dict) or root not in data:
                return _MISSING
            value = data[root]
            validate_data_value(value, function_name, path, root)
            return interpolate(value, invocation)

        def at_level(level):
            function_name, locations = level
            return strategy.lookup(
                [(function_name, loc) for loc in locations], at_location
            )

        return strategy.lookup(levels, at_level)

    def _pruned_module_data(self, data, module_name, function_name, path):
        key = (module_name, path)
        pruned = self._pruned_cache.get(key)
        if pruned is None:
            pruned = prune_module_data(data, module_name, function_name, path)
            self._pruned_cache[key] = pruned
        return pruned

    def _levels_for_provider(self, provider, scope, tag):
        """:meth:`_levels_for`, resolved against ``provider``'s own root."""
        return self._levels_for(provider.hierarchy, provider.root, scope, tag)

    def _lookup_layers(self, root, module_name, invocation, strategy):
        """Puppet's provider stack (``lookup_adapter.rb:332-340``): reduce
        ``_LAYERS``, one provider per layer.

        The global layer always runs. The environment layer runs the usable
        provider (if any) of ``invocation.scope.environment``. The module
        layer runs only for a qualified key (``module_name`` set), the
        usable provider (if any) of that module in the same environment.
        A layer with no usable provider contributes
        :data:`~hyera._navigation._MISSING`, so every layer is always tried
        in order, as Puppet's own multi-variant reduce does.
        """

        def at_layer(layer):
            if layer == "global":
                provider = self._global
            elif layer == "environment":
                state = self._environment(invocation.scope.environment)
                provider = self._usable(state.provider, invocation)
            elif layer == "module":
                if module_name is None:
                    return _MISSING
                state = self._environment(invocation.scope.environment)
                provider = self._usable(
                    self._module_provider(state, module_name), invocation
                )
            else:
                return _MISSING
            if provider is None:
                return _MISSING
            levels = self._levels_for_provider(provider, invocation.scope, "main")
            mod = provider.module_name if provider.place == "Module" else None
            return self._lookup_levels(
                root, levels, invocation, strategy, module_name=mod
            )

        return strategy.lookup(_LAYERS, at_layer)

    def _search_and_merge(self, key, invocation, merge, parsed=None):
        """Resolve ``key`` in full: the port of ``LookupAdapter#lookup``
        plus ``do_lookup`` (``lookup_adapter.rb:46-82,332-340``).

        ``lookup_options`` and a ``"lookup_options."``-prefixed key always
        miss without reaching any data (``lookup_adapter.rb:48-52``) -- the
        one place that rule is enforced (:class:`~hyera._invocation.
        Invocation` no longer duplicates it). Otherwise: ``lookup_options``
        for the key's root is fetched *always*, even when ``merge`` is
        given explicitly -- only the *merge* it names is then skipped,
        never its ``convert_to`` (``lookup_adapter.rb:65-72``). The main
        hierarchy is walked through the provider-layer stack (global, then
        the key's environment, then -- for a qualified key -- its module) on
        the bare root key; a dotted key's segments are dug out of the merged
        root value exactly once (never per location, per level or per
        layer). On a miss, and only when the global config has a
        ``default_hierarchy``, the same walk (without the layer stack, same
        strategy) runs over it. A final miss returns
        :data:`~hyera._navigation._MISSING`; a found value has
        ``convert_to`` applied, if the options set one.

        ``parsed`` lets a caller that already split ``key`` into
        ``(root, segments)`` skip re-parsing it.
        """
        if key == LOOKUP_OPTIONS or key.startswith(LOOKUP_OPTIONS + "."):
            return _MISSING
        root, segments = parsed if parsed is not None else parse_lookup_key(key)
        module_name = module_name_of(root)

        compiled_options = self._retrieve_lookup_options(module_name, invocation)
        options = extract_lookup_options_for_key(root, compiled_options) or {}
        strategy = MergeStrategy.strategy(
            merge if merge is not None else options.get("merge")
        )

        with invocation.check(key):
            value = self._lookup_layers(root, module_name, invocation, strategy)
        if value is not _MISSING and segments:
            value = sub_lookup(key, segments, value)

        if value is _MISSING and self.default_hierarchy:
            default_levels = self._default_levels(invocation.scope)
            with invocation.check(key):
                value = self._lookup_levels(root, default_levels, invocation, strategy)
            if value is not _MISSING and segments:
                value = sub_lookup(key, segments, value)

        if value is _MISSING:
            return _MISSING
        convert_to = options.get("convert_to")
        if convert_to is not None:
            value = convert_result(key, convert_to, value)
        return value

    def _sub_lookup(self, key, invocation):
        """The host callable behind an :class:`~hyera._invocation.Invocation`
        (``%{hiera()}``/``%{lookup()}``/``%{alias()}``): a full lookup of
        ``key`` -- its own ``lookup_options``, ``default_hierarchy``
        fallback and ``convert_to`` all apply exactly as a top-level
        lookup -- with ``merge`` always ``None``: the caller's own
        accumulated merge is never carried over into a sub-lookup
        (``interpolation.rb:84``, which always passes a ``nil`` merge).
        The override hash and the default values hash still apply, exactly
        as a top-level lookup of the same key would see them
        (``interpolation.rb:77-86``). Returns
        :data:`~hyera._navigation._MISSING` on a miss instead of raising.
        """
        return nested_lookup(key, invocation, self._search_and_merge)

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
    ) -> "Hiera":
        """A view of this instance bound to ``self.scope.derive(...)``.

        The view is a full :class:`Hiera`, not a proxy: it shares this
        instance's config, backends and caches (``cache``, ``_source_cache``,
        ``_lookup_options_cache``, all keyed on the scope value already), so
        every method -- ``lookup``/``()``/``[]``/``in``, ``sources()``,
        ``format()`` -- reads the derived scope instead of ``self.scope``.
        Deriving from a view derives from *its* scope, not the original.
        """
        return self._view(
            self.scope.derive(
                variables=variables,
                facts=facts,
                trusted=trusted,
                server_facts=server_facts,
                environment=environment,
                strict=strict,
                node_name=node_name,
            )
        )

    def _view(self, scope: Scope) -> "Hiera":
        view = object.__new__(type(self))
        view.__dict__.update(self.__dict__)
        view.scope = scope
        return view

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
        return self._files_for(self.hierarchy, self.base_path, scope, "main")

    def _levels_for(self, hierarchy, base_path, scope, tag):
        """Every location each hierarchy level visits, one ``(function_name,
        locations)`` pair per level.

        A location entry is a loaded cache-key path (an existing file,
        already read through :meth:`_load_file`) or the plain
        (unloaded/non-existent) candidate path itself -- a "missing"
        location, still a location Puppet's own strategy reduce sees (see
        ``at_location`` in :meth:`_lookup_levels`, which maps anything not
        in ``self.cache`` to a miss). A ``path``/``paths``/mapped location
        that names a directory still reaches :meth:`_load_file`, which
        raises :class:`~hyera.BackendError`; a glob's matches never include
        a directory (:func:`~hyera._location_resolver.resolve_locations`
        drops them) and always exist.

        ``function_name`` is the level's ``data_hash`` function name
        (``level.backend.name``), reported by :func:`~hyera._lookup_adapter.
        validate_data_value` in a RichData error -- the same name
        :meth:`_load_file` already uses for the "returned from data_hash
        function" Hash check.

        Cached per ``(tag, base_path, scope)`` (the filesystem walk --
        glob/stat -- is what's expensive, not the reduce over the result),
        sharing ``_source_cache`` across every layer/level walk this
        instance ever runs: ``base_path`` disambiguates providers that share
        a hierarchy shape but not a root (every environment/module has its
        own), so two providers never collide even under the same ``tag``. A
        Scope value always hashes (it is immutable by construction), so
        there is no "unhashable context value" fallback to skip caching.
        """
        cache_key = (tag, base_path, scope)
        cached = self._source_cache.get(cache_key)
        if cached is not None:
            return cached

        levels = []
        for level in hierarchy:
            locations = []
            for loc in resolve_locations(level, base_path, scope):
                if loc.exist:
                    locations.append(self._load_file(loc.location, level.backend))
                else:
                    locations.append(loc.location)
            levels.append((level.backend.name, tuple(locations)))
        levels = tuple(levels)
        self._source_cache[cache_key] = levels
        return levels

    def _files_for(self, hierarchy, base_path, scope, tag):
        """The flattened, loaded-only view of :meth:`_levels_for` -- exactly
        what the old per-file walk returned, and what ``sources()`` shows."""
        return [
            path
            for _function_name, locations in self._levels_for(
                hierarchy, base_path, scope, tag
            )
            for path in locations
            if path in self._loaded_paths
        ]

    def _default_levels(self, scope):
        return self._levels_for(
            self.default_hierarchy, self.base_path, scope, "default"
        )

    def _layer_lookup_options(self, provider, invocation):
        """The reserved ``lookup_options`` key's raw value across
        ``provider``'s own hierarchy only (a HASH-strategy gather over its
        locations/levels, never across layers) -- :data:`~hyera._navigation.
        _MISSING` when the key is absent everywhere in this provider, else
        the found value (``None`` for an explicit ``lookup_options: ~``,
        distinct from a miss -- ``_retrieve_lookup_options`` tells them
        apart). A module provider's pruned data keeps ``lookup_options``
        (:func:`~hyera._data_provider.prune_module_data` special-cases it).
        """
        levels = self._levels_for_provider(provider, invocation.scope, "main")
        mod = provider.module_name if provider.place == "Module" else None
        with invocation.check(LOOKUP_OPTIONS):
            return self._lookup_levels(
                LOOKUP_OPTIONS,
                levels,
                invocation,
                MergeStrategy.strategy("hash"),
                module_name=mod,
            )

    def _global_lookup_options(self, scope):
        """The global layer's own validated ``lookup_options``, or ``None``.

        Cached once per scope value, with the same recursion-safe "store
        ``None`` first" pattern the single-config gather used
        (``lookup_adapter.rb:375-378``): a value inside the global
        ``lookup_options`` can itself run a full sub-lookup that asks this
        same cache while this gather is still running.
        """
        if scope in self._global_lo_cache:
            return self._global_lo_cache[scope]
        self._global_lo_cache[scope] = None
        try:
            meta = Invocation(scope, self._sub_lookup)
            raw = self._layer_lookup_options(self._global, meta)
            result = validate_lookup_options(None if raw is _MISSING else raw, None)
        except Exception:
            del self._global_lo_cache[scope]
            raise
        self._global_lo_cache[scope] = result
        return result

    def _environment_lookup_options(self, state, scope):
        """The global and environment layers' ``lookup_options`` HASH-merged
        (global wins), or ``None`` (``lookup_adapter.rb:375-380``). Cached
        per environment name and scope.
        """
        cache_key = (state.name, scope)
        if cache_key in self._environment_lo_cache:
            return self._environment_lo_cache[cache_key]
        self._environment_lo_cache[cache_key] = None
        try:
            g = self._global_lookup_options(scope)
            meta = Invocation(scope, self._sub_lookup)
            provider = self._usable(state.provider, meta)
            e = None
            if provider is not None:
                raw = self._layer_lookup_options(provider, meta)
                e = validate_lookup_options(None if raw is _MISSING else raw, None)
            if g is None:
                result = e
            elif e is None:
                result = g
            else:
                result = MergeStrategy.strategy("hash").merge(g, e)
        except Exception:
            del self._environment_lo_cache[cache_key]
            raise
        self._environment_lo_cache[cache_key] = result
        return result

    def _retrieve_lookup_options(self, module_name, invocation):
        """The compiled ``lookup_options`` mapping for ``module_name`` (or
        just the global/environment options, when ``module_name`` is
        ``None``), a port of ``lookup_adapter.rb:346-372``. Cached per
        environment name, module name and scope.

        A module's own options are qualified against its name
        (:func:`~hyera._lookup_adapter.validate_lookup_options`) and
        gathered from its pruned data (which keeps ``lookup_options``),
        never merged with the global/environment options wholesale --
        module wins per key, through the same HASH strategy, but only when
        the module actually declares a real (non-``None``) mapping; a
        module walk that finds nothing at all (:data:`~hyera._navigation.
        _MISSING`) leaves the environment options untouched, while one that
        finds an explicit ``lookup_options: ~`` discards them (Puppet's own
        ``if``/``elsif`` with no ``else``, ``lookup_adapter.rb:358-365``).
        """
        scope = invocation.scope
        state = self._environment(scope.environment)
        cache_key = (state.name, module_name, scope)
        if cache_key in self._lookup_options_cache:
            return self._lookup_options_cache[cache_key]
        self._lookup_options_cache[cache_key] = None
        try:
            opts = self._environment_lookup_options(state, scope)
            if module_name is not None:
                meta = Invocation(scope, self._sub_lookup)
                mprovider = self._usable(
                    self._module_provider(state, module_name), meta
                )
                if mprovider is not None:
                    raw = self._layer_lookup_options(mprovider, meta)
                    if raw is not _MISSING:
                        m = validate_lookup_options(raw, module_name)
                        if opts is None:
                            opts = m
                        elif m is not None:
                            opts = MergeStrategy.strategy("hash").merge(opts, m)
                        else:
                            opts = None
            compiled = compile_patterns(opts)
        except Exception:
            del self._lookup_options_cache[cache_key]
            raise
        self._lookup_options_cache[cache_key] = compiled
        return compiled

    def lookup(
        self,
        name,
        value_type=None,
        merge=None,
        default_value=_MISSING,
        *,
        default_values_hash=None,
        override=None,
        block=None,
    ):
        """Puppet's ``lookup()``: resolve ``name`` against this instance's
        bound scope, in Puppet's own precedence order.

        Five call forms, all equivalent (Puppet's ``functions/lookup.rb``):

        1. ``h.lookup("key")`` -- name only.
        2. ``h.lookup("key", "Integer")`` -- name and ``value_type``.
        3. ``h.lookup("key", "Integer", "first", 0)`` -- name, ``value_type``,
           ``merge``, ``default_value``, all positional.
        4. ``h.lookup({"name": "key", "merge": "first"})`` -- a single dict
           in place of every argument (``name`` required; every other
           positional argument and option keyword must be omitted).
        5. ``h.lookup("key", {"merge": "first"})`` -- name positional, every
           other option in a dict passed as ``value_type``.

        Every option name (``value_type``, ``merge``, ``default_value``,
        ``default_values_hash``, ``override``) also works as a keyword:
        ``h.lookup("key", merge="first")``. Passing an options dict (forms 4
        or 5) together with another positional argument or an option
        keyword is a ``TypeError`` -- except ``block``, which forms 4 and 5
        both still accept as its own argument.

        :param name: the key, or a list of keys tried in order (the first
            one that is found, anywhere in the precedence order below,
            wins).
        :param value_type: a Puppet type expression (``"Integer"``,
            ``"Optional[String]"``); every candidate value (override, found,
            a default) is asserted against it, raising ``HieraLookupError``
            with Puppet's own subject text ("Found value has wrong type,
            …", "Default value has wrong type, …", etc.) on a mismatch.
        :param merge: as :meth:`~hyera.Hiera.lookup`'s data-lookup merge
            strategy; overrides only the *merge* ``lookup_options`` would
            have picked -- an applicable ``convert_to`` still runs.
        :param default_value: returned (after ``value_type``) when nothing
            else was found; omit it entirely for "no default" (a bare
            ``None`` is a real default and beats a miss).
        :param default_values_hash: consulted, per name, only after the
            hierarchy itself missed every name.
        :param override: consulted, per name, *before* the hierarchy; also
            interpolated into any value's ``%{var}`` references, but never
            changes which hierarchy locations are read.
        :param block: called with ``name`` exactly as given when nothing
            else was found (before ``default_value``); its return value is
            asserted against ``value_type`` too.

        Precedence, per name in ``name``'s order: ``override`` -> the
        hierarchy (with ``lookup_options``, ``default_hierarchy`` and
        ``convert_to``) -> (next name) -> ``default_values_hash`` (every
        name again) -> ``block`` -> ``default_value`` -> ``KeyNotFoundError``
        (also a ``KeyError``), naming every name tried.
        """
        call = parse_call(
            name, value_type, merge, default_value, default_values_hash, override, block
        )
        invocation = Invocation(
            self.scope,
            self._sub_lookup,
            override_values=call.override,
            default_values=call.default_values_hash,
        )
        return _lookup_call(call, invocation, self._search_and_merge)

    __call__ = lookup

    def __getitem__(self, item):
        if isinstance(item, tuple):
            return self.lookup(*item)
        return self.lookup(item)

    def __contains__(self, name) -> bool:
        try:
            self.lookup(name)
            return True
        except KeyNotFoundError:
            return False

    #: Without this, ``__getitem__`` alone would make a bare ``Hiera``
    #: iterable (Python falls back to calling ``[0]``, ``[1]``, ... until
    #: ``IndexError`` -- here, an endless stream of ``KeyNotFoundError``
    #: instead). A ``Hiera`` is not a sequence; ``iter(h)`` raises
    #: ``TypeError`` instead.
    __iter__ = None

    def dig(
        self,
        *keys,
        value_type=None,
        merge=None,
        default_values_hash=None,
        override=None,
    ):
        """Puppet's ``dig()`` (``functions/dig.rb``): look up ``keys[0]``,
        then dig the rest of ``keys`` out of it, Ruby ``Hash#dig``/
        ``Array#dig`` style.

        A miss on ``keys[0]`` gives ``None`` (like Puppet's ``dig(undef,
        ...)``) -- `.lookup()` is the strict call; ``.dig()`` never raises
        ``KeyNotFoundError``. ``merge``/``default_values_hash``/``override``
        apply to that root lookup, exactly as they would to `.lookup()`. A
        key after the first that is not an ``int`` against a ``list``, or
        any key against a non-collection value, raises
        ``HieraLookupError`` naming the path walked and the Puppet type
        found instead. ``value_type``, when given, asserts the final result
        with the subject "Found value". Needs at least one key, the first a
        ``str``, else ``TypeError``.
        """
        if not keys or not isinstance(keys[0], str):
            raise TypeError("dig() needs at least one key, the first a str")
        root = self.lookup(
            keys[0],
            None,
            merge,
            None,
            default_values_hash=default_values_hash,
            override=override,
        )
        result = _data_functions.dig(root, keys[1:])
        if value_type is not None:
            assert_instance_of("Found value", parse_type(value_type), result)
        return result

    def get(
        self,
        dotted,
        default_value=None,
        block=None,
        *,
        value_type=None,
        merge=None,
        default_values_hash=None,
        override=None,
    ):
        """Puppet's ``get()`` (``functions/get.rb``): resolve the root of
        ``dotted`` through `.lookup()`, then dig the rest of it out of the
        result -- unlike the removed old ``.get()``, this ``dotted``
        argument is a single Puppet dotted-navigation *string*
        (``"a.b.0"``), not a plain key.

        ``dotted`` must be a non-empty ``str`` (there is no whole-data value
        to fall back to), else ``HieraLookupError("Syntax error in dotted-
        navigation string")``, same as a malformed one; a non-``str``
        ``dotted`` raises ``TypeError`` instead. The root segment is looked
        up like `.lookup()` (``merge``/``default_values_hash``/``override``
        apply to it; an ``int`` root can never match a hiera key, so it is
        treated as a miss directly, without a lookup at all); a root miss
        or a found ``None`` returns ``default_value``, never raises. The
        remaining segments are dug out with Puppet's ``dig()`` semantics; a
        walk error (a non-collection or a non-integer list index) reaches
        ``block(error)`` when given, else raises. ``value_type``, when
        given, asserts the final result with the subject that says where it
        came from ("Found value", "Default value" or "Value returned from
        block").
        """
        if not isinstance(dotted, str):
            raise TypeError(
                "get() dotted key must be a str, not {}".format(type(dotted).__name__)
            )
        if dotted == "":
            raise HieraLookupError("Syntax error in dotted-navigation string")
        segments = split_key(
            dotted,
            lambda _problem: HieraLookupError(
                "Syntax error in dotted-navigation string"
            ),
        )
        root = segments[0]
        if not isinstance(root, str):
            root_value = None
        else:
            call = parse_call(
                root, None, merge, None, default_values_hash, override, None
            )
            invocation = Invocation(
                self.scope,
                self._sub_lookup,
                override_values=call.override,
                default_values=call.default_values_hash,
            )

            def search(name, inv, m, _root=root):
                # Puppet's own `get()` looks up the root by its *segment*
                # form directly (never re-parsed): a quoted root such as
                # `'"a.b".c'` must not have its own embedded dot split
                # again by a second `parse_lookup_key` pass.
                return self._search_and_merge(name, inv, m, parsed=(_root, ()))

            root_value = _lookup_call(call, invocation, search)
        result, subject = _data_functions.get_segments(
            root_value, segments[1:], default_value, block
        )
        if value_type is not None:
            assert_instance_of(subject, parse_type(value_type), result)
        return result

    def getvar(self, dotted, default_value=None, block=None):
        """Puppet's ``getvar()`` (``functions/getvar.rb``): Puppet's
        ``get()`` over a scope variable's value instead of a looked-up one.

        ``dotted`` must start with a valid (optionally ``::``-qualified)
        Puppet variable name, immediately followed by ``.`` or the string's
        end, else ``HieraLookupError``. An undefined variable returns
        ``default_value`` regardless of the bound scope's ``strict`` --
        Puppet's own ``catch(:undefined_variable)``, never a raise for that
        reason alone. The rest navigates exactly as `.get()` does.
        """
        return _data_functions.getvar(self.scope, dotted, default_value, block)


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
