# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb, context.rb,
# lib/puppet/pops/issues.rb, pops/types/type_mismatch_describer.rb,
# lib/puppet/util/run_mode.rb, lib/puppet/pops/lookup/location_resolver.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Hiera configuration: loading base config, building hierarchies and levels.

Ports Puppet's ``pops/lookup/hiera_config.rb``, with the messages and
helpers it cites from ``issues.rb``, ``type_mismatch_describer.rb``,
``util/run_mode.rb`` and ``location_resolver.rb``.
"""

from __future__ import annotations

import copy
import logging
import os
import typing as _ty

import yaml
from pathlib_next import Path

from ..backends import Backend, YAMLBackend
from ..exceptions import BackendError, ConfigError
from .location_resolver import resolve_locations
from .._scope.scope import Scope
from ..backends._psych import symkeys_to_string
from .._enums import _StrEnum, _plain
from .config_source import (
    _ConfigSource,
    _config_error,
)
from .config_v5 import (
    _ALL_FUNCTION_KEYS,
    _FUNCTION_KEYS,
    _LOCATION_KEYS,
    _config_line,
)

_LOGGER = logging.getLogger(__name__)

#: Puppet's built-in default configuration, used when hiera.yaml does not
#: exist (``hiera_config.rb:728-740``, ``HieraConfigV5::DEFAULT_CONFIG_HASH``).
#: Every use deep-copies this -- never mutate it in place.
DEFAULT_CONFIG_HASH = {
    "version": 5,
    "defaults": {"datadir": "data", "data_hash": "yaml_data"},
    "hierarchy": [{"name": "Common", "path": "common.yaml"}],
}

#: Puppet's Hiera 3 default configuration (``hiera_config.rb:433-437``,
#: ``HieraConfigV3::DEFAULT_CONFIG_HASH``): used both as the ``||=`` fill for
#: missing/``false`` top-level v3 keys (:func:`_fill_v3_defaults`) and, via
#: :func:`_read_base_config`, when a hiera.yaml exists but does not parse to
#: a YAML hash at all (Puppet falls back to this, then reads it as v3, at
#: every layer -- ``hiera_config.rb:139-144``). Every use deep-copies this.
V3_DEFAULT_CONFIG_HASH = {
    "backends": ["yaml"],
    "hierarchy": ["nodes/%{::trusted.certname}", "common"],
    "merge_behavior": "native",
}


def _fill_v5_defaults(data: dict) -> None:
    """Puppet's ``defaults ||=``/``hierarchy ||=`` fill
    (``validate_config``, ``hiera_config.rb:742-745``), run after
    :func:`_config_version`. ``is``, not ``==``: ``0 == False`` in Python,
    and Puppet's ``||=`` triggers on Ruby ``nil``/``false`` alike."""
    if data.get("defaults") is None or data.get("defaults") is False:
        data["defaults"] = copy.deepcopy(DEFAULT_CONFIG_HASH["defaults"])
    if data.get("hierarchy") is None or data.get("hierarchy") is False:
        data["hierarchy"] = copy.deepcopy(DEFAULT_CONFIG_HASH["hierarchy"])


def _warn_deprecated(source: "_ConfigSource", version: int, scope) -> None:
    """Puppet's per-version deprecation warning (``hiera_config.rb:85``,
    ``:440``, ``:554``): logged once per read, silenced only when
    ``scope.strict == "off"`` (locations stay lenient regardless)."""
    if scope is not None and getattr(scope, "strict", None) == "off":
        return
    _LOGGER.warning(
        "%s: Use of 'hiera.yaml' version %d is deprecated. It should be "
        "converted to version 5",
        source.label,
        version,
    )


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


def _freeze(value):
    """``value`` with every list, dict and set replaced by a hashable
    equivalent, so a :class:`HieraLevel` with ``options`` can be hashed."""
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return frozenset((k, _freeze(v)) for k, v in value.items())
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(v) for v in value)
    return value


