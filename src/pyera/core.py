# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging

from ._hiera_config import (
    DEFAULT_DATA_DIR,
    HieraLevel,
    _build_hierarchies,
    _config_origin,
    _config_path,
    _read_base_config,
)
from ._interpolation import Interpolation, _format_source, _normalize_source
from ._location_resolver import _resolve_level_paths
from ._lookup_adapter import _extract_lookup_options_for_key
from ._merge_strategy import make_merge
from ._types import Sensitive, _convert_to
from .backends import default_backends
from .util import LookupDict

__all__ = ["Hiera", "ScopedHiera"]

_LOGGER = logging.getLogger(__name__)


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
    applied here so every backend -- third-party ones included -- gets it
    (Design Q9 of ``backend_registry_and_data_loading/registry``)."""
    if isinstance(data, dict):
        return
    raise BackendError(
        "Value returned from data_hash function '{}', when using location "
        "'{}', has wrong type, expects a Hash value, got {}".format(
            name, path, _puppet_type_label(data)
        ),
        path=str(path),
    )


def _as_lookupdict(obj):
    """Recursively adapt a backend's plain ``dict``/``list`` result
    into :class:`~pyera.util.LookupDict` for dotted-key lookup. Backends
    themselves return plain data; only the engine's cache needs the dotted
    lookup. ``navigation_sub_lookup`` removes both sides of this adapter."""
    if isinstance(obj, dict):
        return LookupDict((k, _as_lookupdict(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_as_lookupdict(v) for v in obj]
    return obj


class ScopedHiera:
    def __init__(self, hiera, context=None):
        self.hiera = hiera
        self.context = context or {}

    def has(self, key, context=None, **kwargs):
        # Same layering as .get(): the bound context goes *under* per-call
        # overrides, so a scoped .has() always agrees with the equivalent
        # .get().
        new_context = {}
        new_context.update(self.context)
        new_context.update(context or {})
        new_context.update(kwargs)
        return self.hiera.has(key, context=new_context)

    def get(
        self,
        key,
        default=None,
        merge=None,
        merge_deep=False,
        throw=False,
        context=None,
        **kwargs,
    ):
        new_context = {}
        new_context.update(self.context)
        new_context.update(context or {})
        new_context.update(kwargs)
        return self.hiera.get(key, default, merge, merge_deep, throw, new_context)

    def __getattr__(self, name):
        # Copying/pickling rebuilds the instance without __init__ and then
        # probes it for state/dunder methods; without this guard that probe
        # reaches ``self.hiera`` -- itself an attribute lookup on the same
        # not-yet-initialized instance -- recursing until the stack
        # overflows. ``hiera`` and any dunder name are never proxied.
        if name == "hiera" or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.hiera, name)


class Hiera(Interpolation):
    """A first-class Python interface to Hiera data.

    It takes a base hiera config (YAML file path, file-like object, or dict)
    and exposes methods to retrieve and fully resolve hiera values.

    :param base_config: hiera base configuration: file path, file-like object,
        or a pre-parsed ``dict``.
    :param backends: an allow-list of :class:`~pyera.backends.Backend`
        classes; defaults to :func:`default_backends` — every backend
        registered in the ``function`` namespace (``YAMLBackend``,
        ``JSONBackend``, ``HOCONBackend``, ``SopsBackend``).
    :param base_path: root that relative data dirs/paths resolve against.
    :param context: default format/context variables for this instance's
        lifetime.
    :param kwargs: additional context variables (merged into ``context``).
    """

    def __init__(
        self, base_config, backends=None, base_path=None, context: dict = None, **kwargs
    ):
        self.base_config = base_config
        self.context = dict(context or {})
        self.context.update(kwargs)

        self.hierarchy: "list[HieraLevel]" = []
        self.default_hierarchy: "list[HieraLevel]" = []
        self.cache: dict = {}
        #: Per-context cache of resolved source path lists (see ``sources``).
        self._source_cache: dict = {}
        #: Per-context cache of the merged ``lookup_options`` mapping.
        self._lookup_options_cache: dict = {}

        self._load_config(
            default_backends() if backends is None else backends, base_path
        )

    def _build_context(self, context: dict = None, **kwargs) -> dict:
        new_context = {}
        new_context.update(self.context)
        new_context.update(context or {})
        new_context.update(kwargs)
        # Filter out empty/None values so they don't satisfy a format field.
        return {k: v for k, v in new_context.items() if v}

    def format(self, text: str, context: dict = None, **kwargs) -> str:
        context = self._build_context(context, **kwargs)
        return _format_source(_normalize_source(text), context)

    def _load_config(self, backends, base_path=None):
        """Load and validate the base configuration, building hierarchy state.

        Raises :class:`ConfigError` on any invalid/missing configuration.
        """
        #: Allow-list of backend classes a hierarchy level's ``data_hash``
        #: may resolve to (the Backend registry, looked up by name in
        #: ``_hiera_config._build_levels``).
        self.backends: "list[type]" = list(backends)

        self.base, self.base_path = _read_base_config(self.base_config, base_path)

        if not self.backends:
            raise ConfigError("No backends could be loaded")

        origin = _config_origin(self.base_config)
        path = _config_path(self.base_config)
        try:
            self.hierarchy, self.default_hierarchy = _build_hierarchies(
                self.base, self.backends
            )
        except HieraError as e:  # keep the class and text, add the file
            e.path = e.path or path
            raise
        except Exception as e:
            raise ConfigError(
                "The Lookup Configuration at '{}' is invalid: {}: {}".format(
                    origin, type(e).__name__, _one_line(e)
                ),
                path=path,
            ) from e

        # Pre-load/cache global (context-free) data.
        self._prewarm()

    def _prewarm(self) -> None:
        """Load and cache every context-free source file up front.

        Mirrors the source-resolution side effects of a ``get(None)`` call
        without going through the public API's key-type check.
        """
        ctx = self._build_context()
        self.sources(ctx)
        if self.default_hierarchy:
            self._default_files(ctx)

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
        backend -- third-party ones included -- gets it (Design Q9).
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
            self.cache[path] = _as_lookupdict(data)
        return path

    def _get_key(self, key, paths, context, merge):
        """Get the value of ``key``, resolving it, walking ``paths`` in order.

        ``merge`` is a :class:`Merge` accumulator or ``None`` (first wins).
        """
        found = False
        for path in paths:
            if self.cache[path] is not None and key is not None:
                cache = None
                try:
                    cache = self.cache[path].lookup(key)
                except (KeyError, IndexError):
                    pass

                if cache is not None:
                    value = self._resolve(cache, paths, context, merge)
                    if merge is None:
                        return value
                    merge.merge_value(value)
                    found = True

        if merge is not None and found:
            return merge.finalize()

        if key is not None and len(key.split(".")) > 1:
            _LOGGER.debug(
                "Lookup key '%s' not found; ensure it is provided in the "
                "hiera data.",
                key,
            )
        raise KeyError(key)

    def scoped(self, context=None, **kwargs):
        context = dict(context or {})
        context.update(kwargs)
        return ScopedHiera(self, context)

    def has(self, key, context=None, **kwargs) -> bool:
        """Return True if ``key`` exists in hiera, False otherwise.

        ``context``/``kwargs`` layer over the instance context exactly as in
        :meth:`get`.
        """
        try:
            self.get(key, throw=True, context=context, **kwargs)
            return True
        except KeyNotFoundError:
            return False

    def sources(self, context=None, **kwargs):
        """Resolve the ordered list of source paths for a context.

        Existing files are parsed and cached and their cache-key paths
        returned.

        The filesystem walk (glob/iterdir/stat) is cached per resolved context
        so a merge lookup across many keys does not re-walk the tree for each
        key. This shares the staleness assumption of the parsed-content cache:
        a single instance reflects the tree as first seen for a given context.
        """
        context = self._build_context(context, **kwargs)
        return self._files_for(self.hierarchy, context, "main")

    def _files_for(self, hierarchy, context, tag):
        try:
            cache_key = (tag, frozenset(context.items()))
        except TypeError:
            # An unhashable context value (e.g. a list) — skip caching.
            cache_key = None
        if cache_key is not None:
            cached = self._source_cache.get(cache_key)
            if cached is not None:
                return list(cached)

        files = []
        for level in hierarchy:
            for path in _resolve_level_paths(level, self.base_path, context):
                paths = path.iterdir() if path.is_dir() else [path]
                for path in paths:
                    if path.exists() and path.is_file():
                        files.append(self._load_file(path, level.backend))
        if cache_key is not None:
            self._source_cache[cache_key] = list(files)
        return files

    def _default_files(self, context):
        return self._files_for(self.default_hierarchy, context, "default")

    def _lookup_options_map(self, files, context, tag="main"):
        """The merged ``lookup_options`` mapping for a context, or ``None``.

        Merging it walks every file in the hierarchy, and a default-merge
        ``get()`` needs it for every key — so the result is cached per
        resolved context alongside ``_source_cache``, sharing the same
        instance-lifetime staleness contract.
        """
        try:
            cache_key = (tag, frozenset(context.items()))
        except TypeError:
            # An unhashable context value (e.g. a list) — skip caching.
            cache_key = None
        if cache_key is not None and cache_key in self._lookup_options_cache:
            return self._lookup_options_cache[cache_key]

        try:
            options = self._get_key(
                "lookup_options", files, context, make_merge("hash")
            )
        except KeyError:
            options = None
        if not isinstance(options, dict):
            options = None
        if cache_key is not None:
            self._lookup_options_cache[cache_key] = options
        return options

    def _lookup_adapter(self, key, files, context):
        """Delegate to the lookup options adapter."""
        options = self._lookup_options_map(files, context)
        return _extract_lookup_options_for_key(key, options)

    def _lookup_options_for(self, key, files, context):
        """Return the merged ``lookup_options`` entry matching ``key``, or None.

        ``lookup_options`` is a reserved data key: ``{pattern: {merge, convert_to}}``.
        Higher-priority (earlier) levels win per pattern. An exact key match
        wins over a regex pattern match; the first regex match otherwise wins.
        """
        return self._lookup_adapter(key, files, context)

    def get(
        self,
        key: str,
        default=None,
        merge=None,
        merge_deep=False,
        throw=False,
        context=None,
        **kwargs,
    ):
        """Retrieve a hiera value by fully resolving its location.

        :param key: the hiera key to retrieve.
        :param default: returned when the key is missing (unless ``throw``).
        :param merge: merge strategy. A name (``"first"``/``"unique"``/
            ``"hash"``/``"deep"``), a legacy type (``list``/``set``/``dict``),
            or a hash ``{"strategy": "deep", "knockout_prefix": "--", ...}``.
            When omitted, ``lookup_options`` in the data (if any) decides;
            otherwise first-match wins.
        :param merge_deep: legacy flag — with ``merge`` a type, promote a hash
            merge to a deep merge.
        :param throw: raise ``KeyError`` on a missing key instead of returning
            ``default``.
        :param context: per-call context variables.
        :param kwargs: override context variables.
        """
        if not isinstance(key, str):
            raise TypeError(
                "lookup key must be a str, not {}".format(type(key).__name__)
            )
        new_context = self._build_context(context, **kwargs)
        # Resolve sources against the *built* context: per-call **kwargs are
        # documented context overrides, so they must reach hierarchy path
        # resolution too, not just interpolation and lookup_options. The
        # default_hierarchy retry below uses this same new_context, so both
        # hierarchies always agree on which sources a per-call override picks.
        files = self.sources(new_context)

        explicit = merge is not None
        if merge_deep and merge in (dict, "hash"):
            merge = "deep"
        merge_obj = make_merge(merge)

        convert_to = None
        if not explicit and key is not None:
            opts = self._lookup_options_for(key, files, new_context)
            if opts is not None:
                if opts.get("merge") is not None:
                    merge_obj = make_merge(opts["merge"])
                convert_to = opts.get("convert_to")

        try:
            value = self._get_key(key, files, new_context, merge=merge_obj)
        except KeyError:
            if self.default_hierarchy:
                try:
                    value = self._get_key(
                        key,
                        self._default_files(new_context),
                        new_context,
                        merge=make_merge(merge) if explicit else merge_obj,
                    )
                except KeyError:
                    if throw:
                        raise KeyNotFoundError(key) from None
                    return default
            elif throw:
                raise KeyNotFoundError(key) from None
            else:
                return default

        if convert_to is not None:
            value = _convert_to(value, convert_to)
        return value


# Import after defining Hiera to avoid circular import
from .exceptions import (  # noqa: E402
    BackendError,
    ConfigError,
    HieraError,
    KeyNotFoundError,
    _one_line,
)
