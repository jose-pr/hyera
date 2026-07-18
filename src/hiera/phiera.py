"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging
import os
import re
import typing as _ty
from copy import deepcopy

from pathlib_next import Path

from .backends import Backend, JSONBackend, SopsYAMLBackend, YAMLBackend
from .exceptions import ConfigError, InterpolationError
from .util import LookupDict

__all__ = [
    "Merge",
    "ScopedHiera",
    "HieraLevel",
    "Hiera",
]

function = re.compile(
    r"""%\{(scope|hiera|lookup|literal|alias)\(['"](?:::|)([^"']*)["']\)\}"""
)
# A bare ``%{var}`` reference. Excludes ``(`` so it does not also match a
# function-style ``%{hiera('x')}`` token (those are handled by ``function``);
# without this, an unresolved function leftover would be blanked here.
interpolate = re.compile(r"""%\{(?:::|)([^(}]*)\}""")
# A bare ``%{var}`` reference; the captured name becomes a ``{var}`` format
# field. The character class allows the identifier chars Puppet permits.
rformat = re.compile(r"""%\{(?:::|)([a-zA-Z0-9_.|-]+)\}""")

LOGGER = logging.getLogger(__name__)

#: Default puppet-style data dir, used when a hierarchy omits ``datadir``.
DEFAULT_DATA_DIR = "/etc/puppetlabs/code/environments/%{environment}/hieradata"


def _normalize_source(source: str) -> str:
    """Convert puppet ``%{var}`` references into ``str.format`` ``{var}`` fields."""
    return rformat.sub(r"{\g<1>}", source, count=0)


class Merge(object):
    def __init__(self, typ, deep=False):
        self.typ = typ
        self.deep = deep

        if typ == dict:
            self.value = LookupDict()
        else:
            self.value = typ()

    def merge_value(self, value):
        if isinstance(self.value, list):
            self.value += list(value)
        elif isinstance(self.value, set):
            self.value = self.value | set(value)
        elif isinstance(self.value, dict):
            if self.deep:
                self.value = self.deep_merge(self.value, value)
            else:
                for k, v in value.items():
                    if k not in self.value:
                        self.value[k] = v
        elif isinstance(self.value, str):
            self.value = value
        else:
            raise TypeError(
                "Cannot handle merge_value of type {}".format(type(self.value))
            )

    def deep_merge(self, a, b):
        """Recursively merge dicts. When both ``a`` and ``b`` hold a dict at
        the same key, recurse; lists are concatenated without duplicates.

        ``a`` is the higher-priority side (an earlier hierarchy level). When
        both hold a scalar at the same key, ``a`` wins — later levels never
        clobber a value an earlier level already provided."""
        if not isinstance(b, dict):
            return b
        result = deepcopy(a)
        for k, v in b.items():
            if k in result and isinstance(result[k], dict):
                result[k] = self.deep_merge(result[k], v)
            elif k in result and isinstance(result[k], list):
                if isinstance(v, list):
                    v = [_ for _ in v if _ not in result[k]]
                    result[k] += deepcopy(v)
                else:
                    result[k].append(v)
            elif k not in result:
                # Key only in the lower-priority side; take it. An existing
                # scalar in ``result`` (higher priority) is left untouched.
                result[k] = deepcopy(v)
        return result


class ScopedHiera(object):
    def __init__(self, hiera, context=None):
        self.hiera = hiera
        self.context = context or {}

    def has(self, key, **kwargs):
        kwargs.update(self.context)
        return self.hiera.has(key, **kwargs)

    def get(
        self,
        key,
        default=None,
        merge=None,
        merge_deep=False,
        throw=False,
        context=None,
        **kwargs
    ):
        new_context = {}
        new_context.update(self.context)
        new_context.update(context or {})
        new_context.update(kwargs)
        return self.hiera.get(key, default, merge, merge_deep, throw, new_context)

    def __getattr__(self, name):
        if hasattr(self.hiera, name):
            return getattr(self.hiera, name)
        raise AttributeError(name)


