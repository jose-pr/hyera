# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/pops/lookup/data_hash_function_provider.rb,
# data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import json
import logging
import os
import threading
import typing as _ty
from typing import Any

from . import _data_functions
from ._cache import _LRU, _FileEntry, _ScopeKeyedCache, _probe
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
from ._function_provider import PROVIDER_CLASSES, _EnvironmentContext
from ._interpolation import interpolate
from ._invocation import _STRICT, Invocation
from ._location_resolver import glob as _dir_glob
from ._location_resolver import resolve_glob_specs, resolve_locations
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

#: Sentinel distinguishing "no location in this layer's own hierarchy
#: declares ``lookup_options`` at all" from an explicit ``lookup_options: ~``
#: (``None``) -- needed because :class:`~hyera._cache._ScopeKeyedCache`
#: already uses :data:`~hyera._navigation._MISSING` to mean "not cached yet"
#: (:meth:`Hiera._layer_options_cached`).
_LO_ABSENT = object()


def _no_option_lookup(key, invocation):
    """The ``lookup`` callable for a hierarchy level's ``options``
    :class:`~hyera._invocation.Invocation`. Unreachable in practice: options
    interpolate with ``allow_methods=False``, which rejects every method
    call (``%{hiera()}``/``%{lookup()}``/``%{alias()}``) -- the only way a
    sub-lookup would ever be attempted -- before it could reach this
    callable."""
    raise RuntimeError("hierarchy options never perform a sub-lookup")


class _Location(_ty.NamedTuple):
    """One resolved, non-glob hierarchy location: Puppet's
    ``ResolvedLocation`` (``original``, ``location``, ``is_uri``, ``exist``),
    with ``location`` always an interned ``str`` for a plain path (never a
    ``Path``: a consumer that needs one builds it from ``.location``) or the
    normalized ``uri`` string when ``is_uri``. Field names match
    :class:`~hyera._location_resolver.ResolvedLocation` exactly, so a
    :class:`~hyera._function_provider._FunctionProvider` (built from either
    kind) never has to tell them apart.
    """

    original: str
    location: str
    is_uri: bool
    exist: bool


class _GlobLocation(_ty.NamedTuple):
    """One ``glob``/``globs`` declared string, resolved and rooted but not
    yet walked -- stored in a :class:`_LocationEntry` in place of a
    :class:`_Location`; :meth:`Hiera._materialize` expands it into
    :class:`_Location` matches at lookup time (:meth:`Hiera._glob_matches`),
    never during the hierarchy build itself."""

    original: str
    root: str
    pattern: str


class _LocationEntry:
    """One cached, resolved hierarchy: ``key`` is this entry's own
    :class:`~hyera._cache._ScopeKeyedCache` key (reused as the
    ``lookup_options`` cache's ``extra``, so a ``lookup_options`` entry is
    invalidated whenever its locations are); ``levels`` is one resolved
    locations tuple -- or ``None`` for a location-less entry -- per
    hierarchy level, aligned by index with the ``hierarchy`` list it was
    built from, each tuple holding :class:`_Location`/:class:`_GlobLocation`.
    ``materialized`` caches this entry's own lookup-time expansion (globs
    walked, ``exist`` refreshed) when ``revalidate=False``, so it is
    computed at most once per entry for the instance's life; ``None`` until
    then, and always ``None`` (never read) when ``revalidate=True``, which
    recomputes it fresh every lookup. A plain mutable class, not a
    ``NamedTuple``: ``materialized`` is the one field a cached entry updates
    in place after it was already stored.
    """

    __slots__ = ("key", "levels", "materialized")

    def __init__(self, key, levels):
        self.key = key
        self.levels = levels
        self.materialized = None