class FunctionKind(_StrEnum):
    """A hierarchy entry's resolved function kind: :attr:`HieraLevel.kind`,
    and the ``kind=`` argument of :meth:`HieraLevel.new`. Which Puppet
    Hiera 5 provider hook a level's backend implements."""

    DATA_HASH = "data_hash"
    """Reads a whole data source at once (``YAMLBackend``/``JSONBackend``/
    ``HOCONBackend``/...); the found values are merged across locations and
    levels by hyera itself, never by the backend."""

    LOOKUP_KEY = "lookup_key"
    """Resolves one root key itself, given a
    :class:`~hyera.LookupContext`."""

    DATA_DIG = "data_dig"
    """Resolves one full (possibly dotted) key itself, given a
    :class:`~hyera.LookupContext`."""


class HieraLevel(_ty.NamedTuple):
    """One hierarchy entry, stored exactly as written in hiera.yaml --
    ``locations`` are never interpolated or normalized here; that happens
    per lookup, against a bound :class:`~hyera.Scope`
    (:func:`~hyera._config.location_resolver.resolve_locations`)."""

    name: str
    backend: Backend
    datadir: str
    #: The one location key this entry declared (``"path"``, ``"paths"``,
    #: ``"glob"``, ``"globs"``, ``"uri"``, ``"uris"`` or ``"mapped_paths"``),
    #: or ``None`` for a location-less entry.
    location_key: "_ty.Optional[str]"
    #: The raw declared value(s), always a tuple: one string for a singular
    #: key, the strings as written for a plural one, and ``(collection_var,
    #: item_var, template)`` for ``mapped_paths``.
    locations: "_ty.Tuple[str, ...]"
    #: This entry's resolved function kind: ``"data_hash"``, ``"lookup_key"``
    #: or ``"data_dig"``.
    kind: str = "data_hash"
    #: The entry's own ``options``, else ``defaults``'s (never merged),
    #: exactly as declared -- interpolated per lookup, per scope, not here.
    options: "_ty.Optional[_ty.Dict[str, _ty.Any]]" = None
    #: A version 3/``hiera3_backend`` extension, appended to each declared
    #: ``path``/``paths`` location (after interpolation) unless it already
    #: ends with it (``location_resolver.rb:59-61``). ``None`` for a v4/v5
    #: level (Puppet appends the extension for those during config reading,
    #: not at lookup time -- see :func:`_v4_levels`/:func:`_build_levels`).
    extension: "_ty.Optional[str]" = None
    #: The root a version 3 level's ``datadir`` resolves against -- the
    #: process cwd *at construction*, never the hiera.yaml
    #: directory. ``None`` for a v4/v5 level, which uses the caller's own
    #: ``base_path``.
    datadir_base: "_ty.Optional[Path]" = None
    #: ``True`` for a version 4 level only: ``datadir`` is joined onto the
    #: config root literally, with no interpolation at all
    #: (``hiera_config.rb:525``, unlike v5's ``:664-665``) -- not even the
    #: strict, method-free substitution every other level's ``datadir``
    #: gets, since that would still trip over a literal ``%`` the way a
    #: plain string substitution attempt (``allow_methods=False`` rules out
    #: escaping it with ``%{literal('%')}``) cannot avoid.
    datadir_literal: bool = False
    #: ``True`` unless this is a version 3 or 4 level: only a version 5
    #: hierarchy resolves an undefined variable in a location to ``''`` under
    #: ``strict: error`` (``hiera_config.rb``'s ``avoid_hiera_interpolation_
    #: errors``), the older readers let it fail the lookup.
    lenient_locations: bool = True

    def __hash__(self) -> int:
        return hash(_freeze(tuple(self)))

    @classmethod
    def new(
        cls,
        conf: _ty.Dict[str, _ty.Any],
        backend: Backend,
        kind: _ty.Union[FunctionKind, str] = "data_hash",
        *,
        extension: _ty.Optional[str] = None,
        datadir_base: "_ty.Optional[Path]" = None,
        datadir_literal: bool = False,
        lenient_locations: bool = True,
    ) -> "HieraLevel":
        """Build a level from one already-resolved hierarchy entry
        ``conf`` (a hiera.yaml entry, or ``defaults`` filled in): reads
        ``conf["name"]``/``["datadir"]``, whichever location key is
        present, and ``conf.get("options")``, leaving every location
        uninterpolated.

        :param conf: the entry's own mapping, with ``defaults`` already
            merged in by the caller where the entry names nothing.
        :param backend: the resolved backend for this level's function.
        :param kind: this level's function kind (``"data_hash"``,
            ``"lookup_key"`` or ``"data_dig"``).
        :param extension: appended to a v3-derived location (``None`` for
            v4/v5).
        :param datadir_base: the root a v3 level's ``datadir`` resolves
            against (``None`` for v4/v5).
        :param datadir_literal: whether ``datadir`` is joined onto the
            config root literally, with no interpolation (v4 only).
        :param lenient_locations: whether an undefined variable in a location
            resolves to ``''`` under ``strict: error`` (``False`` for v3/v4).
        :returns: the built level.
        """
        location_key = next((k for k in _LOCATION_KEYS if k in conf), None)
        if location_key is None:
            locations: "_ty.Tuple[str, ...]" = ()
        elif location_key in ("paths", "globs", "uris", "mapped_paths"):
            locations = tuple(conf[location_key])
        else:
            locations = (conf[location_key],)
        kind = _plain(kind)
        return cls(
            name=conf["name"],
            backend=backend,
            datadir=conf.get("datadir", "data"),
            location_key=location_key,
            locations=locations,
            kind=kind,
            options=conf.get("options"),
            extension=extension,
            datadir_base=datadir_base,
            datadir_literal=datadir_literal,
            lenient_locations=lenient_locations,
        )

    def paths(self, base_path: Path, scope: Scope) -> "_ty.List[str]":
        """The candidate source (file) paths for this level in a bound
        :class:`~hyera.Scope`. A location-less entry, or one using ``uri``/
        ``uris`` (which never resolve to a filesystem path), yields ``[]``.

        :param base_path: the root relative locations resolve against.
        :param scope: the scope location templates interpolate against.
        :returns: the candidate paths, interpolated but not filtered by
            existence.
        """
        resolved = resolve_locations(self, base_path, scope)
        if resolved is None:
            return []
        return [loc.location for loc in resolved if not loc.is_uri]


