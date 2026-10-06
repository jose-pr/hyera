# Ported from Puppet 8 lib/puppet/pops/lookup/data_hash_function_provider.rb,
# data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging
import os
import threading
import typing as _ty
from typing import Any

from pathlib_next import Path

from ._lookup import data_functions as _data_functions
from ._lookup.cache import _ScopeKeyedCache
from ._config.data_provider import (
    _EnvironmentState,
    _Provider,
    find_environment,
    load_layer_provider,
    module_name_of,
    prune_module_data,
    split_path_setting,
)
from ._output import explain as _explain
from ._output.explain import (
    Explainer,
    ExplainResult,
    _DebugExplainer,
    _ProviderRef,
    _debug_preamble,
    _provider_ref,
)
from ._config.hiera_config import (
    HieraLevel,
    _build_hierarchies,
    _config_error,
    _config_version,
    _default_codedir,
    _fill_v5_defaults,
    _read_base_config,
    _read_v3,
    _read_v4,
    _validate_v5,
)
from ._lookup.function_provider import PROVIDER_CLASSES, _EnvironmentContext
from ._lookup.interpolation import interpolate, unshare
from ._lookup.invocation import _STRICT, Invocation
from ._lookup.locations import _LocationStore
from ._lookup.lookup_adapter import (
    LOOKUP_OPTIONS,
    _ExplainOptionsMemo,
    convert_result,
    extract_lookup_options_for_key,
    memoized_options,
    module_default_lookup_options,
    retrieve_lookup_options,
)
from ._lookup.lookup_function import (
    check_call,
    depth_error,
    lookup as _lookup_call,
    parse_call,
    recursion_bound,
)
from ._lookup.merge_strategy import MergeSpec, MergeStrategy
from ._lookup.navigation import (
    _MISSING,
    join_key,
    parse_lookup_key,
    split_key,
    sub_lookup,
)
from ._scope.scope import Scope, Strict
from ._types.mismatch import assert_instance_of
from .backends import Backend, default_backends
from .exceptions import (
    BackendError,
    ConfigError,
    HieraError,
    HieraLookupError,
    KeyNotFoundError,
    _escapes,
    _one_line,
)
from .types import TypeSpec

__all__ = ["Hiera"]

_LOGGER = logging.getLogger(__name__)

#: A single path, an iterable of paths, or a string of paths joined by
#: ``os.pathsep`` -- the shape every ``*path`` constructor argument takes.
_PathSpec = _ty.Union[
    str, "os.PathLike[str]", _ty.Iterable[_ty.Union[str, "os.PathLike[str]"]], None
]

#: Puppet's provider stack (``lookup_adapter.rb:296``): a key is looked up
#: through each layer in turn, merged the same way as levels/locations
#: within a layer. All three layers are wired through
#: :meth:`Hiera._lookup_layers`; ``environment``/``module`` contribute
#: :data:`~hyera._lookup.navigation._MISSING` when no usable config exists there.
_LAYERS = ("global", "environment", "module")


def _no_option_lookup(key, invocation):
    """The ``lookup`` callable for a hierarchy level's ``options``
    :class:`~hyera._lookup.invocation.Invocation`. Unreachable in practice: options
    interpolate with ``allow_methods=False``, which rejects every method
    call (``%{hiera()}``/``%{lookup()}``/``%{alias()}``) -- the only way a
    sub-lookup would ever be attempted -- before it could reach this
    callable."""
    raise RuntimeError("hierarchy options never perform a sub-lookup")


#: Cache attributes that take the shared lock themselves in ``clear()``.
_LOCKED_CACHES = ("_lookup_options_cache",)
#: Cache attributes cleared under the shared lock by :meth:`Hiera.clear_cache`.
_PLAIN_CACHES = (
    "_pruned_cache",
    "_compiled_options_cache",
    "_providers",
    "_environment_context",
)
#: Everything :meth:`Hiera._init_caches` creates: a copy or an unpickled
#: instance starts with none of it.
_DERIVED_STATE = ("_cache_lock", "_generation", "_store") + (
    _LOCKED_CACHES + _PLAIN_CACHES
)
#: The derived state a view keeps for itself: bound to one scope.
_VIEW_OWN_STATE = ("_providers",)


