# Ported from Puppet 8 lib/puppet/pops/lookup/{data_hash_function_provider,
# data_provider,lookup_adapter,interpolation,hiera_config,location_resolver,
# function_provider,configured_data_provider}.rb, pops/lookup.rb, functions/{dig,get,
# getvar}.rb, util/run_mode.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

from __future__ import annotations

import logging
import os
import threading
import typing as _ty
from typing import Any

from pathlib_next import Path

from ._lookup import data_functions as _data_functions
from ._lookup.cache import _ScopeKeyedCache
from ._config.data_provider import (
    environment_for,
    load_global_layer,
    module_name_of,
    split_path_setting,
)
from ._output import explain as _explain
from ._output.explain import Explainer, ExplainResult, _DebugExplainer
from ._config.config_v3 import _default_codedir
from ._config.hiera_config import HieraLevel
from ._lookup.function_provider import _EnvironmentContext
from ._lookup.interpolation import interpolate, unshare
from ._lookup.invocation import _STRICT, Invocation
from ._lookup import enumeration as _enumeration
from ._lookup import layer_walk as _layer_walk
from ._lookup.locations import _LocationStore
from ._lookup.providers import files_for
from ._lookup.lookup_options import _ExplainOptionsMemo
from ._lookup.lookup_function import (
    check_call,
    depth_error,
    lookup as _lookup_call,
    parse_call,
    recursion_bound,
)
from ._lookup.merge_strategy import MergeSpec, MergeStrategy
from ._lookup.navigation import _MISSING, LOOKUP_OPTIONS, split_key
from ._scope.scope import Scope, Strict
from ._types.mismatch import assert_instance_of
from .backends import Backend, default_backends
from .exceptions import BackendError, HieraLookupError, KeyNotFoundError
from .types import TypeSpec

__all__ = ["Hiera"]