def _read_base_config(base_config, base_path) -> "_ty.Tuple[_ConfigSource, dict]":
    """Load the base configuration (``HieraConfig.create``, ``hiera_config.rb:127-168``).

    Returns ``(source, base)``: ``base`` is a dict this call owns outright
    (a deep copy of a dict/default config, or a freshly parsed file) --
    ``self.base_config`` keeps the caller's own argument untouched, and a
    dict passed in is never mutated. Raises :class:`ConfigError`
    (``.path`` set for a path- or stream-configured hiera.yaml) on any read,
    parse, or top-level-shape problem.

    A file that parses but is not a YAML hash never raises here, at any
    layer: it logs Puppet's own warning and falls back to
    :data:`V3_DEFAULT_CONFIG_HASH` (``hiera_config.rb:139-144``), which
    :func:`_config_version` then reads as version 3 -- read in full by
    :func:`_read_v3` at the global layer, or left, like any other version-3
    layer config, for :func:`~hyera._config.data_provider.usable_provider` to ignore or raise
    about outside it.
    """
    if base_config is None:
        # Puppet's missing-file default (`hiera_config.rb:147-149`): no file
        # to point at, so `.path` stays None.
        root = Path(os.getcwd() if base_path is None else base_path).absolute()
        return _ConfigSource("<default>", None, None, root), copy.deepcopy(
            DEFAULT_CONFIG_HASH
        )

    if isinstance(base_config, dict):
        root = Path(os.getcwd() if base_path is None else base_path).absolute()
        return _ConfigSource("<dict>", None, None, root), copy.deepcopy(base_config)

    # Read once, hold no open handle: keeps the caller's path/stream free to
    # be replaced or pickled across, and leaves a stream at the caller's
    # mercy. Decoded as strict UTF-8 -- Puppet reads every data file this
    # way (``context.rb:53``) and ``hiera_config.rb`` parses ``hiera.yaml``
    # with the same ``safe_load`` data files use.
    if hasattr(base_config, "read"):
        name = getattr(base_config, "name", None)
        label = str(name) if name else "<stream>"
        path = str(name) if name else None
        root = Path(os.getcwd() if base_path is None else base_path).absolute()
        content = base_config.read()
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
    else:
        configpath = Path(base_config).absolute()
        root = (
            Path(base_path).absolute() if base_path is not None else configpath.parent
        )
        label = str(configpath)
        path = label
        if configpath.is_dir():
            raise ConfigError(
                "Unable to read the Lookup Configuration at '{}': Is a "
                "directory".format(label),
                path=path,
            )
        try:
            raw = configpath.read_bytes()
        except OSError as e:
            raise ConfigError(
                "Unable to read the Lookup Configuration at '{}': {}".format(
                    label, e.strerror or e
                ),
                path=path,
            ) from e

    # Raised after the handler so no chained exception keeps the bytes
    # (`UnicodeDecodeError.object`).
    problem = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        problem = str(e)
    if problem is not None:
        raise ConfigError("({}): {}".format(label, problem), path=path)
    source = _ConfigSource(label, path, text, root)
    # `puppet lookup` reads hiera.yaml via `HieraConfig.create` ->
    # `cached_file_data` -> `Puppet::Util::Yaml.safe_load(content, ...)`
    # directly on the file's content -- *not* through
    # `Puppet::Util::Yaml.safe_load_file`'s BOM-stripping
    # `Puppet::FileSystem.read(path, encoding: "bom|utf-8")`. A leading
    # BOM therefore reaches `YAML.safe_load` exactly as it does for a
    # data file (`context.rb:53`), keeping a literal U+FEFF character;
    # measured 2026-09-29 against real Puppet 8.10.0 on a
    # `<BOM>---\nversion: 5\n...` config (`config-hiera-yaml-bom`):
    # Puppet errors identically to a data file with the same content, so
    # this is *not* stripped here -- `YAMLBackend.loads` (via
    # `_psych.safe_load`'s BOM-swap) handles it the same way.
    try:
        base = YAMLBackend().loads(text)
    except BackendError as e:
        # The cause is the loader's own chain-free error: one line, no text.
        raise ConfigError("({}): {}".format(label, e), path=path) from e
    if isinstance(base, dict):
        # hiera_config.rb:181 -- symbol keys (however written) become
        # plain strings for every config version, not just data files.
        base = symkeys_to_string(base)

    if not isinstance(base, dict):
        _LOGGER.warning(
            "%s: File exists but does not contain a valid YAML hash. "
            "Falling back to Hiera version 3 default config",
            source.label,
        )
        return source, copy.deepcopy(V3_DEFAULT_CONFIG_HASH)

    return source, base


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
    struct type places on it (``hiera_config.rb``'s ``@@CONFIG_TYPE``,
    confirmed against a real Puppet 8.10 run in
    ``config-defaults-hiera3-backend-key``). Puppet's own
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
        # Puppet resolves the function (name and kind alike) only when it
        # calls it for an existing location, so a level naming an unknown
        # function is built anyway and raises on its first such call (see
        # `_lookup.function_provider`); every other level keeps answering.
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
            # Global-only (_validate_v5 already rejected this entry
            # everywhere else); replaces a v5 data_hash function with a
            # registered v3-namespace backend, matching Puppet's own
            # strip-then-Hiera-3-appends extension rule
            # (hiera_config.rb:692-714, hiera/backend.rb:57-58) by giving
            # it the same "append unless already present" extension a v3
            # `backends:` entry gets.
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