def _debug_explainer(explainer=None):
    """The ``explainer`` a root :class:`~hyera._lookup.invocation.Invocation`
    should actually carry: wrapped in a :class:`~hyera._output.explain._DebugExplainer`
    while the ``hyera._output.explain`` logger allows ``DEBUG`` (checked once per
    top-level call, matching Puppet's own ``Puppet[:debug]`` read at
    ``Invocation.new``), else ``explainer`` unchanged (``None`` for an
    ordinary lookup with no explicit ``explain()`` in progress).
    """
    if _explain._LOGGER.isEnabledFor(logging.DEBUG):
        return _DebugExplainer(explainer)
    return explainer


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
    :param codedir: Puppet's ``$codedir``, consulted only by a version 3
        hiera.yaml's default per-backend datadir
        (``<codedir>/environments/%{::environment}/hieradata``). ``None``
        (the default) means Puppet's own AIO default for the platform
        (``%ALLUSERSPROFILE%\\PuppetLabs\\code`` on Windows,
        ``/etc/puppetlabs/code`` elsewhere) — never the per-user
        ``~/.puppetlabs/etc/code`` default, and never discovered from
        ``puppet.conf``.
    :param cache_size: how many distinct scope-dependent entries each of
        the location/``lookup_options``/glob caches keeps before evicting
        the least recently used; ``None`` means unbounded. Must be a
        non-negative ``int``, else ``TypeError``/``ValueError``.
    :param revalidate: whether every lookup re-checks the data files and
        glob listings it uses for changes since they were last read.
        ``False`` keeps everything as first read until :meth:`clear_cache`.
        Must be a ``bool``, else ``TypeError``.
    :raises ConfigError: for a missing, unreadable or invalid
        ``hiera.yaml``, or an environment named by ``scope.environment``
        that ``environmentpath`` cannot find.
    :raises TypeError: for a ``scope``/``cache_size``/``revalidate`` of
        the wrong type.
    :raises ValueError: for a negative ``cache_size``.
    """

    def __init__(
        self,
        base_config: "_ty.Union[str, os.PathLike[str], _ty.IO[str], _ty.IO[bytes], _ty.Dict[str, _ty.Any], None]",
        backends: "_ty.Optional[_ty.Sequence[_ty.Type[Backend]]]" = None,
        base_path: "_ty.Union[str, os.PathLike[str], None]" = None,
        *,
        scope: _ty.Optional[Scope] = None,
        environmentpath: _PathSpec = None,
        basemodulepath: _PathSpec = (),
        modulepath: _PathSpec = None,
        cache_size: _ty.Optional[int] = 256,
        revalidate: bool = True,
        codedir: "_ty.Union[str, os.PathLike[str], None]" = None,
    ) -> None:
        self.base_config: "_ty.Union[str, os.PathLike[str], _ty.IO[str], _ty.IO[bytes], _ty.Dict[str, _ty.Any], None]" = (base_config)
        #: Whether this is Puppet's own built-in default config
        #: (``Hiera(None, ...)``), the one case ``explain()`` prunes a
        #: missing candidate from at all (``hiera_config.rb:688``,
        #: ``location_resolver.rb:63``) -- a dict or stream config is
        #: user-authored and keeps every ``Path not found`` line.
        self._is_default_config = base_config is None
        #: Puppet's ``$codedir`` (``util/run_mode.rb``), used only by a
        #: version 3 hierarchy's default per-backend ``datadir``
        #: (``<codedir>/environments/%{::environment}/hieradata``). An
        #: explicit value is made absolute against the working directory at
        #: construction, exactly like ``base_path``.
        self._codedir: Path = (
            _default_codedir() if codedir is None else Path(codedir).absolute()
        )
        if scope is None:
            scope = Scope()
        elif not isinstance(scope, Scope):
            raise TypeError("scope must be a hyera.Scope")
        self.scope = scope
        if cache_size is not None:
            if isinstance(cache_size, bool) or not isinstance(cache_size, int):
                raise TypeError(
                    "cache_size must be an int or None, not {}".format(
                        type(cache_size).__name__
                    )
                )
            if cache_size < 0:
                raise ValueError("cache_size must be >= 0")
        self._cache_size: _ty.Optional[int] = cache_size
        if not isinstance(revalidate, bool):
            raise TypeError(
                "revalidate must be a bool, not {}".format(type(revalidate).__name__)
            )
        self._revalidate: bool = revalidate

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
        self._hierarchy: "list[HieraLevel]" = []
        self._default_hierarchy: "list[HieraLevel]" = []
        self._init_caches()

        self._load_config(
            default_backends() if backends is None else backends, base_path
        )

    def _init_caches(self) -> None:
        """(Re)create every cache and the lock they share -- called from
        ``__init__`` and from :meth:`__setstate__` (a copy/unpickle starts
        with every cache empty). One lock covers dict/``_known``/``_last``
        mutation on all of them; a rebuild itself never runs under it.
        """
        self._cache_lock = threading.Lock()
        #: Bumped by :meth:`clear_cache`; shared by an instance and its
        #: views. A provider built under an older value is rebuilt, which
        #: drops the data it holds.
        self._generation = [0]
        #: Resolved locations, glob listings and parsed data files: shared
        #: by every view derived from this instance (unlike ``_providers``,
        #: see ``_view``), since ``base_path`` -- the owning layer's own
        #: root -- disambiguates one layer's hierarchy from another's.
        self._store = _LocationStore(
            self._cache_lock, self._cache_size, self._revalidate
        )
        #: The ``lookup_options`` value gathered from one layer's own
        #: hierarchy alone (never merged across layers), keyed the same way,
        #: plus the location entry it was built against. See
        #: ``layer_options_cached``.
        self._lookup_options_cache = _ScopeKeyedCache(
            self._cache_lock, self._cache_size
        )
        #: ``(module_name, function_name, path) -> (parsed data, pruned
        #: data)``, apart from the store's unpruned file cache a global/
        #: environment read of the same file uses; valid while the parsed
        #: data is the very object the pruned copy was made from.
        self._pruned_cache: dict = {}
        #: ``module_name -> (scope, compiled)``: the single most recent
        #: ``retrieve_lookup_options`` result for that ``module_name``, an
        #: identity fast path exactly like ``_ScopeKeyedCache``'s own
        #: ``_last`` (safe because ``Scope`` is immutable -- the same scope
        #: object always composes to the same result). Composing the three
        #: layers' already-cached raw gathers and re-running
        #: ``validate_lookup_options``/``compile_patterns`` (which
        #: recompiles every ``^``-prefixed pattern's regex) on every single
        #: lookup would otherwise repeat that work for a ``lookup_options``
        #: mapping that never changed. A miss here (a different scope, or a
        #: ``module_name`` not seen before) just recomputes, exactly as
        #: before this cache existed -- never a correctness fallback to get
        #: right, only a speedup to get to skip.
        self._compiled_options_cache: dict = {}
        #: Per-(Hiera instance or ``h.scoped(...)`` view) function providers,
        #: keyed by ``(tag, base_path, level index)`` -- never shared with
        #: another view (see :meth:`_view`), since a provider's interpolated
        #: options are bound to exactly one scope. ``base_path``
        #: disambiguates a level index across layers (the global hierarchy
        #: and an environment's/module's each start their own indexing from
        #: 0), and ``tag`` tells a module's ``default_hierarchy`` apart from
        #: its main one (same root, a different level list). A provider
        #: already cached here has its own ``.locations`` refreshed in place
        #: on every call while ``revalidate=True`` (:meth:`_provider_for`),
        #: rather than rebuilt, so a repeated lookup on the same view still
        #: sees a changed/added/removed file.
        self._providers: dict = {}
        #: Shared with every view (like the store): the file-content
        #: cache a ``lookup_key``/``data_dig`` provider's ``LookupContext.
        #: cached_file_data`` reads through.
        self._environment_context = _EnvironmentContext()

    def clear_cache(self) -> None:
        """Drop every cached location, ``lookup_options`` mapping, glob
        listing, parsed data file, per-view function-provider state and
        pruned module data. The next lookup re-reads whatever it needs from
        disk. Safe to call while other threads are looking things up on this
        instance (or a ``.scoped(...)`` view of it, which shares every cache
        below except ``_providers``, cleared on each view separately): each
        cache clears itself under the shared lock.

        Layer/module *discovery* (``_environments``, and which ``hiera.yaml``
        each one found) is untouched -- re-reading a changed ``hiera.yaml``
        during an instance's life is out of this method's scope, same as the
        base config itself.
        """
        with self._cache_lock:
            self._generation[0] += 1
        for name in _LOCKED_CACHES:
            getattr(self, name).clear()
        self._store.clear()
        with self._cache_lock:
            for name in _PLAIN_CACHES:
                getattr(self, name).clear()

    def __getstate__(self) -> _ty.Dict[str, _ty.Any]:
        """Drop every cache and the lock they share -- a ``threading.Lock``
        is never picklable, and a freshly rebuilt, empty set of caches is a
        perfectly valid starting state for a pickle/``copy.copy``/
        ``copy.deepcopy``: the next lookup rebuilds whatever it needs,
        including re-reading (and, for sops, re-decrypting) every data file.
        """
        state = self.__dict__.copy()
        for name in _DERIVED_STATE:
            del state[name]
        return state

    def __setstate__(self, state: _ty.Dict[str, _ty.Any]) -> None:
        """Restore from :meth:`__getstate__`'s state and rebuild every
        cache empty (:meth:`_init_caches`)."""
        self.__dict__.update(state)
        self._init_caches()

    def format(self, text: str) -> Any:
        """Interpolate ``text`` against this instance's bound scope, exactly
        as a data value is interpolated (Puppet's ``Context#interpolate``).

        :param text: the string to interpolate.
        :returns: the interpolated result (a ``str``, or another Puppet
            Data value when ``text`` is a single, un-embedded ``%{...}``).
        :raises TypeError: if ``text`` is not a ``str``.
        :raises InterpolationError: if a ``%{...}`` reference or function
            call could not be resolved.
        """
        return self._format(text, self.scope)

    def _format(self, text, scope: Scope):
        if not isinstance(text, str):
            raise TypeError(
                "format() expects a str, not {}".format(type(text).__name__)
            )
        strict_token = _STRICT.set(scope.strict)
        try:
            inv = Invocation(scope, self._sub_lookup)
            with recursion_bound():
                return interpolate(text, inv)
        finally:
            _STRICT.reset(strict_token)

    def _load_config(self, backends, base_path=None):
        """Load and validate the base configuration, building hierarchy state.

        Raises :class:`ConfigError` for a missing, unreadable or invalid
        ``hiera.yaml``.
        """
        #: Allow-list of backend classes a hierarchy level's ``data_hash``
        #: may resolve to (the Backend registry, looked up by name in
        #: ``_hiera_config._build_levels``).
        self._backends: "list[type]" = list(backends)

        # Captured before reading the config: a relative version 3 datadir
        # follows the process cwd AT CONSTRUCTION (Puppet's own
        # ``Pathname(datadir)`` behavior, ``location_resolver.rb:56-66``),
        # never the cwd of a later lookup.
        cwd = Path(os.getcwd())

        source, base = _read_base_config(self.base_config, base_path)
        self._base: _ty.Dict[str, _ty.Any] = base
        self._base_path: Path = source.root
        version = _config_version(self._base, source)

        if not self._backends:
            raise ConfigError("No backends could be loaded")

        if version == 3:
            # Global-layer version 3 (or versionless) config: read and
            # validated in full against Puppet's own v3 schema. A version-3
            # config outside the global layer is never read this way -- it
            # is ignored (with a warning) or raised about by
            # :meth:`_usable` instead.
            self._hierarchy, self._default_hierarchy = _read_v3(
                self._base, source, self.scope, self._backends, self._codedir, cwd
            )
        elif version == 4:
            # Puppet validates a version 4 config's own schema (building
            # its provider list) before ever checking whether version 4 is
            # allowed in this layer -- probed: a schema-invalid version 4
            # file at the global layer raises its schema error, never this
            # one. Only a config that validates reaches the layer check.
            _read_v4(self._base, source, self.scope, self._backends)
            raise ConfigError(
                "hiera.yaml version 4 cannot be used in the global layer",
                path=source.path,
            )
        else:
            _fill_v5_defaults(self._base)
            _validate_v5(self._base, source)
            try:
                self._hierarchy, self._default_hierarchy = _build_hierarchies(
                    self._base, self._backends, source, scope=self.scope
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
        #: (:meth:`_lookup_layers`) -- the same ``self._hierarchy``/
        #: ``self._base_path``/``self._default_hierarchy`` objects, not a copy.
        self._global = _Provider(
            "Global",
            None,
            self._base_path,
            source,
            self._hierarchy,
            self._default_hierarchy,
            version,
        )
        # Puppet fails every lookup on a broken environment config; loading
        # the construction scope's own environment now gives the same
        # failure at construction instead.
        self._environment(self.scope.environment)

    def _environment(self, name):
        """The cached :class:`~hyera._config.data_provider._EnvironmentState` for
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
            load_layer_provider("Environment", root, self._backends, self.scope)
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
                "Module",
                module_dir,
                self._backends,
                self.scope,
                module_name=module_name,
            )
        cache[module_name] = result
        return result

    def _usable(self, provider, invocation):
        """A layer provider ready to be walked, or ``None``.

        ``None``/a real :class:`~hyera._config.data_provider._Provider` pass
        through unchanged. An :class:`~hyera._config.data_provider._IgnoredConfig`
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

    def _resolved_locations_for(
        self, hierarchy, index, base_path, scope, tag, invocation
    ):
        """The current, materialized locations for one hierarchy level
        (``hierarchy[index]``), or ``None`` for a location-less entry --
        used by both :meth:`_build_provider` (the first build) and
        :meth:`_provider_for` (a ``revalidate=True`` refresh of an
        already-cached provider)."""
        store = self._store
        entry = store.location_entry_for(hierarchy, base_path, scope, tag, invocation)
        resolved = store.materialize(entry, invocation)[index]
        return None if resolved is None else list(resolved)

    def _provider_for(
        self, tag, base_path, index, hierarchy, scope, invocation, module_name=None
    ):
        """The :class:`~hyera._lookup.function_provider._FunctionProvider` for one
        hierarchy level, bound to ``scope`` -- built once per ``(tag,
        base_path, index)`` on this instance/view and cached in
        ``self._providers`` (never shared with another view; see
        :meth:`_view`). While ``revalidate=True``, an already-cached
        provider has its ``.locations`` refreshed in place
        (:meth:`_resolved_locations_for`) on every call, so a repeated
        lookup on the same view/scope still sees a changed, added or
        removed location -- rebuilding the whole provider (re-interpolating
        its ``options``) would cost more than this plan's own benchmarks
        show that revalidation needs to.

        ``base_path`` -- the owning layer's own root -- disambiguates a
        level index across layers (the global hierarchy and every
        environment's/module's own each start indexing from 0) the same way
        :meth:`_LocationStore.location_entry_for`'s own cache key already does; ``tag``
        additionally tells a module's ``default_hierarchy`` apart from its
        main one, since both share the same root. ``module_name`` -- set
        only for a level in a module's own hierarchy -- makes a
        ``data_hash`` result go through :func:`~hyera._config.data_provider.
        prune_module_data` (Puppet's module-data namespace rule); it plays
        no part in the cache key, since a level's owning module never
        changes once built.
        """
        key = (tag, base_path, id(hierarchy), index)
        provider = self._providers.get(key)
        generation = self._generation[0]
        if provider is not None and provider.generation != generation:
            provider = None
        if provider is None:
            provider = self._build_provider(
                hierarchy,
                index,
                scope,
                base_path,
                tag,
                invocation,
                module_name,
                generation,
            )
            self._providers[key] = provider
        elif self._revalidate:
            provider.locations = self._resolved_locations_for(
                hierarchy, index, base_path, scope, tag, invocation
            )
        return provider

    def _build_provider(
        self,
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
        its resolved locations from :meth:`_resolved_locations_for` (shared,
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
        locations = self._resolved_locations_for(
            hierarchy, index, base_path, scope, tag, invocation
        )
        provider_cls = PROVIDER_CLASSES[level.kind]
        return provider_cls(
            level.name,
            level.backend,
            options,
            locations,
            self._environment_context,
            scope.environment,
            load_file=self._store.load_file,
            module_name=module_name,
            prune=self._pruned_module_data if module_name is not None else None,
            revalidate=self._revalidate,
            generation=generation,
        )

    def _pruned_module_data(self, module_name, data, function_name, path):
        if path is None:
            return prune_module_data(data, module_name, function_name, path)
        key = (module_name, function_name, path)
        cached = self._pruned_cache.get(key)
        # The pruned hash is valid for the very parsed hash it came from: a
        # re-read produces a new object.
        if cached is not None and cached[0] is data:
            return cached[1]
        pruned = prune_module_data(data, module_name, function_name, path)
        self._pruned_cache[key] = (data, pruned)
        return pruned

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
            provider = self._provider_for(
                tag, base_path, index, hierarchy, scope, invocation, module_name
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
                if not invocation.global_only and self._global_only_for(scope):
                    inv = invocation.derive(invocation._lookup, global_only=True)
            elif invocation.global_only:
                return _MISSING
            elif layer == "environment":
                state = self._environment(scope.environment)
                provider = self._usable(state.provider, invocation)
                inv = invocation
            else:
                # `layer` is always one of `_LAYERS` (the only caller,
                # `strategy.lookup(_LAYERS, at_layer, invocation)` below,
                # never passes anything else); "global" and "environment"
                # are already handled above, so reaching here always means
                # "module" -- never a fourth, unhandled name to check for.
                if module_name is None:
                    return _MISSING
                state = self._environment(scope.environment)
                raw = self._module_provider(state, module_name)
                if raw is None:
                    if module_name in state.modules():
                        invocation.report_module_provider_not_found(module_name)
                    else:
                        invocation.report_module_not_found(module_name)
                    return _MISSING
                provider = self._usable(raw, invocation)
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

    def _global_only_for(self, scope) -> bool:
        """Whether a lookup into the global layer's own data must stay
        confined to it (``lookup_adapter.rb:266-269``): the global layer is
        version 3, and there is no *version 5* environment provider for
        ``scope.environment`` -- an absent environment, an ignored version
        3 one, and a version 4 one all count as none (only a real
        :class:`~hyera._config.data_provider._Provider` with ``version == 5``
        disqualifies global-only)."""
        if self._global.version != 3:
            return False
        provider = self._environment(scope.environment).provider
        return not (isinstance(provider, _Provider) and provider.version == 5)

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
        has a ``default_hierarchy``, :meth:`_lookup_default_in_module` is
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

        compiled_options = memoized_options(
            invocation, "main", module_name, gather_main
        )
        options = extract_lookup_options_for_key(root, compiled_options) or {}
        explicit_merge = merge is not None
        if not explicit_merge and options.get("merge") is not None:
            invocation.report_merge_source(LOOKUP_OPTIONS)
        strategy = MergeStrategy.strategy(
            merge if explicit_merge else options.get("merge")
        )

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
                value = self._lookup_default_in_module(
                    text_key, root, segments, module_name, invocation
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

    def scoped(
        self,
        *,
        variables: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        trusted: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        server_facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        environment: _ty.Optional[str] = None,
        strict: _ty.Optional[_ty.Union[Strict, str]] = None,
        node_name: _ty.Optional[str] = None,
    ) -> "Hiera":
        """A view of this instance bound to ``self.scope.derive(...)``.

        The view is a full :class:`Hiera`, not a proxy: it shares this
        instance's config, backends and caches (the location, glob and
        file caches of its store, and ``_lookup_options_cache``), so
        every method -- ``lookup``/``()``/``[]``/``in``, ``sources()``,
        ``format()`` -- reads the derived scope instead of ``self.scope``.
        Deriving from a view derives from *its* scope, not the original.

        :param variables: node parameters, shallow-updating this scope's own.
        :param facts: facts, shallow-updating this scope's own.
        :param trusted: replaces this scope's trusted data when given.
        :param server_facts: server facts, shallow-updating this scope's own.
        :param environment: replaces this scope's ``$environment`` when given.
        :param strict: replaces this scope's strictness when given.
        :param node_name: replaces this scope's node name when given.
        :returns: the new, bound :class:`Hiera` view.
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
        # Never shared with the instance it was derived from, or with any
        # other view: a provider's interpolated options are bound to exactly
        # one scope (unlike the store and ``_lookup_options_cache``, keyed
        # on the scope value itself or not at all, and ``_environment_context``, whose file
        # cache has no scope at all).
        for name in _VIEW_OWN_STATE:
            setattr(view, name, {})
        return view

    def sources(self) -> _ty.List[str]:
        """Resolve the ordered list of source paths for this instance's
        bound scope.

        Existing files are parsed and cached and their paths returned.

        The filesystem walk (glob/iterdir/stat) is cached, keyed on the
        values of the variables the hierarchy's own interpolation reads
        (not the whole scope -- see :meth:`_LocationStore.location_entry_for`), so a merge
        lookup across many keys does not re-walk the tree for each key, and
        a scope differing only elsewhere shares the same cached walk. With
        ``revalidate=True`` (the default) each call still re-probes every
        candidate and re-lists any glob whose directory changed; with
        ``revalidate=False`` it reflects the tree as first seen for this
        scope's referenced-variable values, until :meth:`clear_cache`.

        :returns: the existing main-hierarchy ``data_hash`` file paths, in
            search order.
        """
        return self._sources(self.scope)

    def _sources(self, scope, invocation=None):
        return self._files_for(
            self._hierarchy, self._base_path, scope, "main", invocation
        )

    def _files_for(self, hierarchy, base_path, scope, tag, invocation=None):
        """The ordered list of existing, successfully loaded ``data_hash``
        file paths ``hierarchy`` visits for ``scope`` -- what ``sources()``
        shows.

        Only a ``data_hash`` level's *path* locations are ever loaded here
        (through :meth:`_LocationStore.load_file`, so they land in its file cache exactly
        as a real lookup would find them): a ``lookup_key``/``data_dig``
        function is never called without a real key, and a ``uri`` location
        is never fetched or stat'ed -- ``sources()`` keeps its documented
        meaning, "the files a lookup may read".

        Re-derived on every call, never cached as its own flattened list:
        the expensive part -- resolving/materializing locations, and
        reading each file -- is already cached the referenced-variable/
        ``(path, strict, options)`` way (:meth:`_LocationStore.location_entry_for`/
        :meth:`_LocationStore.load_file`), both shared across every view of this
        instance, so re-walking an already-cached level/location list here
        costs no repeated filesystem access beyond what ``revalidate=True``
        itself asks for.
        """
        paths = []
        for index, level in enumerate(hierarchy):
            if level.kind != "data_hash":
                continue
            provider = self._provider_for(
                tag, base_path, index, hierarchy, scope, invocation
            )
            locations = provider.locations
            if locations is None:
                continue
            for loc in locations:
                if loc.is_uri or not loc.exist:
                    continue
                path = loc.location
                self._store.load_file(
                    path, level.backend, provider.options_for(loc), invocation
                )
                if path in self._store._loaded_paths:
                    paths.append(path)
        return tuple(paths)

    def _lookup_default_in_module(self, key, root, segments, module_name, invocation):
        """Puppet's ``lookup_default_in_module``
        (``module_data_provider.rb:26-40``, ``lookup_adapter.rb:180-217``):
        a module's own ``default_hierarchy``, consulted only after the main
        stack (and its dig) misses.

        :data:`~hyera._lookup.navigation._MISSING` when ``module_name`` is
        ``None`` (an unqualified key never reaches a module's default
        hierarchy either), the module has no usable provider, or its
        ``default_hierarchy`` is empty. The merge strategy comes only from
        the default hierarchy's own ``lookup_options``
        (:func:`~hyera._lookup.lookup_adapter.module_default_lookup_options`) -- never the caller's
        ``merge=`` or the main hierarchy's options, which
        :meth:`_search_and_merge` still applies its ``convert_to`` from,
        regardless of which walk actually found the value.
        """
        if module_name is None:
            return _MISSING
        state = self._environment(invocation.scope.environment)
        provider = self._usable(self._module_provider(state, module_name), invocation)
        if provider is None or not provider.default_hierarchy:
            return _MISSING
        with invocation.recording(
            "scope", 'Searching default_hierarchy of module "{}"'.format(module_name)
        ):

            def gather_default():
                with invocation.recording("scope", 'Searching for "lookup_options"'):
                    return module_default_lookup_options(self, provider, invocation)

            compiled = memoized_options(
                invocation, "default", module_name, gather_default
            )
            entry = extract_lookup_options_for_key(root, compiled) or {}
            strategy = MergeStrategy.strategy(entry.get("merge"))
            with invocation.recording("scope", 'Searching for "{}"'.format(key)):
                with invocation.recording("data_provider", _provider_ref(provider)):
                    with invocation.check(key):
                        result = self._lookup_levels(
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

    def lookup(
        self,
        name: "_ty.Union[str, _ty.Tuple[_ty.Union[str, int], ...], _ty.Sequence[_ty.Union[str, _ty.Tuple[_ty.Union[str, int], ...]]], _ty.Mapping[str, _ty.Any]]",
        value_type: "_ty.Union[str, TypeSpec, _ty.Mapping[str, _ty.Any], None]" = None,
        merge: MergeSpec = None,
        default_value: _ty.Any = _MISSING,
        *,
        default_values_hash: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        override: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        block: _ty.Optional[_ty.Callable[..., _ty.Any]] = None,
    ) -> _ty.Any:
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

        A hyera-only extension beyond this vocabulary (not Puppet's own):
        ``name`` -- or any entry of a name ``list`` -- may be a non-empty
        ``tuple`` instead of a ``str``, treated as an exact key path:
        element 0 is the root key, every later element a dig segment
        (``str`` a hash key, ``int`` an array index, never ``bool``), each
        taken verbatim -- no dot splitting, no quote syntax, no whitespace
        stripping. ``h.lookup(("a.b", "c", 0))`` resolves exactly as
        ``h.lookup('"a.b".c.0')`` does, including paths a quoted string
        cannot spell (a segment holding both quote kinds). ``h[...]`` is
        unchanged: a tuple subscript still unpacks into ``lookup(*item)``,
        so a path there is ``h[("a.b", "c"),]``.

        :param name: the key, a tuple key path, or a list of keys/paths
            tried in order (the first one that is found, anywhere in the
            precedence order below, wins).
        :param value_type: a type object, a ``hyera.types`` class, or a
            Puppet type expression string (``"Integer"``,
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

        :returns: the found (or defaulted) value.
        :raises KeyNotFoundError: no value was found and no default was given.
        :raises HieraLookupError: a ``value_type``/``convert_to`` assertion
            failed, or resolving the key otherwise failed.
        :raises InterpolationError: a ``%{...}`` reference or function call
            in the found data could not be resolved.
        :raises MergeError: an unknown or invalid merge strategy was named.
        :raises BackendError: a data file the lookup needed could not be
            read or parsed.
        :raises TypeError: the arguments do not match one of the five call
            forms above.
        """
        call = parse_call(
            name, value_type, merge, default_value, default_values_hash, override, block
        )
        invocation = Invocation(
            self.scope,
            self._sub_lookup,
            override_values=call.override,
            default_values=call.default_values_hash,
            explainer=_debug_explainer(),
        )
        with recursion_bound():
            return _lookup_call(call, invocation, self._search_and_merge)

    __call__ = lookup

    def __getitem__(self, item: _ty.Any) -> _ty.Any:
        """``h[key]``/``h[key, *args]``/``h[key, {options}]``: the same
        five call forms as :meth:`lookup`, unpacking a tuple subscript into
        positional arguments -- unchanged by :meth:`lookup`'s own tuple key
        path extension, so a path here is written ``h[("a.b", "c"),]``
        (one positional argument, itself a tuple).

        :param item: a single argument (the ``name``), or a tuple of the
            positional/dict arguments :meth:`lookup` accepts.
        :returns: the found (or defaulted) value.
        :raises KeyNotFoundError: no value was found and no default was given.
        """
        if isinstance(item, tuple):
            return self.lookup(*item)
        return self.lookup(item)

    def __contains__(self, name: _ty.Any) -> bool:
        """``name in h``: whether :meth:`lookup` finds a value for
        ``name`` (any form :meth:`lookup` accepts).

        :param name: the same ``name`` argument :meth:`lookup` accepts.
        :returns: ``True`` if a value was found, ``False`` on a miss.
        """
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
        *keys: _ty.Any,
        value_type: "_ty.Union[str, TypeSpec, None]" = None,
        merge: MergeSpec = None,
        default_values_hash: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        override: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    ) -> _ty.Any:
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

        :param keys: the root key -- a lookup key, parsed as ``.lookup()``
            parses one (``dig("a.b", 0)`` looks up ``a.b``) -- then each
            key/index to dig into the result, used exactly as given.
        :param value_type: a type object, a ``hyera.types`` class, or a
            Puppet type expression string, asserted against the final
            result.
        :param merge: the root lookup's merge strategy.
        :param default_values_hash: consulted for the root key only after
            the hierarchy itself missed it.
        :param override: consulted for the root key before the hierarchy.
        :returns: the dug-out value, or ``None`` on a root miss.
        :raises TypeError: fewer than one key was given, the first is not a
            ``str``, or ``value_type`` is not a type spec.
        :raises HieraLookupError: a key after the first does not fit the
            value found there (a non-``int`` against a ``list``, or any key
            against a non-collection).
        """
        if not keys or not isinstance(keys[0], str):
            raise TypeError("dig() needs at least one key, the first a str")
        parsed_type = check_call("dig", value_type, None)
        root = self.lookup(
            keys[0],
            None,
            merge,
            None,
            default_values_hash=default_values_hash,
            override=override,
        )
        with recursion_bound():
            result = _data_functions.dig(root, keys[1:])
            if parsed_type is not None:
                assert_instance_of("Found value", parsed_type, result)
        return result

    def get(
        self,
        dotted: str,
        default_value: _ty.Any = None,
        block: _ty.Optional[_ty.Callable[..., _ty.Any]] = None,
        *,
        value_type: "_ty.Union[str, TypeSpec, None]" = None,
        merge: MergeSpec = None,
        default_values_hash: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        override: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    ) -> _ty.Any:
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

        :param dotted: a Puppet dotted-navigation string (``"a.b.0"``;
            quoted segments, numeric segments index arrays unless quoted).
        :param default_value: returned on a root miss or a found ``None``.
        :param block: called with the navigation error when a later
            segment cannot be dug out; its return value is used instead of
            raising.
        :param value_type: a type object, a ``hyera.types`` class, or a
            Puppet type expression string, asserted against the final
            result.
        :param merge: the root lookup's merge strategy.
        :param default_values_hash: consulted for the root key only after
            the hierarchy itself missed it.
        :param override: consulted for the root key before the hierarchy.
        :returns: the dug-out value, or ``default_value``.
        :raises TypeError: ``dotted`` is not a ``str``, ``block`` is not
            callable, or ``value_type`` is not a type spec.
        :raises HieraLookupError: ``dotted`` is empty or malformed, or a
            navigation error was reached with no ``block``.
        """
        if not isinstance(dotted, str):
            raise TypeError(
                "get() dotted key must be a str, not {}".format(type(dotted).__name__)
            )
        parsed_type = check_call("get", value_type, block)
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
                explainer=_debug_explainer(),
            )

            def search(name, inv, m, _root=root):
                # Puppet's own `get()` looks up the root by its *segment*
                # form directly (never re-parsed): a quoted root such as
                # `'"a.b".c'` must not have its own embedded dot split
                # again by a second `parse_lookup_key` pass.
                return self._search_and_merge(name, inv, m, parsed=(_root, ()))

            with recursion_bound():
                root_value = _lookup_call(call, invocation, search)
        with recursion_bound():
            result, subject = _data_functions.get_segments(
                root_value, segments[1:], default_value, block
            )
            if parsed_type is not None:
                assert_instance_of(subject, parsed_type, result)
        return result

    def getvar(
        self,
        dotted: str,
        default_value: _ty.Any = None,
        block: _ty.Optional[_ty.Callable[..., _ty.Any]] = None,
    ) -> _ty.Any:
        """Puppet's ``getvar()`` (``functions/getvar.rb``): Puppet's
        ``get()`` over a scope variable's value instead of a looked-up one.

        ``dotted`` must start with a valid (optionally ``::``-qualified)
        Puppet variable name, immediately followed by ``.`` or the string's
        end, else ``HieraLookupError``. An undefined variable returns
        ``default_value`` regardless of the bound scope's ``strict`` --
        Puppet's own ``catch(:undefined_variable)``, never a raise for that
        reason alone. The rest navigates exactly as `.get()` does.

        :param dotted: a top-scope variable name, optionally followed by a
            Puppet dotted-navigation path into its value.
        :param default_value: returned when the variable is undefined, or
            a later segment cannot be dug out with no ``block``.
        :param block: called with the navigation error when a segment
            after the variable cannot be dug out; its return value is used
            instead of raising.
        :returns: the dug-out value, or ``default_value``.
        :raises TypeError: ``dotted`` is not a ``str``, or ``block`` is not
            callable.
        :raises HieraLookupError: ``dotted`` does not start with a valid
            variable name, or a navigation error was reached with no
            ``block``.
        """
        if not isinstance(dotted, str):
            raise TypeError(
                "getvar(): dotted must be a str, not {}".format(type(dotted).__name__)
            )
        if block is not None and not callable(block):
            raise TypeError("getvar(): block must be callable")
        with recursion_bound():
            return unshare(
                _data_functions.getvar(self.scope, dotted, default_value, block)
            )

    def explain(
        self,
        name: "_ty.Union[str, _ty.Tuple[_ty.Union[str, int], ...], _ty.Sequence[_ty.Union[str, _ty.Tuple[_ty.Union[str, int], ...]]], _ty.Mapping[str, _ty.Any]]",
        value_type: "_ty.Union[str, TypeSpec, _ty.Mapping[str, _ty.Any], None]" = None,
        merge: MergeSpec = None,
        default_value: _ty.Any = _MISSING,
        *,
        default_values_hash: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        override: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        block: _ty.Optional[_ty.Callable[..., _ty.Any]] = None,
        explain_options: bool = False,
    ) -> ExplainResult:
        """What ``puppet lookup --explain``/``--explain-options`` shows:
        every hierarchy entry and path consulted for `.lookup()`, and
        whether ``name`` was found there.

        Takes exactly `.lookup()`'s own signature and dispatcher (the same
        five call forms, the same keyword spellings), plus one keyword-only
        ``explain_options``: mirroring ``--explain-options``, it reports
        only how ``lookup_options`` was assembled for ``name`` (its own
        module, if any) -- ``True`` with ``explain_options`` also true is
        Puppet's ``--explain --explain-options``, byte-identical to
        ``explain_options=False`` (measured).

        Returns an :class:`~hyera.ExplainResult` regardless of outcome:
        ``.text()`` is the indented report, ``.to_hash()`` the same tree
        with Puppet's own keys. ``.error`` is the :class:`~hyera.HieraError`
        the lookup ended with -- a miss (:class:`~hyera.KeyNotFoundError`),
        an invalid ``lookup_options`` value, a failed ``convert_to``, an
        interpolation syntax error, or a lookup error raised from
        *environment or module* data -- reported as the report's own last
        line, exactly as ``puppet lookup --explain`` prints some errors
        instead of raising. Any other error (a ``--type`` mismatch, or a
        lookup error left unhandled by the *global* layer's own data)
        raises instead, same as `.lookup()`; a :class:`~hyera.ConfigError`
        always raises, even where ``puppet lookup --explain`` would print
        it as its own last line -- our configs are read before the lookup
        starts, so there is never one to report mid-lookup.

        :param name: as :meth:`lookup`.
        :param value_type: as :meth:`lookup`.
        :param merge: as :meth:`lookup`.
        :param default_value: as :meth:`lookup`.
        :param default_values_hash: as :meth:`lookup`.
        :param override: as :meth:`lookup`.
        :param block: as :meth:`lookup`.
        :param explain_options: report only how ``lookup_options`` was
            assembled, instead of the full search.
        :returns: the explain report, alongside the outcome.
        :raises HieraLookupError: a ``value_type``/``convert_to`` assertion
            failed, or resolving the key otherwise failed outside the
            global/environment/module data itself.
        :raises ConfigError: the base configuration is invalid (never
            reachable mid-lookup, but kept for parity with ``lookup()``).
        :raises TypeError: the arguments do not match one of the five call
            forms `.lookup()` accepts.
        """
        call = parse_call(
            name, value_type, merge, default_value, default_values_hash, override, block
        )
        explainer = Explainer(explain_options, explain_options)
        lo_memo = _ExplainOptionsMemo(
            _ScopeKeyedCache(threading.Lock(), self._cache_size)
        )
        invocation = Invocation(
            self.scope,
            self._sub_lookup,
            override_values=call.override,
            default_values=call.default_values_hash,
            explainer=_debug_explainer(explainer),
            _lo_cache=lo_memo,
        )
        error = None
        try:
            if invocation.only_explain_options:
                # lookup_adapter.rb:61-63: look up the literal key
                # "lookup_options" through the very same layer stack an
                # ordinary key would use (never `retrieve_lookup_options`'s
                # own hand-composed combining, which never builds a `merge`
                # explain node) -- swallow whatever it finds or misses.
                first_name = call.names[0] if call.names else None
                first_root = (
                    first_name[0] if isinstance(first_name, tuple) else first_name
                )
                module_name = module_name_of(first_root) if first_root else None
                self._lookup_layers(
                    LOOKUP_OPTIONS,
                    module_name,
                    invocation,
                    MergeStrategy.strategy("hash"),
                )
            else:
                _lookup_call(call, invocation, self._search_and_merge)
        except RecursionError as exc:
            raise depth_error(exc) from None
        except BackendError:
            # A data file that cannot be read or parsed is a data/
            # infrastructure problem, not one of Puppet's own reportable
            # LookupErrors -- it always escapes, wherever it was raised
            # from, same as an ordinary `.lookup()` never catches it.
            raise
        except HieraLookupError as e:
            if getattr(e, "_explain_escape", False):
                raise
            invocation.report_text(lambda: str(e))
            error = e
        return ExplainResult(explainer, error)
