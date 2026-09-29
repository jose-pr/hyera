# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Hiera configuration: loading base config, building hierarchies and levels.

Ports Puppet's ``pops/lookup/hiera_config.rb``.
"""

import copy
import os
import re
import typing as _ty

from pathlib_next import Path

from .backends import Backend, YAMLBackend
from .exceptions import BackendError, ConfigError
from ._interpolation import _normalize_source
from ._location_resolver import _resolve_level_paths
from ._yaml_loader import symkeys_to_string

#: Default puppet-style data dir, used when a hierarchy omits ``datadir``.
DEFAULT_DATA_DIR = "/etc/puppetlabs/code/environments/%{environment}/hieradata"

#: Puppet's built-in default configuration, used when hiera.yaml does not
#: exist (``hiera_config.rb:728-740``, ``HieraConfigV5::DEFAULT_CONFIG_HASH``).
#: Every use deep-copies this -- never mutate it in place.
DEFAULT_CONFIG_HASH = {
    "version": 5,
    "defaults": {"datadir": "data", "data_hash": "yaml_data"},
    "hierarchy": [{"name": "Common", "path": "common.yaml"}],
}


class _ConfigSource(_ty.NamedTuple):
    """Where a base config came from: for error messages and, later, lines.

    ``label`` is what a Puppet-style message names as "The Lookup
    Configuration at '<label>'": an absolute path for a path-configured
    hiera.yaml, a stream's ``.name`` (else ``"<stream>"``), ``"<dict>"`` for
    a dict config, or ``"<default>"`` for ``Hiera(None, ...)``.
    """

    label: str
    #: The file path for a ``ConfigError.path``, else ``None`` (a dict or a
    #: stream with no real file to point at).
    path: "_ty.Optional[str]"
    #: The raw YAML source, so a later error can locate a line. ``None`` for
    #: a dict or default config, which have no text to walk.
    text: "_ty.Optional[str]"
    #: The absolute root relative paths/datadirs resolve against.
    root: Path


def _config_error(source: "_ConfigSource", message: str, line=None) -> ConfigError:
    """Puppet's ``LookupError`` file/line suffix (``issues.rb:841-871``),
    appended to ``message``. Returned, not raised -- callers ``raise`` it."""
    path = source.path if source else None
    if path and line:
        message = "{} (file: {}, line: {})".format(message, path, line)
    elif path:
        message = "{} (file: {})".format(message, path)
    elif line:
        message = "{} (line: {})".format(message, line)
    return ConfigError(message, path=path, line=line)


def _type_error(source: "_ConfigSource", detail: str, line=None) -> ConfigError:
    """Puppet's ``The Lookup Configuration at '<label>' has wrong type, ...``
    (``hiera_config.rb``'s ``Types::TypeMismatchDescriber`` on ``CONFIG_TYPE``)."""
    label = source.label if source else "<dict>"
    message = "The Lookup Configuration at '{}' has wrong type, {}".format(
        label, detail
    )
    if line:
        message = "{} (line: {})".format(message, line)
    path = source.path if source else None
    return ConfigError(message, path=path, line=line)


def _ruby_type_name(value) -> str:
    """The Puppet type name a value of this Python type reports as."""
    if isinstance(value, bool):
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
        return "Tuple"
    if isinstance(value, dict):
        if value and all(isinstance(k, str) for k in value):
            return "Struct"
        return "Hash"
    return type(value).__name__


_VERSION_LEADING_INT = re.compile(r"\s*[+-]?\d+")


def _select_version(data: dict, source: "_ConfigSource") -> None:
    """Puppet's version dispatch (``hiera_config.rb:153-167``).

    Raises :class:`ConfigError` for anything but a literal Integer ``5``.
    Version 3/4 parsing is ``hiera_v3_v4_configs``' job; here both a missing
    ``version`` (Hiera 3's own signal) and an explicit ``3``/``4`` just name
    what they are, unsupported for now.
    """
    v = data.get("version")
    _v3_text = (
        "hiera.yaml version 3 is not supported yet (a hiera.yaml without "
        "'version' is version 3)"
    )
    if v is None:
        raise _config_error(source, _v3_text)
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise _type_error(
            source,
            "entry 'version' expects an Integer value, got {}".format(
                _ruby_type_name(v)
            ),
        )
    if isinstance(v, str):
        m = _VERSION_LEADING_INT.match(v)
        n = int(m.group()) if m else 0
    elif isinstance(v, float):
        n = int(v)
    else:
        n = v
    if n == 5:
        if not isinstance(v, int):
            raise _type_error(
                source,
                "entry 'version' expects an Integer value, got {}".format(
                    _ruby_type_name(v)
                ),
            )
        return
    if n == 3:
        raise _config_error(source, _v3_text)
    if n == 4:
        raise _config_error(
            source, "hiera.yaml version 4 cannot be used in the global layer"
        )
    raise _config_error(
        source, "This runtime does not support hiera.yaml version {}".format(n)
    )


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

        return cls(
            backend,
            [_normalize_source(source) for source in sources if source],
            is_glob,
            mapped,
        )

    def paths(self, base_path: Path, context: dict):
        """Yield the candidate source paths for this level in a given context."""
        return _resolve_level_paths(self, base_path, context)


def _read_base_config(base_config, base_path) -> "_ty.Tuple[_ConfigSource, dict]":
    """Load the base configuration (``HieraConfig.create``, ``hiera_config.rb:127-168``).

    Returns ``(source, base)``: ``base`` is a dict this call owns outright
    (a deep copy of a dict/default config, or a freshly parsed file) --
    ``self.base_config`` keeps the caller's own argument untouched, and a
    dict passed in is never mutated. Raises :class:`ConfigError`
    (``.path`` set for a path- or stream-configured hiera.yaml) on any read,
    parse, or top-level-shape problem.
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

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConfigError("({}): {}".format(label, e), path=path) from e
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
    # `_yaml_loader.safe_load`'s BOM-swap) handles it the same way.
    try:
        base = YAMLBackend().loads(text)
    except BackendError as e:
        raise ConfigError("({}): {}".format(label, e), path=path) from e
    if isinstance(base, dict):
        # hiera_config.rb:181 -- symbol keys (however written) become
        # plain strings for every config version, not just data files.
        base = symkeys_to_string(base)

    if not isinstance(base, dict):
        raise _config_error(
            source,
            "File exists but does not contain a valid YAML hash; Puppet "
            "falls back to the Hiera version 3 default config, which is "
            "not supported yet",
        )

    return source, base


def _build_hierarchies(base, backends):
    """Build ``hierarchy`` and ``default_hierarchy`` from base config.

    Returns ``(hierarchy_levels, default_hierarchy_levels)``.
    """
    version = base.get("version")
    if version is not None and version != 5:
        raise ConfigError(
            "Unsupported hiera config version {!r}; this implements "
            "version 5".format(version)
        )

    hierarchy = base.get("hierarchy")
    defaults = base.get("defaults") or {}
    if hierarchy is None:
        raise ConfigError("Invalid base Hiera config: missing 'hierarchy' key")

    defaults.setdefault("data_dir", DEFAULT_DATA_DIR)

    backend_levels = _build_levels(hierarchy, defaults, backends)
    default_levels = _build_levels(
        base.get("default_hierarchy") or [], defaults, backends
    )

    return backend_levels, default_levels


def _build_levels(hierarchy, defaults, backends):
    """Build HieraLevel instances from hierarchy configuration.

    ``backends`` is an allow-list of :class:`~hyera.backends.Backend`
    subclasses: ``data_hash`` names are resolved against the
    process-global registry (:meth:`Backend.find`), then checked against
    this allow-list, so a name registered by a third party but not passed
    to ``Hiera(backends=...)`` is refused exactly like an unknown one.
    """
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
        backend_cls = Backend.find(data_hash, kind="function")
        if backend_cls is None or backend_cls not in backends:
            allowed_names = [
                name
                for name in Backend.names("function")
                if Backend.find(name, "function") in backends
            ]
            raise ConfigError(
                "Unable to find 'data_hash' function named '{}'; known: {}".format(
                    data_hash, ", ".join(allowed_names)
                )
            ) from None
        # Normalize datadir spelling for the backend.
        conf.setdefault("datadir", conf.get("data_dir"))
        backend = Backend.new(data_hash, conf, kind="function")
        backend.datadir = _normalize_source(backend.datadir)
        levels.append(HieraLevel.new(conf, backend))
    return levels