#: A single path, an iterable of paths, or a string of paths joined by
#: ``os.pathsep`` -- the shape every ``*path`` constructor argument takes.
_PathSpec = _ty.Union[
    str, "os.PathLike[str]", _ty.Iterable[_ty.Union[str, "os.PathLike[str]"]], None
]


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
        #: Whether this is Puppet's default config (``Hiera(None, ...)``), the one case
        #: where ``explain()`` prunes missing candidates (``hiera_config.rb:688``,
        #: ``location_resolver.rb:63``); other configs keep every ``Path not found``.
        self._is_default_config = base_config is None
        #: Puppet's ``$codedir`` (``util/run_mode.rb``): used by a version 3 hierarchy's
        #: default ``datadir`` (``<codedir>/environments/%{::environment}/hieradata``)
        #: only. Made absolute against the working directory.
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

        #: ``split_path_setting``: ``environmentpath`` is ``None`` when unset (no
        #: environment directories), ``basemodulepath`` normalizes to ``()``,
        #: ``modulepath`` is ``None`` only if never given (empty gives ``()``).
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
        # : Paths already warned about for an ignored version-3 layer config :
        # (:func:`~hyera._config.data_provider.usable_provider`), so the warning fires
        # once per config file.
        self._v3_warned_paths: set = set()
        self._hierarchy: "list[HieraLevel]" = []
        self._default_hierarchy: "list[HieraLevel]" = []
        self._init_caches()

        (
            self._backends,
            self._base,
            self._hierarchy,
            self._default_hierarchy,
            self._global,
        ) = load_global_layer(
            self.base_config,
            base_path,
            default_backends() if backends is None else backends,
            self.scope,
            self._codedir,
        )
        self._base_path: Path = self._global.root
        # Puppet fails every lookup on a broken environment config; loading
        # the construction scope's own environment now gives the same
        # failure at construction instead.
        environment_for(self, self.scope.environment)

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
        #: Resolved locations, glob listings and parsed data files, shared by every view
        #: of this instance (unlike ``_providers``): ``base_path``, the layer's root,
        #: tells one layer's hierarchy from another's.
        self._store = _LocationStore(
            self._cache_lock, self._cache_size, self._revalidate
        )
        # : The ``lookup_options`` value gathered from one layer's own hierarchy alone
        # (never merged across layers), : keyed the same way, plus the location entry it
        # was built against. See ``layer_options_cached``.
        self._lookup_options_cache = _ScopeKeyedCache(
            self._cache_lock, self._cache_size
        )
        #: ``(module_name, function_name, path) -> (parsed data, pruned data)``, apart
        #: from the store's unpruned file cache; valid while the parsed data is the
        #: object the pruned copy came from.
        self._pruned_cache: dict = {}
        #: ``module_name -> (scope, compiled)``: the latest ``retrieve_lookup_options``
        #: result per module, an identity fast path (``Scope`` is immutable) skipping
        #: ``validate_lookup_options`` and ``compile_patterns``; a miss recomputes.
        self._compiled_options_cache: dict = {}
        #: Per-view providers, never shared across views: their interpolated options are
        #: bound to one scope. The key's ``tag`` splits a module's ``default_hierarchy``
        #: from its main one; ``revalidate=True`` refreshes ``.locations`` in place.
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

    def __repr__(self) -> str:
        """One line naming the class, the base config and the scope's
        environment; never data, scope values or options."""
        return "{}(config={!r}, environment={!r})".format(
            type(self).__name__,
            self._global.source.label,  # type: ignore[attr-defined]
            self.scope.environment,
        )

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

    _lookup_levels = _layer_walk._lookup_levels
    _lookup_layers = _layer_walk._lookup_layers
    _search_and_merge = _layer_walk._search_and_merge
    _sub_lookup = _layer_walk._sub_lookup

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
        # Never shared with the instance it came from or another view: a provider's
        # interpolated options are bound to one scope, unlike the store,
        # ``_lookup_options_cache`` and ``_environment_context``, which are scope-free.
        for name in _VIEW_OWN_STATE:
            setattr(view, name, {})
        return view

    def sources(self) -> _ty.List[str]:
        """Resolve the ordered list of source paths for this instance's
        bound scope.

        Existing files are parsed and cached and their paths returned.

        The filesystem walk (glob/iterdir/stat) is cached, keyed on the
        values of the variables the hierarchy's own interpolation reads
        (not the whole scope -- see :meth:`_LocationStore.location_entry_for`),
        so a merge lookup across many keys does not re-walk the tree for each
        key, and a scope differing only elsewhere shares the same cached walk. With
        ``revalidate=True`` (the default) each call still re-probes every
        candidate and re-lists any glob whose directory changed; with
        ``revalidate=False`` it reflects the tree as first seen for this
        scope's referenced-variable values, until :meth:`clear_cache`.

        :returns: the existing main-hierarchy ``data_hash`` file paths, in
            search order.
        """
        return self._sources(self.scope)

    def _sources(self, scope, invocation=None):
        return files_for(
            self, self._hierarchy, self._base_path, scope, "main", invocation
        )

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

    #: Without this, ``__getitem__`` alone would make a ``Hiera`` iterable: Python would
    #: call ``[0]``, ``[1]``, ... and never see ``IndexError``. A ``Hiera`` is not a
    #: sequence; ``iter(h)`` raises ``TypeError``.
    __iter__ = None

    def keys(self) -> _ty.List[str]:
        """The top-level keys this instance can answer for its bound scope.

        Lists every key held by a ``data_hash`` level: the global hierarchy,
        the environment's, each module's own (only keys in its namespace, in
        name order), then each module's ``default_hierarchy``. A key appears
        once, at its first place in that order; ``lookup_options`` is never
        listed. A ``lookup_key`` or ``data_dig`` level cannot be listed and
        adds nothing. Files are read as a lookup reads them, so
        ``revalidate`` applies. Use ``h.scoped(...)`` for another scope.

        :returns: a new list of key names.
        :raises BackendError: a data file could not be read or parsed.
        :raises ConfigError: a layer's configuration is invalid.
        """
        return _enumeration.keys(self)

    def to_dict(self, *, merge: MergeSpec = None) -> _ty.Dict[str, _ty.Any]:
        """Every listed key with its looked-up value, in :meth:`keys` order.

        Runs one exact-key ``lookup((key,), merge=merge)`` per key, so each
        value is interpolated and converted as a lookup returns it (a
        ``convert_to: Sensitive`` key stays a :class:`~hyera.Sensitive`). A key
        whose lookup misses is left out. The result is a new dict that shares
        nothing with cached data.

        :param merge: a merge strategy applied to every key; ``None`` uses each
            key's own ``lookup_options``, as :meth:`lookup` does.
        :returns: a new dict of key to value.
        :raises HieraLookupError: resolving a key failed; the first error met
            propagates unchanged (an :class:`~hyera.InterpolationError` among
            them).
        """
        return _enumeration.to_dict(self, merge)

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
        result. ``dotted`` is a single Puppet dotted-navigation *string*
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
                # Puppet's `get()` looks up the root by its *segment* form directly,
                # never re-parsed: a quoted root such as `'"a.b".c'` must not have its
                # embedded dot split again by a second `parse_lookup_key` pass.
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
                # lookup_adapter.rb:61-63: look up "lookup_options" through the layer
                # stack like any key, not via `retrieve_lookup_options`'s own combining
                # (no `merge` explain node); swallow what it finds or misses.
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
            # A data file that cannot be read or parsed is a data/infrastructure
            # problem, not one of Puppet's reportable LookupErrors: it always escapes,
            # wherever raised, as an ordinary `.lookup()` never catches it.
            raise
        except HieraLookupError as e:
            if getattr(e, "_explain_escape", False):
                raise
            invocation.report_text(lambda: str(e))
            error = e
        return ExplainResult(explainer, error)