class _GlobEntry(_ty.NamedTuple):
    """One cached glob listing: ``matches`` is the interned-string tuple of
    matched files (directories already dropped); ``dirs`` is the
    ``((directory, (st_ino, st_mtime_ns)), ...)`` pairs the walk actually
    called ``os.scandir`` on, used to decide whether the listing is still
    fresh (``Hiera._glob_matches``, ``revalidate=True``) without re-walking
    unless one of them changed."""

    matches: tuple
    dirs: tuple


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
        cache_size=256,
        revalidate=True,
    ):
        self.base_config = base_config
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
        self.cache_size = cache_size
        if not isinstance(revalidate, bool):
            raise TypeError(
                "revalidate must be a bool, not {}".format(type(revalidate).__name__)
            )
        self.revalidate = revalidate

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
        #: ``_file_cache`` a global/environment read of the same file uses.
        self._pruned_cache: dict = {}
        #: ``module_name -> (scope, compiled)``: the single most recent
        #: ``_retrieve_lookup_options`` result for that ``module_name``, an
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

        self.hierarchy: "list[HieraLevel]" = []
        self.default_hierarchy: "list[HieraLevel]" = []
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
        #: Shared with every view (like ``_file_cache``): the file-content
        #: cache a ``lookup_key``/``data_dig`` provider's ``LookupContext.
        #: cached_file_data`` reads through.
        self._environment_context = _EnvironmentContext()

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
        #: Resolved hierarchy locations for one ``(tag, base_path)`` layer,
        #: keyed on the values of the variables their own interpolation
        #: reads (``_cache.py``), not on the whole scope -- see
        #: ``_location_entry_for``. Shared by every view derived from this
        #: instance (unlike ``_providers`` above, see ``_view``), since
        #: ``base_path`` -- the owning layer's own root -- disambiguates a
        #: layer's hierarchy from any other's, the same way ``_providers``'
        #: own cache key already does.
        self._location_cache = _ScopeKeyedCache(self._cache_lock, self.cache_size)
        #: The ``lookup_options`` value gathered from one layer's own
        #: hierarchy alone (never merged across layers), keyed the same way,
        #: plus the location entry it was built against. See
        #: ``_layer_options_cached``.
        self._lookup_options_cache = _ScopeKeyedCache(self._cache_lock, self.cache_size)
        #: ``(scope, tag, base_path)`` tuples whose ``lookup_options`` gather
        #: is currently running on this instance -- guards against a value
        #: inside ``lookup_options`` that sub-looks-up a key whose own
        #: ``lookup_options`` gather would otherwise re-enter this same
        #: gather while it is still running (a merge spec written as
        #: ``%{lookup(...)}``). A reentrant call sees no options at all
        #: (matching a key with none), never recurses, and the pending
        #: marker is removed in ``finally`` so a gather that raises does not
        #: wedge future lookups.
        self._lookup_options_pending: set = set()
        #: Interned path strings, shared by every location entry and by
        #: ``_file_cache``: ``s -> s`` so equal paths from independent
        #: builds share one string object.
        self._paths: dict = {}
        #: Parsed data files: ``(path, backend.strict, options) ->
        #: _FileEntry``. See ``_load_file``. Unbounded, as Puppet's own
        #: per-environment file cache is (its size follows the data tree,
        #: not the number of scopes seen) -- only ``clear_cache()`` empties
        #: it.
        self._file_cache: dict = {}
        #: Every plain path ever loaded successfully into ``_file_cache``,
        #: under any ``strict``/``options`` variant -- what ``_files_for``
        #: consults to tell a loaded location from a missing/unattempted one.
        self._loaded_paths: set = set()
        #: Glob listings: ``(root, pattern) -> _GlobEntry``, bounded like the
        #: two scope-keyed caches (a listing depends on directory contents,
        #: not on scope, so a plain ``_LRU`` is enough). See
        #: :meth:`_glob_matches`.
        self._glob_cache = _LRU(self._cache_lock, self.cache_size)

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
        self._location_cache.clear()
        self._lookup_options_cache.clear()
        self._glob_cache.clear()
        with self._cache_lock:
            self._file_cache.clear()
            self._loaded_paths.clear()
            self._paths.clear()
        self._pruned_cache.clear()
        self._compiled_options_cache.clear()
        self._providers.clear()
        self._environment_context.clear()

    def __getstate__(self):
        """Drop every cache and the lock they share -- a ``threading.Lock``
        is never picklable, and a freshly rebuilt, empty set of caches is a
        perfectly valid starting state for a pickle/``copy.copy``/
        ``copy.deepcopy``: the next lookup rebuilds whatever it needs,
        including re-reading (and, for sops, re-decrypting) every data file.
        """
        state = self.__dict__.copy()
        for name in (
            "_cache_lock",
            "_location_cache",
            "_lookup_options_cache",
            "_lookup_options_pending",
            "_paths",
            "_file_cache",
            "_glob_cache",
        ):
            del state[name]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._init_caches()

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
        """Resolve the bound scope's own locations and read every main
        hierarchy file up front, same as a lookup would need to.

        Mirrors the source-resolution side effects of a ``get(None)`` call
        without going through the public API's key-type check. Uses a
        one-off probe per location (no shared :attr:`~hyera._invocation.
        Invocation._fs_memo`, since there is no top-level lookup to share
        one with): every existing ``data_hash`` file the main hierarchy
        visits is still read via :meth:`_load_file` (:meth:`_files_for`),
        which is what makes a malformed data file fail construction itself,
        not just the first lookup.

        A malformed dotted reference or a navigation type mismatch
        reachable while resolving a hierarchy path (``%{...}`` in a
        ``path``/``paths``/``glob``/``mapped_paths`` template) is swallowed
        here and logged at debug level, not raised out of the constructor:
        Puppet raises these at lookup time, never at construction, and a
        constructor should fail only for configuration errors. Swallowing it
        here also means the walk this aborts was never cached, so the first
        real lookup retries it in full and raises the same error again, now
        at the right time.

        Runs under ``self.scope.strict`` (the ``_STRICT`` ContextVar, same as
        ``lookup``/``dig``/``get``): a genuinely non-hash data file under
        ``strict="error"`` raises here as :class:`~hyera.BackendError` and is
        NOT caught by the except clause below (matching the documented
        constructor contract -- a data file that cannot be read or parsed
        can fail construction itself).
        """
        strict_token = _STRICT.set(self.scope.strict)
        try:
            self._sources(self.scope)
        except (HieraLookupError, InterpolationError) as e:
            _LOGGER.debug("Pre-warm skipped after a lookup-time error: %s", e)
        finally:
            _STRICT.reset(strict_token)

    def _intern(self, p) -> str:
        """The canonical interned ``str`` for a path-like ``p``: equal paths
        from independent hierarchy builds (and independent glob matches)
        share one string object, which is what makes a :class:`_Location`
        cheap to hold in every cache entry that resolves to it."""
        s = os.fspath(p)
        return self._paths.setdefault(s, s)

    def _load_file(self, path, backend, options, invocation=None):
        """Load ``path`` via ``backend.data_hash(path, options)``, returning
        the parsed, cached data, or :data:`~hyera._navigation._MISSING` when
        ``revalidate=True`` and ``path`` has vanished since it was last
        cached (a materialized location whose ``exist`` was true earlier in
        this same lookup, per its own memoized probe, but no longer is --
        caught here rather than treated as a read error, the same way an
        always-absent location is).

        With ``revalidate=True`` (the default), one probe -- through
        ``invocation``'s memo when given, a fresh one-off otherwise -- either
        confirms a cached parse is still current (its signature unchanged)
        or triggers a re-read, logged at debug level; ``invocation=None``
        (``sources()``, the constructor's own pre-warm) still revalidates,
        just without sharing the probe with anything else. With
        ``revalidate=False``, a cached entry is returned untouched, and a
        first read is cached with no signature at all, so it is never
        reconsidered short of :meth:`clear_cache`.

        A read failure (``OSError``, e.g. the file vanished between the
        directory walk and here) becomes ``Unable to read (<path>): ...``; a
        parse failure (a :class:`BackendError` without ``.path`` set --
        ``Backend.load`` already sets it) becomes ``Unable to parse
        (<path>): ...``; any other non-:class:`HieraError` exception is
        wrapped the same way, naming its type. An already-pathed
        ``BackendError`` (or any other :class:`HieraError`) propagates
        unchanged. There is no directory check here any more: a location
        that is a directory is caught once, when it is resolved/materialized
        (:meth:`_location_entry_for`/:meth:`_materialize`/
        :meth:`_require_not_dir`), never reaching this method at all --
        repeating the check here would just be a second probe of the same
        path for the same answer.

        Puppet's own Hash check on the result
        (``data_hash_function_provider.rb:70-76``) runs here too, so every
        backend -- third-party ones included -- gets it.

        Cached per ``(path, backend.strict, options)``, not per bare
        ``path``: a data file's own non-hash rule (``YAMLBackend.
        _as_data_hash``'s ``strict``-sensitive raise-or-warn) must run again
        for a call whose effective ``strict`` differs from a previous one,
        never reuse a result computed under a different strictness;
        ``options`` joins the key too, so a cache hit never skips a file
        function's own options check (``Backend._require_path_only``) --
        ``options`` is Puppet ``Data``, so it always serializes.
        ``self._loaded_paths`` separately tracks which plain paths were ever
        read successfully, for :meth:`_files_for`'s "was this location
        loaded" check, independent of which ``strict``/``options`` variant
        did the loading. This is the only place a location is actually
        read: a hierarchy build only resolves and records locations now, so
        every location -- even one visited many times across many lookups --
        is parsed here at most once per probe-confirmed version.
        """
        options_key = json.dumps(options, sort_keys=True)
        cache_key = (path, backend.strict, options_key)
        probe = None
        with self._cache_lock:
            entry = self._file_cache.get(cache_key)

        if self.revalidate:
            probe = (
                invocation._memo_probe(path) if invocation is not None else _probe(path)
            )
            if probe.kind == "absent":
                if entry is not None:
                    # A path that was cached (successfully read before) and
                    # has since vanished reads as absent, as Puppet's next
                    # compilation would see it -- never an error for that
                    # alone.
                    with self._cache_lock:
                        self._file_cache.pop(cache_key, None)
                        self._loaded_paths.discard(path)
                    return _MISSING
                # Never cached, and materialization still says this is a
                # location to read (a glob match's own `exist` is not a
                # fresh probe result -- a dangling symlink matches by name
                # like any other file): attempt the read and let it fail
                # naturally, exactly as it would have with no revalidation
                # at all.
            elif entry is not None and entry.signature == probe.sig:
                return entry.data
        elif entry is not None:
            return entry.data

        try:
            data = backend.data_hash(path, dict(options))
        except BackendError as e:
            if e.path is None:
                raise BackendError(
                    "Unable to parse ({}): {}".format(path, e), path=str(path)
                ) from e
            raise
        except HieraError:
            raise
        except OSError as e:
            raise BackendError(
                "Unable to read ({}): {}".format(path, e.strerror or e), path=str(path)
            ) from e
        except Exception as e:
            raise BackendError(
                "Unable to parse ({}): {}: {}".format(path, type(e).__name__, e),
                path=str(path),
            ) from e

        _validate_data_hash(data, backend.name, path)
        if entry is not None:
            _LOGGER.debug("File at '%s' was changed, reloading", path)
        with self._cache_lock:
            self._file_cache[cache_key] = _FileEntry(probe.sig if probe else None, data)
            self._loaded_paths.add(path)
        return data

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

    def _location_entry_for(
        self, hierarchy, base_path, scope, tag, invocation=None
    ) -> _LocationEntry:
        """The cached :class:`_LocationEntry` for one layer's ``hierarchy``
        -- valid for every scope that reads the same values from the
        variables the hierarchy's own interpolation reads (Puppet's
        ``scope_interpolations_stable?``), not just the exact scope it was
        built for.

        Shared by every view derived from this instance (unlike
        ``self._providers``, see :meth:`_view`): ``base_path`` -- the owning
        layer's own root -- disambiguates a layer's hierarchy from any
        other's the same way :meth:`_provider_for`'s own cache key already
        does, so two providers never collide even under the same ``tag``.

        This only resolves locations, and probes each non-glob, non-uri one
        (through ``invocation``'s filesystem memo when given, else a
        one-off probe) to tell a directory -- which raises
        :class:`~hyera.BackendError` right here, the same way a later lookup
        that finds one would -- from an existing or absent plain location;
        it never reads a file's *content* (:meth:`_load_file` is what a
        ``data_hash`` provider calls, lazily, the first time a location's
        data is actually needed). Building an entry is therefore
        ``strict``-independent: two scopes reading the same variables share
        one entry regardless of ``strict``, and a data file's own
        strict-sensitive non-hash rule always sees whichever ``strict`` the
        lookup that actually reads it is running under.

        A ``glob``/``globs`` level's declared strings are only interpolated
        and rooted here (:class:`_GlobLocation`); the actual match listing
        is deferred to :meth:`_materialize`, once per lookup, so a file
        added to (or removed from) a glob-matched directory is seen by a
        later lookup even when this entry itself is reused unchanged. A
        hierarchy entry with no location key at all resolves to ``None``
        (:func:`~hyera._location_resolver.resolve_locations`), distinct from
        one that resolves to zero candidates.
        """
        kind = ("locations", tag, base_path)
        cached = self._location_cache.get(kind, scope)
        if cached is not _MISSING:
            return cached

        fs_memo = invocation._fs_memo if invocation is not None else None
        refs = []
        levels = []
        for level in hierarchy:
            if level.location_key in ("glob", "globs"):
                specs = resolve_glob_specs(level, base_path, scope, refs, fs_memo)
                locations = tuple(
                    _GlobLocation(spec.original, spec.root, spec.pattern)
                    for spec in specs
                )
            else:
                resolved = resolve_locations(level, base_path, scope, refs, fs_memo)
                if resolved is None:
                    locations = None
                else:
                    built = []
                    for loc in resolved:
                        if loc.is_uri:
                            built.append(
                                _Location(loc.original, loc.location, True, True)
                            )
                        else:
                            path = self._intern(loc.location)
                            built.append(
                                _Location(
                                    loc.original,
                                    path,
                                    False,
                                    self._require_not_dir(path, invocation),
                                )
                            )
                    locations = tuple(built)
            levels.append(locations)
        key = self._location_cache.key_for(kind, refs)
        entry = _LocationEntry(key, tuple(levels))
        self._location_cache.put(key, entry)
        return entry

    def _require_not_dir(self, path: str, invocation) -> bool:
        """The memo-aware probe every plain (non-glob, non-uri) location
        goes through, both at build time and, with ``revalidate=True``,
        again at every materialization: ``True`` if ``path`` is currently a
        regular file, ``False`` if absent, :class:`~hyera.BackendError`
        ("Is a directory") if it is one -- caught here, once, rather than
        left for :meth:`_load_file` to rediscover with a second, redundant
        probe.
        """
        probe = invocation._memo_probe(path) if invocation is not None else _probe(path)
        if probe.kind == "dir":
            raise BackendError(
                "Unable to read ({}): Is a directory".format(path), path=path
            )
        return probe.kind == "file"

    def _materialize(self, entry: _LocationEntry, invocation):
        """This entry's ``levels``, with every :class:`_GlobLocation`
        expanded into its current file matches (:meth:`_glob_matches`) and,
        with ``revalidate=True``, every plain location's ``exist`` refreshed
        through a fresh probe -- Puppet re-globs and re-checks on every
        compilation, and a library has no compilation of its own, so one
        top-level lookup is the unit that gets one filesystem snapshot. A
        ``uri`` location is never probed at all (Puppet never fetches or
        stats one; it always "exists").

        With ``revalidate=False`` this is computed only the first time for
        a given entry and cached on it (``entry.materialized``), exactly as
        first seen, until :meth:`clear_cache`.
        """
        if not self.revalidate and entry.materialized is not None:
            return entry.materialized

        levels = []
        for locations in entry.levels:
            if locations is None:
                levels.append(None)
                continue
            resolved = []
            for loc in locations:
                if isinstance(loc, _GlobLocation):
                    for match in self._glob_matches(loc.root, loc.pattern, invocation):
                        resolved.append(_Location(loc.original, match, False, True))
                elif loc.is_uri:
                    resolved.append(loc)
                elif self.revalidate:
                    exist = self._require_not_dir(loc.location, invocation)
                    resolved.append(_Location(loc.original, loc.location, False, exist))
                else:
                    resolved.append(loc)
            levels.append(tuple(resolved))
        materialized = tuple(levels)
        if not self.revalidate:
            entry.materialized = materialized
        return materialized

    def _glob_matches(self, root: str, pattern: str, invocation) -> tuple:
        """The current file matches for one rooted glob pattern, memoized by
        ``(root, pattern)`` in ``self._glob_cache`` (never by scope: a
        listing depends on directory contents alone).

        With ``revalidate=True``, a cached listing is reused only while
        every directory the walk actually scanned still probes to the same
        ``(st_ino, st_mtime_ns)`` pair it did when listed; otherwise (or on
        a first call) the walker re-lists, recording exactly the
        directories it scans this time via ``on_scandir``. A matched
        directory is dropped the same memo-aware way a plain location's
        existence is checked, so a match already probed while listing is
        never probed again by :meth:`_load_file`. With ``revalidate=False``
        a cached listing is reused unconditionally.
        """
        key = (root, pattern)
        cached = self._glob_cache.get(key)
        if cached is not _MISSING:
            if not self.revalidate:
                return cached.matches
            fresh = True
            for d, sig in cached.dirs:
                probe = (
                    invocation._memo_probe(d) if invocation is not None else _probe(d)
                )
                if probe.kind != "dir" or probe.sig[:2] != sig:
                    fresh = False
                    break
            if fresh:
                return cached.matches

        dirs = []

        def on_scandir(d):
            probe = invocation._memo_probe(d) if invocation is not None else _probe(d)
            if probe.kind == "dir":
                dirs.append((d, probe.sig[:2]))

        def probe_isdir(d):
            probe = invocation._memo_probe(d) if invocation is not None else _probe(d)
            return probe.kind == "dir"

        raw = _dir_glob(
            root,
            pattern,
            on_scandir if self.revalidate else None,
            probe_isdir if self.revalidate else None,
        )
        matches = []
        for m in raw:
            # Only a directory is dropped here (``reject(&:directory?)``,
            # as the eager `_expand_globs` does) -- a dangling symlink match
            # is kept, exactly like the eager path: `_load_file` is what
            # raises for it, when something actually tries to read it, not
            # this listing step.
            if self.revalidate:
                probe = (
                    invocation._memo_probe(m) if invocation is not None else _probe(m)
                )
                is_dir = probe.kind == "dir"
            else:
                is_dir = os.path.isdir(m)
            if not is_dir:
                matches.append(self._intern(m))
        matches = tuple(matches)
        self._glob_cache.put(key, _GlobEntry(matches, tuple(dirs)))
        return matches

    def _resolved_locations_for(
        self, hierarchy, index, base_path, scope, tag, invocation
    ):
        """The current, materialized locations for one hierarchy level
        (``hierarchy[index]``), or ``None`` for a location-less entry --
        used by both :meth:`_build_provider` (the first build) and
        :meth:`_provider_for` (a ``revalidate=True`` refresh of an
        already-cached provider)."""
        entry = self._location_entry_for(hierarchy, base_path, scope, tag, invocation)
        materialized = self._materialize(entry, invocation)
        resolved = materialized[index]
        return None if resolved is None else list(resolved)

    def _provider_for(
        self, tag, base_path, index, hierarchy, scope, invocation, module_name=None
    ):
        """The :class:`~hyera._function_provider._FunctionProvider` for one
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
        :meth:`_location_entry_for`'s own cache key already does; ``tag``
        additionally tells a module's ``default_hierarchy`` apart from its
        main one, since both share the same root. ``module_name`` -- set
        only for a level in a module's own hierarchy -- makes a
        ``data_hash`` result go through :func:`~hyera._data_provider.
        prune_module_data` (Puppet's module-data namespace rule); it plays
        no part in the cache key, since a level's owning module never
        changes once built.
        """
        key = (tag, base_path, index)
        provider = self._providers.get(key)
        if provider is None:
            provider = self._build_provider(
                hierarchy, index, scope, base_path, tag, invocation, module_name
            )
            self._providers[key] = provider
        elif self.revalidate:
            provider.locations = self._resolved_locations_for(
                hierarchy, index, base_path, scope, tag, invocation
            )
        return provider

    def _build_provider(
        self, hierarchy, index, scope, base_path, tag, invocation, module_name=None
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
        prune = None
        if module_name is not None:

            def prune(data, function_name, path, _mod=module_name):
                return self._pruned_module_data(data, _mod, function_name, path)

        return provider_cls(
            level.name,
            level.backend,
            options,
            locations,
            self._environment_context,
            scope.environment,
            load_file=self._load_file,
            prune=prune,
            revalidate=self.revalidate,
        )

    def _pruned_module_data(self, data, module_name, function_name, path):
        key = (module_name, path)
        pruned = self._pruned_cache.get(key)
        if pruned is None:
            pruned = prune_module_data(data, module_name, function_name, path)
            self._pruned_cache[key] = pruned
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
        (``self.hierarchy``/``self.base_path`` for the global layer, or a
        :class:`~hyera._data_provider._Provider`'s own ``hierarchy``/
        ``root``); ``tag`` names which of that layer's hierarchies (its
        main one, or -- for a module -- its ``default_hierarchy``), for
        provider caching (:meth:`_provider_for`). ``strategy`` is an
        already resolved :class:`~hyera._merge_strategy.MergeStrategy`.
        Returns the merged root value, or :data:`~hyera._navigation._MISSING`
        on a miss.

        ``module_name``, when given, is the level's owning module: every
        ``data_hash`` result is read through :func:`~hyera._data_provider.
        prune_module_data` first (Puppet's module-data namespace rule),
        cached per ``(module_name, path)`` apart from ``_file_cache``'s own
        unpruned entry -- a file shared with the global layer stays unpruned
        there. A ``lookup_key``/``data_dig`` result is never pruned (Puppet
        prunes only a ``data_hash`` function's return value,
        ``data_hash_function_provider.rb:72``).

        A found value that is ``None`` (data explicitly set to ``~``) is a
        genuine value, not a miss: only an absent root key is.
        """

        def at_level(entry):
            index, _level = entry
            provider = self._provider_for(
                tag, base_path, index, hierarchy, scope, invocation, module_name
            )
            return provider.key_lookup(root, segments, invocation, strategy)

        return strategy.lookup(list(enumerate(hierarchy)), at_level)

    def _lookup_layers(self, root, module_name, invocation, strategy, segments=()):
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
        scope = invocation.scope

        def at_layer(layer):
            if layer == "global":
                provider = self._global
            elif layer == "environment":
                state = self._environment(scope.environment)
                provider = self._usable(state.provider, invocation)
            elif layer == "module":
                if module_name is None:
                    return _MISSING
                state = self._environment(scope.environment)
                provider = self._usable(
                    self._module_provider(state, module_name), invocation
                )
            else:
                return _MISSING
            if provider is None:
                return _MISSING
            mod = provider.module_name if provider.place == "Module" else None
            return self._lookup_levels(
                root,
                provider.hierarchy,
                provider.root,
                "main",
                scope,
                invocation,
                strategy,
                segments,
                module_name=mod,
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
        layer -- except a ``data_dig`` provider, which is handed them
        directly; see :meth:`_lookup_levels`). On a miss -- including a hit
        whose *dig* misses -- and only for a qualified key whose own module
        has a ``default_hierarchy``, :meth:`_lookup_default_in_module` is
        consulted the same way (``lookup_adapter.rb:73-79``). A final miss
        returns :data:`~hyera._navigation._MISSING`; a found value has
        ``convert_to`` applied, if the options set one -- from the main
        hierarchy's ``lookup_options`` either way, even when the value came
        from the default hierarchy fallback.

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
            value = self._lookup_layers(
                root, module_name, invocation, strategy, segments
            )
        if value is not _MISSING and segments:
            value = sub_lookup(key, segments, value)

        if value is _MISSING:
            value = self._lookup_default_in_module(
                key, root, segments, module_name, invocation
            )
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
        instance's config, backends and caches (``_location_cache``,
        ``_lookup_options_cache``, ``_file_cache``, ``_glob_cache``), so
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
        # Never shared with the instance it was derived from, or with any
        # other view: a provider's interpolated options are bound to exactly
        # one scope (unlike ``_location_cache``/``_lookup_options_cache``/
        # ``_file_cache``/``_glob_cache``, all keyed on the scope value
        # itself or not at all, and ``_environment_context``, whose file
        # cache has no scope at all).
        view._providers = {}
        return view

    def sources(self):
        """Resolve the ordered list of source paths for this instance's
        bound scope.

        Existing files are parsed and cached and their paths returned.

        The filesystem walk (glob/iterdir/stat) is cached, keyed on the
        values of the variables the hierarchy's own interpolation reads
        (not the whole scope -- see :meth:`_location_entry_for`), so a merge
        lookup across many keys does not re-walk the tree for each key, and
        a scope differing only elsewhere shares the same cached walk. With
        ``revalidate=True`` (the default) each call still re-probes every
        candidate and re-lists any glob whose directory changed; with
        ``revalidate=False`` it reflects the tree as first seen for this
        scope's referenced-variable values, until :meth:`clear_cache`.
        """
        return self._sources(self.scope)

    def _sources(self, scope, invocation=None):
        return self._files_for(
            self.hierarchy, self.base_path, scope, "main", invocation
        )

    def _files_for(self, hierarchy, base_path, scope, tag, invocation=None):
        """The ordered list of existing, successfully loaded ``data_hash``
        file paths ``hierarchy`` visits for ``scope`` -- what ``sources()``
        shows.

        Only a ``data_hash`` level's *path* locations are ever loaded here
        (through :meth:`_load_file`, so they land in ``_file_cache`` exactly
        as a real lookup would find them): a ``lookup_key``/``data_dig``
        function is never called without a real key, and a ``uri`` location
        is never fetched or stat'ed -- ``sources()`` keeps its documented
        meaning, "the files a lookup may read".

        Re-derived on every call, never cached as its own flattened list:
        the expensive part -- resolving/materializing locations, and
        reading each file -- is already cached the referenced-variable/
        ``(path, strict, options)`` way (:meth:`_location_entry_for`/
        :meth:`_load_file`), both shared across every view of this
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
                self._load_file(
                    path, level.backend, provider.options_for(loc), invocation
                )
                if path in self._loaded_paths:
                    paths.append(path)
        return tuple(paths)

    def _layer_options_cached(self, hierarchy, base_path, tag, module_name, invocation):
        """The raw ``lookup_options`` value gathered from one layer's own
        hierarchy alone (a HASH-strategy gather over its locations/levels,
        never across layers) -- cached the same referenced-variable way as
        :meth:`_location_entry_for`, plus the location entry's own key and,
        with ``revalidate=True``, the current signature of every location it
        materializes to (a rebuilt hierarchy, or any of its files changing,
        invalidates any ``lookup_options`` gathered against the old state)
        -- and never cached at all when gathering it makes a sub-lookup (a
        sub-lookup can reach data outside this entry, such as the default
        hierarchy or another layer, so keying on referenced *variables*
        alone would not be sound). Gathered through the location/level
        nesting only, never the layer stack (``lookup_adapter.rb:241,
        346-380``); callers compose the layers and validate/compile the
        result (:func:`~hyera._lookup_adapter.validate_lookup_options`/
        ``compile_patterns``).

        ``invocation``, the caller's own top-level one, passed straight
        through (never a ``.derive()`` -- only its ``.scope``/``._fs_memo``
        are ever read here, so deriving one first would cost an allocation
        for nothing), shares its filesystem probe memo with the location
        build, the materialization below, and the gather's own invocation
        (built fresh, around ``self._sub_lookup``, only if the gather
        itself actually runs), so this whole gather costs
        at most one real probe per location for the caller's top-level
        lookup.

        Returns :data:`_LO_ABSENT` for "no location in this hierarchy
        declares ``lookup_options`` at all" -- distinct from an explicit
        ``lookup_options: ~`` (``None``), which a caller (Puppet's own
        ``if``/``elsif`` with no ``else``, ``lookup_adapter.rb:358-365``)
        treats differently.

        ``self._lookup_options_pending`` guards a value inside
        ``lookup_options`` that itself runs a full sub-lookup
        (:meth:`_sub_lookup`) asking this same method for its own key's
        options while this gather is still running (measured against a
        ``merge:`` spec interpolated through a nested ``%{lookup(...)}``):
        marking ``(scope, tag, base_path)`` pending before the gather starts
        means that nested lookup sees no options at all (:data:`_LO_ABSENT`),
        instead of re-entering this gather and recursing forever
        (``lookup_adapter.rb:376-378``); the marker comes off in ``finally``,
        so a gather that raises does not wedge a later, independent lookup
        for the same ``(scope, tag, base_path)``.
        """
        scope = invocation.scope
        entry = self._location_entry_for(hierarchy, base_path, scope, tag, invocation)
        materialized = self._materialize(entry, invocation)
        kind = ("lookup_options", tag, base_path)
        if self.revalidate:
            versions = tuple(
                (
                    loc.location,
                    (
                        invocation._memo_probe(loc.location)
                        if invocation is not None
                        else _probe(loc.location)
                    ).sig,
                )
                for locations in materialized
                if locations is not None
                for loc in locations
                if not loc.is_uri and loc.exist
            )
        else:
            versions = ()
        extra = (entry.key, versions)
        cached = self._lookup_options_cache.get(kind, scope, extra)
        if cached is not _MISSING:
            return cached

        pending_key = (scope, tag, base_path)
        if pending_key in self._lookup_options_pending:
            return _LO_ABSENT
        self._lookup_options_pending.add(pending_key)
        try:
            lo_refs = []
            made_sub_lookup = False

            def counting_lookup(key, inv):
                nonlocal made_sub_lookup
                made_sub_lookup = True
                return self._sub_lookup(key, inv)

            fs_memo = invocation._fs_memo if invocation is not None else None
            gather_invocation = Invocation(
                scope, counting_lookup, scope_interpolations=lo_refs, _fs_memo=fs_memo
            )
            with gather_invocation.check(LOOKUP_OPTIONS):
                raw = self._lookup_levels(
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
            self._lookup_options_pending.discard(pending_key)

        result = _LO_ABSENT if raw is _MISSING else raw
        if not made_sub_lookup:
            put_key = self._lookup_options_cache.key_for(kind, lo_refs, extra)
            self._lookup_options_cache.put(put_key, result)
        return result

    def _global_lookup_options(self, invocation):
        """The global layer's own validated ``lookup_options``, or ``None``.

        Passes ``invocation`` straight through, never a ``.derive()`` of it:
        :meth:`_layer_options_cached` only ever reads its own ``.scope``/
        ``._fs_memo`` from what it is handed here (the gather itself builds
        its own fresh ``Invocation`` around ``self._sub_lookup``), so
        deriving one first would only add an allocation this hot path (every
        lookup runs it) does not need.
        """
        raw = self._layer_options_cached(
            self._global.hierarchy, self._global.root, "main", None, invocation
        )
        return validate_lookup_options(None if raw is _LO_ABSENT else raw, None)

    def _environment_lookup_options(self, state, invocation):
        """The global and environment layers' ``lookup_options`` HASH-merged
        (global wins), or ``None`` (``lookup_adapter.rb:375-380``). See
        :meth:`_global_lookup_options` for why ``invocation`` is passed
        through unchanged rather than derived.
        """
        g = self._global_lookup_options(invocation)
        provider = self._usable(state.provider, invocation)
        e = None
        if provider is not None:
            raw = self._layer_options_cached(
                provider.hierarchy, provider.root, "main", None, invocation
            )
            e = validate_lookup_options(None if raw is _LO_ABSENT else raw, None)
        if g is None:
            return e
        if e is None:
            return g
        return MergeStrategy.strategy("hash").merge(g, e)

    def _retrieve_lookup_options(self, module_name, invocation):
        """The compiled ``lookup_options`` mapping for ``module_name`` (or
        just the global/environment options, when ``module_name`` is
        ``None``), a port of ``lookup_adapter.rb:346-372``.

        A module's own options are qualified against its name
        (:func:`~hyera._lookup_adapter.validate_lookup_options`) and
        gathered from its pruned data (which keeps ``lookup_options``),
        never merged with the global/environment options wholesale --
        module wins per key, through the same HASH strategy, but only when
        the module actually declares a real (non-``None``) mapping; a
        module walk that finds nothing at all (:data:`_LO_ABSENT`) leaves
        the environment options untouched, while one that finds an explicit
        ``lookup_options: ~`` discards them (Puppet's own ``if``/``elsif``
        with no ``else``, ``lookup_adapter.rb:358-365``).

        With ``revalidate=False``, the final composed-and-compiled result is
        itself memoized in ``self._compiled_options_cache`` by an identity
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
        """
        scope = invocation.scope
        if not self.revalidate:
            cached = self._compiled_options_cache.get(module_name)
            if cached is not None and cached[0] is scope:
                return cached[1]

        state = self._environment(scope.environment)
        opts = self._environment_lookup_options(state, invocation)
        if module_name is not None:
            mprovider = self._usable(
                self._module_provider(state, module_name), invocation
            )
            if mprovider is not None:
                raw = self._layer_options_cached(
                    mprovider.hierarchy, mprovider.root, "main", module_name, invocation
                )
                if raw is not _LO_ABSENT:
                    m = validate_lookup_options(raw, module_name)
                    if opts is None:
                        opts = m
                    elif m is not None:
                        opts = MergeStrategy.strategy("hash").merge(opts, m)
                    else:
                        opts = None
        compiled = compile_patterns(opts)
        if not self.revalidate:
            self._compiled_options_cache[module_name] = (scope, compiled)
        return compiled

    def _module_default_lookup_options(self, provider, invocation):
        """The compiled ``lookup_options`` mapping gathered from
        ``provider``'s own ``default_hierarchy`` data only -- never merged
        with the global/environment/module options
        (:meth:`_retrieve_lookup_options`), as Puppet's
        ``module_data_provider.rb:26-40``'s ``key_lookup_in_default``
        never touches the main options.
        """
        raw = self._layer_options_cached(
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

    def _lookup_default_in_module(self, key, root, segments, module_name, invocation):
        """Puppet's ``lookup_default_in_module``
        (``module_data_provider.rb:26-40``, ``lookup_adapter.rb:180-217``):
        a module's own ``default_hierarchy``, consulted only after the main
        stack (and its dig) misses.

        :data:`~hyera._navigation._MISSING` when ``module_name`` is
        ``None`` (an unqualified key never reaches a module's default
        hierarchy either), the module has no usable provider, or its
        ``default_hierarchy`` is empty. The merge strategy comes only from
        the default hierarchy's own ``lookup_options``
        (:meth:`_module_default_lookup_options`) -- never the caller's
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
        compiled = self._module_default_lookup_options(provider, invocation)
        entry = extract_lookup_options_for_key(root, compiled) or {}
        strategy = MergeStrategy.strategy(entry.get("merge"))
        with invocation.check(key):
            return self._lookup_levels(
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
