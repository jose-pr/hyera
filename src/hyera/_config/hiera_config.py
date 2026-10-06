# Ported from Puppet 8 lib/puppet/pops/lookup/{hiera_config,context,location_resolver}.rb,
# pops/issues.rb, pops/types/type_mismatch_describer.rb, util/run_mode.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr. See NOTICE.
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

from pathlib_next import Path

from ..backends import Backend, YAMLBackend
from ..exceptions import BackendError, ConfigError
from .location_resolver import resolve_locations
from .._scope.scope import Scope
from ..backends._psych import symkeys_to_string
from .._enums import _StrEnum, _plain
from .config_source import _ConfigSource
from .config_v5 import _LOCATION_KEYS

_LOGGER = logging.getLogger(__name__)

#: Puppet's built-in default configuration, used when hiera.yaml does not
#: exist (``hiera_config.rb:728-740``, ``HieraConfigV5::DEFAULT_CONFIG_HASH``).
#: Every use deep-copies this -- never mutate it in place.
DEFAULT_CONFIG_HASH = {
    "version": 5,
    "defaults": {"datadir": "data", "data_hash": "yaml_data"},
    "hierarchy": [{"name": "Common", "path": "common.yaml"}],
}

#: Puppet's Hiera 3 default configuration (``hiera_config.rb:433-437``): fills missing or
#: ``false`` top-level v3 keys, and is read as v3 when a hiera.yaml parses to no hash
#: (``hiera_config.rb:139-144``). Every use deep-copies it.
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
    #: A version 3/``hiera3_backend`` extension, appended to each declared ``path``/``paths``
    #: location (after interpolation) unless already present (``location_resolver.rb:59-61``).
    #: ``None`` for a v4/v5 level, whose extension is applied while reading the config.
    extension: "_ty.Optional[str]" = None
    #: The root a version 3 level's ``datadir`` resolves against: the process cwd at
    #: construction, never the hiera.yaml directory. ``None`` for a v4/v5 level, which uses
    #: the caller's ``base_path``.
    datadir_base: "_ty.Optional[Path]" = None
    #: ``True`` for a version 4 level only: ``datadir`` is joined onto the config root
    #: literally, with no interpolation (``hiera_config.rb:525``, unlike v5's ``:664-665``),
    #: so a literal ``%`` survives.
    datadir_literal: bool = False
    #: ``True`` unless this is a version 3 or 4 level: only a version 5 hierarchy resolves an
    #: undefined variable in a location to ``''`` under ``strict: error``
    #: (``avoid_hiera_interpolation_errors``); the older readers fail the lookup.
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

    # Read once, holding no open handle, so the caller's path or stream stays free to be
    # replaced or pickled. Decoded as strict UTF-8, as Puppet reads every data file (``context.rb:53``).
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
    # Puppet parses hiera.yaml's raw content with `Util::Yaml.safe_load`, not the BOM-stripping
    # `safe_load_file`, so a leading BOM reaches the parser as in a data file (`context.rb:53`)
    # and errors the same way; it is not stripped here (`YAMLBackend.loads` swaps it for a space).
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