class HieraLevel(_ty.NamedTuple):
    backend: Backend
    sources: "list[str]"
    #: True when sources are glob patterns rather than literal relative paths.
    glob: bool = False

    @classmethod
    def new(cls, conf: dict, backend: Backend) -> "HieraLevel":
        sources: "list[str]" = []
        is_glob = False
        path = conf.get("path")
        paths = conf.get("paths")
        glob = conf.get("glob")
        globs = conf.get("globs")
        if path:
            sources = [path]
        elif paths:
            sources = list(paths)
        elif glob:
            sources = [glob]
            is_glob = True
        elif globs:
            sources = list(globs)
            is_glob = True

        return HieraLevel(
            backend,
            [_normalize_source(source) for source in sources if source],
            is_glob,
        )

    def paths(self, base_path: Path, context: dict):
        """Yield the candidate source paths for this level in a given context.

        Glob levels expand their patterns against the filesystem (sorted for
        determinism); literal levels format each source with the context.
        Sources referencing a context var that is absent are skipped.
        """
        for source in self.sources:
            try:
                datadir = self.backend.datadir.format_map(context)
                rel = source.format_map(context)
            except KeyError:
                continue
            root = base_path / datadir
            if self.glob:
                for match in sorted(root.glob(rel)):
                    yield match
            else:
                yield root / rel


