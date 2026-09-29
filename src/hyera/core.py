# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/pops/lookup/data_hash_function_provider.rb,
# data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging
import os
from typing import Any

from ._hiera_config import (
    HieraLevel,
    _build_hierarchies,
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
from ._navigation import _MISSING, parse_lookup_key, sub_lookup
from ._scope import Scope
from .backends import default_backends

__all__ = ["Hiera"]

_LOGGER = logging.getLogger(__name__)

#: Puppet's provider stack (``lookup_adapter.rb:296``): a key is looked up
#: through each layer in turn, merged the same way as levels/locations
#: within a layer. Only ``"global"`` is populated; the ``environment``
#: and ``module`` layers are not implemented yet.
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
        #: ``(path, backend.strict) -> loaded data``. See ``_load_file``.
        self.cache: dict = {}
        #: Every plain path ever loaded successfully into ``self.cache``,
        #: under any ``strict`` variant -- what ``_files_for`` consults to
        #: tell a loaded location from a missing/unattempted one.
        self._loaded_paths: set = set()
        #: Per-scope cache of resolved source path lists (see ``sources``).
        self._source_cache: dict = {}
        #: Per-scope cache of the merged ``lookup_options`` mapping.
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

    def _lookup_levels(self, root, levels, invocation, strategy):
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
            if data is _MISSING or not isinstance(data, dict) or root not in data:
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

    def _lookup_layers(self, root, levels, invocation, strategy):
        """Puppet's provider stack (``lookup_adapter.rb:332-340``): reduce
        ``_LAYERS`` the same way, with only ``"global"`` populated."""

        def at_layer(layer):
            if layer != "global":
                return _MISSING
            return self._lookup_levels(root, levels, invocation, strategy)

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
        hierarchy is walked through the provider-layer stack on the bare
        root key; a dotted key's segments are dug out of the merged root
        value exactly once (never per location or per level). On a miss,
        and only when a ``default_hierarchy`` is configured, the same walk
        (without the layer stack, same strategy) runs over it. A final
        miss returns :data:`~hyera._navigation._MISSING`; a found value
        has ``convert_to`` applied, if the options set one.

        ``parsed`` lets a caller that already split ``key`` into
        ``(root, segments)`` skip re-parsing it.
        """
        if key == LOOKUP_OPTIONS or key.startswith(LOOKUP_OPTIONS + "."):
            return _MISSING
        root, segments = parsed if parsed is not None else parse_lookup_key(key)

        options = self._lookup_options_for(root, invocation.scope) or {}
        strategy = MergeStrategy.strategy(
            merge if merge is not None else options.get("merge")
        )

        levels = self._levels_for(self.hierarchy, invocation.scope, "main")
        with invocation.check(key):
            value = self._lookup_layers(root, levels, invocation, strategy)
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
        return self._files_for(self.hierarchy, scope, "main")

    def _levels_for(self, hierarchy, scope, tag):
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

        Cached per scope value (the filesystem walk -- glob/stat -- is
        what's expensive, not the reduce over the result), sharing
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
            for loc in resolve_locations(level, self.base_path, scope):
                if loc.exist:
                    locations.append(self._load_file(loc.location, level.backend))
                else:
                    locations.append(loc.location)
            levels.append((level.backend.name, tuple(locations)))
        levels = tuple(levels)
        self._source_cache[cache_key] = levels
        return levels

    def _files_for(self, hierarchy, scope, tag):
        """The flattened, loaded-only view of :meth:`_levels_for` -- exactly
        what the old per-file walk returned, and what ``sources()`` shows."""
        return [
            path
            for _function_name, locations in self._levels_for(hierarchy, scope, tag)
            for path in locations
            if path in self._loaded_paths
        ]

    def _default_levels(self, scope):
        return self._levels_for(self.default_hierarchy, scope, "default")

    def _lookup_options_map(self, scope, tag="main"):
        """The compiled ``lookup_options`` mapping for a scope, or ``None``.

        Merging it walks every location/level, and a default-merge lookup
        needs it for every key — so the result is cached per scope value
        alongside ``_source_cache``, sharing the same instance-lifetime
        staleness contract. Gathered through the location/level nesting
        only, never the layer stack (``lookup_adapter.rb:241,346-380``).
        Compiling the regex patterns here, once per scope, means an invalid
        pattern fails every lookup that reads ``lookup_options``, as in
        Puppet (``compile_patterns``/``validate_lookup_options`` raise
        eagerly, not lazily per key; a raised error is not cached).

        Before gathering, ``None`` (no options) is stored under this
        ``(tag, scope)`` key: a value inside ``lookup_options`` can itself
        run a full sub-lookup (:meth:`_sub_lookup`) that asks this same
        cache for its own key's options while the gather below is still
        running (measured against a ``merge:`` spec interpolated through
        a nested ``%{lookup(...)}``) -- storing "no options" first means
        that nested lookup sees none, instead of re-entering this gather
        and recursing forever (``lookup_adapter.rb:376-378``). A gather
        that raises removes the stored ``None`` again, so the next lookup
        retries it instead of caching the failure.
        """
        cache_key = (tag, scope)
        if cache_key in self._lookup_options_cache:
            return self._lookup_options_cache[cache_key]

        self._lookup_options_cache[cache_key] = None
        try:
            levels = self._levels_for(self.hierarchy, scope, "main")
            invocation = Invocation(scope, self._sub_lookup)
            with invocation.check(LOOKUP_OPTIONS):
                options = self._lookup_levels(
                    LOOKUP_OPTIONS, levels, invocation, MergeStrategy.strategy("hash")
                )
        except Exception:
            del self._lookup_options_cache[cache_key]
            raise
        if options is _MISSING:
            options = None
        compiled = compile_patterns(validate_lookup_options(options))
        self._lookup_options_cache[cache_key] = compiled
        return compiled

    def _lookup_options_for(self, root, scope):
        """Return the merged ``lookup_options`` entry matching ``root``, or None.

        ``lookup_options`` is a reserved data key: ``{pattern: {merge, convert_to}}``.
        Higher-priority (earlier) levels win per pattern. An exact key match
        wins over a regex pattern match; the first regex match (searched
        from the start of the key, Ruby ``=~``, not a Python ``fullmatch``)
        otherwise wins. Always matched against the key's *root* -- never a
        dotted key -- since a dig happens after any merge, not before.
        """
        compiled = self._lookup_options_map(scope)
        return extract_lookup_options_for_key(root, compiled)

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
