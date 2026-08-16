"""Core hiera engine: hierarchy loading, key lookup, and interpolation."""

import logging
import os
import re
import string
import typing as _ty
from copy import deepcopy

from pathlib_next import Path

from .backends import (
    Backend,
    HOCONBackend,
    JSONBackend,
    SopsYAMLBackend,
    YAMLBackend,
    has_hocon,
)
from .exceptions import ConfigError, InterpolationError
from .util import LookupDict

__all__ = [
    "Merge",
    "ScopedHiera",
    "HieraLevel",
    "Hiera",
    "Sensitive",
    "make_merge",
    "default_backends",
]


def default_backends():
    """The default backend list: YAML, sops-YAML, JSON, and HOCON if available."""
    backends = [YAMLBackend, SopsYAMLBackend, JSONBackend]
    if has_hocon():
        backends.append(HOCONBackend)
    return backends

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


#: Sentinel for "no such context reference" (``None`` is a legitimate value).
_MISSING = object()


def _ctx_lookup(context, name, default=_MISSING):
    """Resolve a possibly-dotted context reference against nested containers.

    Hiera 5 defines ``%{trusted.certname}`` as *nested key access*, so a
    dotted name walks into nested dicts (and indexes lists with numeric
    segments), mirroring :meth:`LookupDict.lookup`.

    A flat key that literally contains dots wins over the nested walk, so an
    explicit ``{"a.b": 1}`` context entry keeps working.
    """
    if not isinstance(context, dict):
        return default
    if name in context:
        return context[name]
    if "." not in name:
        return default
    obj = context
    for segment in name.split("."):
        if isinstance(obj, dict):
            if segment not in obj:
                return default
            obj = obj[segment]
        elif isinstance(obj, (list, tuple)):
            try:
                obj = obj[int(segment)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return obj


class _ContextFormatter(string.Formatter):
    """``str.format`` where a dotted field is *mapping*, not attribute, access.

    ``"{a.b}".format_map({"a": {"b": 1}})`` raises ``AttributeError`` because
    ``str.format`` reads ``.b`` as an attribute. Hierarchy sources are full of
    dotted references (``%{trusted.certname}``), and the contexts they resolve
    against are plain dicts — so the default behavior is never what hiera
    wants. Overriding ``get_field`` routes the whole dotted name through
    :func:`_ctx_lookup` instead of letting ``str.format`` split it.
    """

    def get_field(self, field_name, args, kwargs):
        value = _ctx_lookup(kwargs, field_name)
        if value is _MISSING:
            # KeyError is the signal callers already use to skip a level.
            raise KeyError(field_name)
        return value, field_name


_FORMATTER = _ContextFormatter()


def _format_source(source: str, context: dict) -> str:
    """Format a normalized source/template against a (possibly nested) context."""
    return _FORMATTER.vformat(source, (), context)


#: Strategy names that build a Merge; also mapped from the legacy type API.
_MERGE_STRATEGIES = {"first", "unique", "hash", "deep"}

#: Legacy type-based merge= values -> strategy name.
_TYPE_TO_STRATEGY = {list: "unique", set: "unique", dict: "hash"}

_UNSET = object()


class Sensitive(object):
    """Thin marker wrapping a value flagged ``Sensitive`` via ``convert_to``.

    ``str()`` redacts; ``.unwrap()`` returns the real value. Mirrors Puppet's
    Sensitive type without pulling in a dependency.
    """

    def __init__(self, value):
        self._value = value

    def unwrap(self):
        return self._value

    def __repr__(self):
        return "Sensitive(<redacted>)"

    __str__ = __repr__


def _is_regex(pattern):
    """Hiera 5's rule: a lookup_options key is a regex only when ``^``-anchored.

    The previous metacharacter heuristic made any key containing ``.``/``$``/
    ``(`` etc. a pattern, so a literal dotted key like ``db.port`` silently
    regex-matched unrelated keys such as ``dbxport``.
    """
    return isinstance(pattern, str) and pattern.startswith("^")


def _convert_to(value, spec):
    """Best-effort ``convert_to`` cast. Unknown types leave the value as-is.

    ``spec`` is a type name (``"Integer"``) or ``[name, *args]``. Kept
    dependency-free and non-raising so unattended lookups never crash on a
    cast; a failed/unknown cast logs at debug and returns the original value.
    """
    args = []
    if isinstance(spec, (list, tuple)):
        name, args = spec[0], list(spec[1:])
    else:
        name = spec
    try:
        if name == "Integer":
            return int(value, *(args or []))
        if name == "Float":
            return float(value)
        if name == "String":
            return str(value)
        if name == "Boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "1", "on")
            return bool(value)
        if name == "Array":
            if isinstance(value, list):
                return value
            return [value]
        if name == "Sensitive":
            return Sensitive(value)
    except (ValueError, TypeError) as e:
        LOGGER.debug("convert_to %s failed for %r: %s", name, value, e)
        return value
    LOGGER.debug("convert_to: unknown type %r; leaving value unchanged", name)
    return value


def make_merge(spec):
    """Build a :class:`Merge` from a caller ``merge=`` spec, or ``None``.

    Accepts a strategy name (``"first"``/``"unique"``/``"hash"``/``"deep"``),
    a legacy type (``list``/``set``/``dict``), or a hash
    ``{"strategy": "deep", "knockout_prefix": "--", ...}``. ``None``/``"first"``
    yields ``None`` (first-match-wins, no accumulation).
    """
    if spec is None:
        return None
    options: dict = {}
    if isinstance(spec, dict):
        options = dict(spec)
        strategy = options.pop("strategy", None) or options.pop("merge", None)
    elif isinstance(spec, type):
        strategy = _TYPE_TO_STRATEGY.get(spec)
        if strategy is None:
            raise ValueError("Unsupported merge type: {!r}".format(spec))
    else:
        strategy = spec
    if strategy in (None, "first"):
        return None
    if strategy not in _MERGE_STRATEGIES:
        raise ValueError("Unknown merge strategy: {!r}".format(strategy))
    return Merge(strategy, **options)


class Merge(object):
    """Accumulates matches across the hierarchy per a merge strategy.

    Strategies: ``unique`` (flatten scalars+arrays, dedupe, first-seen order),
    ``hash`` (shallow, higher priority wins per key), ``deep`` (recursive, with
    ``knockout_prefix`` / ``sort_merged_arrays`` / ``merge_hash_arrays``).
    """

    def __init__(
        self,
        strategy,
        knockout_prefix=None,
        sort_merged_arrays=False,
        merge_hash_arrays=False,
        **_ignored
    ):
        self.strategy = strategy
        self.knockout_prefix = knockout_prefix
        self.sort_merged_arrays = sort_merged_arrays
        self.merge_hash_arrays = merge_hash_arrays
        # Back-compat attributes used elsewhere.
        self.deep = strategy == "deep"
        self.typ = {"hash": dict, "deep": dict}.get(strategy, list)

        if strategy == "unique":
            self.value = []
        elif strategy == "hash":
            self.value = LookupDict()
        elif strategy == "deep":
            # Deep values may be dicts OR lists; let the first match set the
            # type rather than presuming a dict.
            self.value = _UNSET
        else:
            self.value = None

    def merge_value(self, value):
        if self.strategy == "unique":
            if isinstance(value, (list, tuple, set)):
                self.value += list(value)
            else:
                self.value.append(value)
        elif self.strategy == "hash":
            if isinstance(value, dict):
                for k, v in value.items():
                    if k not in self.value:  # higher priority (earlier) wins
                        self.value[k] = v
        elif self.strategy == "deep":
            self.value = self.deep_merge(self.value, value)
        else:  # pragma: no cover - first never accumulates
            self.value = value

    def finalize(self):
        """Return the accumulated value, applying post-merge normalization."""
        if self.strategy == "unique":
            seen: list = []
            for item in self.value:
                if item not in seen:
                    seen.append(item)
            if self.sort_merged_arrays:
                try:
                    seen.sort()
                except TypeError:
                    pass
            return seen
        if self.strategy == "deep":
            value = self._knockout(self.value)
            if self.sort_merged_arrays:
                value = self._sort_arrays(value)
            return value
        return self.value

    def _sort_arrays(self, obj):
        """Recursively sort every list in a merged structure, best-effort.

        ``sort_merged_arrays`` is a deep-merge option in Puppet, so it has to
        reach lists nested anywhere in the result, not just a top-level one.
        Heterogeneous lists have no total order in Python 3; those are left
        in merge order rather than failing the whole lookup.
        """
        if isinstance(obj, dict):
            out = LookupDict()
            for k, v in obj.items():
                out[k] = self._sort_arrays(v)
            return out
        if isinstance(obj, list):
            items = [self._sort_arrays(item) for item in obj]
            try:
                items.sort()
            except TypeError:
                pass
            return items
        return obj

    def _knockout(self, obj):
        """Apply knockout_prefix removals to a merged structure."""
        prefix = self.knockout_prefix
        if not prefix:
            return obj
        if isinstance(obj, dict):
            removed = {
                k[len(prefix):]
                for k in obj
                if isinstance(k, str) and k.startswith(prefix)
            }
            out = LookupDict()
            for k, v in obj.items():
                if isinstance(k, str) and k.startswith(prefix):
                    continue
                if k in removed:
                    continue
                out[k] = self._knockout(v)
            return out
        if isinstance(obj, list):
            drop = {
                item[len(prefix):]
                for item in obj
                if isinstance(item, str) and item.startswith(prefix)
            }
            return [
                self._knockout(item)
                for item in obj
                if not (isinstance(item, str) and item.startswith(prefix))
                and item not in drop
            ]
        return obj

    def deep_merge(self, a, b):
        """Recursively merge ``b`` (lower priority) into ``a`` (higher).

        Hashes recurse; lists concatenate (deduped); a scalar already present
        in ``a`` wins — later levels never clobber an earlier level's value.
        With ``merge_hash_arrays``, equal-length lists of dicts are merged
        element-wise by index."""
        if a is _UNSET or a is None:
            return deepcopy(b)
        if isinstance(a, list) and isinstance(b, list):
            return self._merge_lists(a, b)
        if not isinstance(a, dict) or not isinstance(b, dict):
            # Mismatched or scalar types: higher-priority (a) wins.
            return a
        result = deepcopy(a)
        for k, v in b.items():
            if k in result and isinstance(result[k], dict):
                result[k] = self.deep_merge(result[k], v)
            elif k in result and isinstance(result[k], list):
                result[k] = self._merge_lists(result[k], v)
            elif k not in result:
                # Key only in the lower-priority side; take it. An existing
                # scalar in ``result`` (higher priority) is left untouched.
                result[k] = deepcopy(v)
        return result

    def _merge_lists(self, a, v):
        if not isinstance(v, list):
            out = list(a)
            out.append(v)
            return out
        if self.merge_hash_arrays and len(a) == len(v) and all(
            isinstance(x, dict) for x in a
        ) and all(isinstance(x, dict) for x in v):
            return [self.deep_merge(x, y) for x, y in zip(a, v)]
        extra = [item for item in v if item not in a]
        return a + deepcopy(extra)


class ScopedHiera(object):
    def __init__(self, hiera, context=None):
        self.hiera = hiera
        self.context = context or {}

    def has(self, key, context=None, **kwargs):
        # Same layering as .get(): the bound context goes *under* per-call
        # overrides. The old `kwargs.update(self.context)` inverted this, so
        # a scoped .has() disagreed with the equivalent .get().
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
    #: ``(collection_var, item_var, template)`` for a mapped_paths level, else None.
    mapped: "tuple" = None

    @classmethod
    def new(cls, conf: dict, backend: Backend) -> "HieraLevel":
        sources: "list[str]" = []
        is_glob = False
        mapped = None
        path = conf.get("path")
        paths = conf.get("paths")
        glob = conf.get("glob")
        globs = conf.get("globs")
        mapped_paths = conf.get("mapped_paths")
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
        elif mapped_paths:
            # [collection_var, item_var, template]
            collection_var, item_var, template = mapped_paths
            mapped = (collection_var, item_var, _normalize_source(template))

        return HieraLevel(
            backend,
            [_normalize_source(source) for source in sources if source],
            is_glob,
            mapped,
        )

    def paths(self, base_path: Path, context: dict):
        """Yield the candidate source paths for this level in a given context.

        Glob levels expand their patterns against the filesystem (sorted for
        determinism); mapped_paths bind each element of a context collection to
        the item var and format the template; literal levels format each source
        with the context. Sources referencing an absent context var are skipped.
        """
        try:
            datadir = _format_source(self.backend.datadir, context)
        except (KeyError, IndexError, TypeError, AttributeError):
            return
        root = base_path / datadir

        if self.mapped:
            collection_var, item_var, template = self.mapped
            collection = context.get(collection_var)
            if collection is None:
                return
            if isinstance(collection, dict):
                items = list(collection.values())
            elif isinstance(collection, (list, tuple, set)):
                items = list(collection)
            else:
                items = [collection]
            for item in items:
                mapped_ctx = dict(context)
                mapped_ctx[item_var] = item
                try:
                    yield root / _format_source(template, mapped_ctx)
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
            return

        for source in self.sources:
            try:
                rel = _format_source(source, context)
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
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
        self.default_hierarchy: "list[HieraLevel]" = []
        self.cache: dict = {}
        #: Per-context cache of resolved source path lists (see ``sources``).
        self._source_cache: dict = {}

        self.load(backends or default_backends(), base_path)

    def buildcontext(self, context: dict = None, **kwargs) -> dict:
        new_context = {}
        new_context.update(self.context)
        new_context.update(context or {})
        new_context.update(kwargs)
        # Filter out empty/None values so they don't satisfy a format field.
        return {k: v for k, v in new_context.items() if v}

    def format(self, text: str, context: dict = None, **kwargs) -> str:
        context = self.buildcontext(context, **kwargs)
        return _format_source(_normalize_source(text), context)

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

        version = self.base.get("version")
        if version is not None and version != 5:
            raise ConfigError(
                "Unsupported hiera config version {!r}; this implements "
                "version 5".format(version)
            )

        hierarchy = self.base.get("hierarchy")
        defaults = self.base.get("defaults") or {}
        if hierarchy is None:
            raise ConfigError("Invalid base Hiera config: missing 'hierarchy' key")

        defaults.setdefault("data_dir", DEFAULT_DATA_DIR)
        self.hierarchy = self._build_levels(hierarchy, defaults)
        self.default_hierarchy = self._build_levels(
            self.base.get("default_hierarchy") or [], defaults
        )

        # Pre-load/cache global (context-free) data.
        self.get(None)

    def _build_levels(self, hierarchy, defaults):
        levels: "list[HieraLevel]" = []
        for level in hierarchy:
            conf = {**level}
            for k, v in defaults.items():
                conf.setdefault(k, v)
            data_hash = conf.get("data_hash")
            if data_hash is None:
                raise ConfigError(
                    "Hierarchy level {!r} is missing a 'data_hash' backend "
                    "(only file-based data_hash backends are supported)".format(
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
            levels.append(HieraLevel.new(conf, backend))
        return levels

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
                # Inline interpolation needs a single value; do not thread the
                # parent's array/hash merge into the referenced key.
                try:
                    replace = self.get_key(arg, paths, context, None)
                except KeyError:
                    replace = None
            elif call == "scope":
                # Dotted names resolve as nested lookups here too, so
                # %{scope('facts.os')} agrees with %{facts.os}.
                replace = _ctx_lookup(context, arg, None)
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
            # Dotted names are nested lookups here too, so a reference means
            # the same thing in a value as it does in a hierarchy path.
            replacement = _ctx_lookup(context, i, "") or ""
            s = interpolate.sub(lambda _m, r=str(replacement): r, s, 1)
        return s

    def resolve(self, s, paths, context, merge):
        """Fully resolve ``s``: functions, interpolation, and nested structures.

        ``merge`` is only meaningful for a top-level ``%{alias(key)}`` (which
        may carry the caller's merge onto the aliased key). Nested structure
        elements resolve without it — accumulation happens once, in get_key.
        """
        if isinstance(s, dict):
            return self.resolve_dict(s, paths, context, None)
        elif isinstance(s, list):
            return list(self.resolve_list(s, paths, context, None))
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
                    value = self.resolve(cache, paths, context, merge)
                    if merge is None:
                        return value
                    merge.merge_value(value)
                    found = True

        if merge is not None and found:
            return merge.finalize()

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

    def has(self, key, context=None, **kwargs) -> bool:
        """Return True if ``key`` exists in hiera, False otherwise.

        ``context``/``kwargs`` layer over the instance context exactly as in
        :meth:`get`.
        """
        try:
            self.get(key, throw=True, context=context, **kwargs)
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
        return self._files_for(self.hierarchy, context, _load, "main")

    def _files_for(self, hierarchy, context, _load, tag):
        try:
            cache_key = (tag, _load, frozenset(context.items()))
        except TypeError:
            # An unhashable context value (e.g. a list) — skip caching.
            cache_key = None
        if cache_key is not None:
            cached = self._source_cache.get(cache_key)
            if cached is not None:
                return list(cached)

        files = []
        for level in hierarchy:
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

    def _default_files(self, context):
        return self._files_for(self.default_hierarchy, context, True, "default")

    def _lookup_options_for(self, key, files, context):
        """Return the merged ``lookup_options`` entry matching ``key``, or None.

        ``lookup_options`` is a reserved data key: ``{pattern: {merge, convert_to}}``.
        Higher-priority (earlier) levels win per pattern. An exact key match
        wins over a regex pattern match; the first regex match otherwise wins.
        """
        try:
            options = self.get_key("lookup_options", files, context, make_merge("hash"))
        except KeyError:
            return None
        if not isinstance(options, dict):
            return None
        if key in options and isinstance(options[key], dict):
            return options[key]
        for pattern, entry in options.items():
            if not isinstance(entry, dict):
                continue
            if pattern == key:
                return entry
            if _is_regex(pattern):
                try:
                    if re.fullmatch(pattern, key):
                        return entry
                except re.error:
                    continue
        return None

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
        new_context = self.buildcontext(context, **kwargs)
        # Resolve sources against the *built* context: per-call **kwargs are
        # documented context overrides, so they must reach hierarchy path
        # resolution too, not just interpolation and lookup_options. (The
        # default_hierarchy retry below has always used new_context; passing
        # the raw `context` here let the two hierarchies disagree.)
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
            value = self.get_key(key, files, new_context, merge=merge_obj)
        except KeyError:
            if self.default_hierarchy:
                try:
                    value = self.get_key(
                        key,
                        self._default_files(new_context),
                        new_context,
                        merge=make_merge(merge) if explicit else merge_obj,
                    )
                except KeyError:
                    if throw:
                        raise
                    return default
            elif throw:
                raise
            else:
                return default

        if convert_to is not None:
            value = _convert_to(value, convert_to)
        return value