class Hiera(object):
    """A first-class Python interface to Hiera data.

    It takes a base hiera config (YAML file path, file-like object, or dict)
    and exposes methods to retrieve and fully resolve hiera values.

    :param base_config: hiera base configuration: file path, file-like object,
        or a pre-parsed ``dict``.
    :param backends: backend classes to use for loading; defaults to
        ``[YAMLBackend, SopsYAMLBackend, JSONBackend]``.
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
        self.cache: dict = {}
        #: Per-context cache of resolved source path lists (see ``sources``).
        self._source_cache: dict = {}

        self.load(backends or [YAMLBackend, SopsYAMLBackend, JSONBackend], base_path)

    def buildcontext(self, context: dict = None, **kwargs) -> dict:
        new_context = {}
        new_context.update(self.context)
        new_context.update(context or {})
        new_context.update(kwargs)
        # Filter out empty/None values so they don't satisfy a format field.
        return {k: v for k, v in new_context.items() if v}

    def format(self, text: str, context: dict = None, **kwargs) -> str:
        context = self.buildcontext(context, **kwargs)
        return _normalize_source(text).format_map(context)

    def load(self, backends, base_path=None):
        """Load and validate the base configuration, building hierarchy state.

        Raises :class:`ConfigError` on any invalid/missing configuration.
        """
        # Register each backend under every name it answers to.
        self.backends: "dict[str, type[Backend]]" = {}
        for backend in backends:
            for name in backend.NAMES:
                self.backends[name] = backend

        if isinstance(self.base_config, dict):
            self.base = self.base_config
            self.base_path = Path(os.getcwd() if base_path is None else base_path)
        else:
            if not hasattr(self.base_config, "read"):
                configpath = Path(self.base_config)
                self.base_path = configpath.parent
                self.base_config = configpath.open()
            else:
                self.base_path = Path(os.getcwd())
            self.base = YAMLBackend.load_ordered(self.base_config)

        if not self.base:
            raise ConfigError("Failed to parse base Hiera configuration")

        if base_path is not None:
            self.base_path = Path(base_path)

        if not self.backends:
            raise ConfigError("No backends could be loaded")

        hierarchy = self.base.get("hierarchy")
        defaults = self.base.get("defaults") or {}
        if hierarchy is None:
            raise ConfigError("Invalid base Hiera config: missing 'hierarchy' key")

        self.hierarchy = []
        defaults.setdefault("data_dir", DEFAULT_DATA_DIR)
        for level in hierarchy:
            conf = {**level}
            for k, v in defaults.items():
                conf.setdefault(k, v)
            data_hash = conf.get("data_hash")
            if data_hash is None:
                raise ConfigError(
                    "Hierarchy level {!r} is missing a 'data_hash' backend".format(
                        conf.get("name", conf)
                    )
                )
            try:
                backend_cls = self.backends[data_hash]
            except KeyError:
                raise ConfigError(
                    "Unknown backend {!r}; known: {}".format(
                        data_hash, ", ".join(sorted(self.backends))
                    )
                )
            # Normalize datadir spelling for the backend.
            conf.setdefault("datadir", conf.get("data_dir"))
            backend = backend_cls(conf)
            backend.datadir = _normalize_source(backend.datadir)
            self.hierarchy.append(HieraLevel.new(conf, backend))

        # Pre-load/cache global (context-free) data.
        self.get(None)

    def load_file(self, path: Path, backend: Backend, ignore_cache=False):
        """Load ``path`` via ``backend``, caching the parsed result."""
        if path not in self.cache or ignore_cache:
            try:
                self.cache[path] = backend.load(backend.read_file(path))
            except Exception as e:
                raise ConfigError("Failed to load file {}: `{}`".format(path, e)) from e
        return path

    def can_resolve(self, s) -> bool:
        """True if any function call or interpolation is present in ``s``."""
        return isinstance(s, str) and bool(function.findall(s) or interpolate.findall(s))

    def resolve_function(self, s, paths, context, merge):
        """Fully resolve hiera function calls (``%{hiera(...)}`` etc.) in ``s``."""
        calls = function.findall(s)
        # An alias replaces the whole value (no string interpolation).
        if len(calls) == 1 and calls[0][0] == "alias":
            if function.sub("", s) != "":
                raise InterpolationError(
                    "Alias cannot be used for string interpolation: `{}`".format(s)
                )
            try:
                return self.get_key(calls[0][1], paths, context, merge)
            except KeyError:
                raise InterpolationError(
                    "Alias lookup failed: key '{}' does not exist".format(calls[0][1])
                )

        for call, arg in calls:
            replace = None
            if call == "hiera" or call == "lookup":
                try:
                    replace = self.get_key(arg, paths, context, merge)
                except KeyError:
                    replace = None
            elif call == "scope":
                replace = context.get(arg)
            elif call == "literal":
                replace = arg
            elif call == "alias":
                raise InterpolationError("Invalid alias function call: `{}`".format(s))
            else:  # pragma: no cover - guarded by the `function` regex
                raise InterpolationError(
                    "Unknown function call {!r} in: `{}`".format(call, s)
                )

            # Reject only a genuinely absent value; falsy results (0, "",
            # False) are legitimate and must interpolate as themselves.
            if replace is None:
                raise InterpolationError(
                    "Could not resolve value for function call: `{}`".format(s)
                )

            # A function call standing alone as the whole value keeps the
            # resolved value's native type (so `%{alias(...)}`-style single
            # calls to a list/dict pass through). When it is embedded in a
            # larger string, the resolved value is stringified.
            if function.sub("", s) == "" and len(calls) == 1:
                s = replace
            elif isinstance(replace, (str, int, float, bool)):
                text = str(replace)
                s = function.sub(lambda _m, r=text: r, s, 1)
            else:
                raise InterpolationError(
                    "Cannot interpolate non-scalar value {!r} into string: "
                    "`{}`".format(replace, s)
                )

        return s

    def resolve_interpolates(self, s, context):
        """Resolve context-based ``%{var}`` string interpolation."""
        for i in interpolate.findall(s):
            # Missing vars interpolate to empty string (matches ruby hiera).
            replacement = context.get(i) or ""
            s = interpolate.sub(lambda _m, r=str(replacement): r, s, 1)
        return s

    def resolve(self, s, paths, context, merge):
        """Fully resolve ``s``: functions, interpolation, and nested structures."""
        if isinstance(s, dict):
            return self.resolve_dict(s, paths, context, merge)
        elif isinstance(s, list):
            return list(self.resolve_list(s, paths, context, merge))
        elif not self.can_resolve(s):
            return s

        base = self.resolve_function(s, paths, context, merge)
        if isinstance(base, str):
            base = self.resolve_interpolates(base, context)
        return base

    def resolve_dict(self, obj, paths, context, merge):
        new_obj = LookupDict()
        for k, v in obj.items():
            new_obj[k] = self.resolve(v, paths, context, merge)
        return new_obj

    def resolve_list(self, obj, paths, context, merge):
        for item in obj:
            yield self.resolve(item, paths, context, merge)

    def get_key(self, key, paths, context, merge):
        """Get the value of ``key``, resolving it, walking ``paths`` in order."""
        merges: dict = {}
        for path in paths:
            if self.cache[path] is not None and key is not None:
                cache = None
                try:
                    cache = self.cache[path].lookup(key)
                except (KeyError, IndexError):
                    pass

                if cache is not None:
                    if merge and key not in merges:
                        merges[key] = Merge(merge.typ, merge.deep)

                    value = self.resolve(
                        cache,
                        paths,
                        context,
                        (merges[key] if merge and merge.deep else merge),
                    )

                    if merge and merges[key]:
                        merges[key].merge_value(value)
                    else:
                        return value

        if merge and key in merges and merges[key].value is not None:
            return merges[key].value

        if key is not None and len(key.split(".")) > 1:
            LOGGER.debug(
                "Lookup key '%s' not found; ensure it is provided in the "
                "hiera data.",
                key,
            )
        raise KeyError(key)

    def scoped(self, context=None, **kwargs):
        context = dict(context or {})
        context.update(kwargs)
        return ScopedHiera(self, context)

    def has(self, key, **kwargs) -> bool:
        """Return True if ``key`` exists in hiera, False otherwise."""
        try:
            self.get(key, throw=True, **kwargs)
            return True
        except KeyError:
            return False

    def sources(self, context=None, _load=True, **kwargs):
        """Resolve the ordered list of source paths for a context.

        When ``_load`` is True, existing files are parsed and cached and their
        cache-key paths returned; otherwise the raw candidate paths are yielded.

        The filesystem walk (glob/iterdir/stat) is cached per resolved context
        so a merge lookup across many keys does not re-walk the tree for each
        key. This shares the staleness assumption of the parsed-content cache:
        a single instance reflects the tree as first seen for a given context.
        """
        context = self.buildcontext(context, **kwargs)
        try:
            cache_key = (_load, frozenset(context.items()))
        except TypeError:
            # An unhashable context value (e.g. a list) — skip caching.
            cache_key = None
        if cache_key is not None:
            cached = self._source_cache.get(cache_key)
            if cached is not None:
                return list(cached)

        files = []
        for level in self.hierarchy:
            for path in level.paths(self.base_path, context):
                paths = path.iterdir() if path.is_dir() else [path]
                for path in paths:
                    if _load:
                        if path.exists() and path.is_file():
                            files.append(self.load_file(path, level.backend))
                    else:
                        files.append(path)
        if cache_key is not None:
            self._source_cache[cache_key] = list(files)
        return files

    def get(
        self,
        key: str,
        default=None,
        merge=None,
        merge_deep=False,
        throw=False,
        context=None,
        **kwargs
    ):
        """Retrieve a hiera value by fully resolving its location.

        :param key: the hiera key to retrieve.
        :param default: returned when the key is missing (unless ``throw``).
        :param merge: ``list``/``dict``/``set`` to array/hash-merge across the
            whole hierarchy instead of returning the first match.
        :param merge_deep: deep-merge dict values when merging.
        :param throw: raise ``KeyError`` on a missing key instead of returning
            ``default``.
        :param context: per-call context variables.
        :param kwargs: override context variables.
        """
        new_context = self.buildcontext(context, **kwargs)
        files = self.sources(context)

        if merge:
            merge = Merge(merge, merge_deep)

        try:
            return self.get_key(key, files, new_context, merge=merge)
        except KeyError:
            if throw:
                raise
            return default
