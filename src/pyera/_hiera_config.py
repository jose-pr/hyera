# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Hiera configuration: loading base config, building hierarchies and levels.

Ports Puppet's ``pops/lookup/hiera_config.rb``.
"""

import os
import typing as _ty

from pathlib_next import Path

from .backends import Backend, YAMLBackend
from .exceptions import ConfigError
from ._interpolation import _normalize_source
from ._location_resolver import _resolve_level_paths

#: Default puppet-style data dir, used when a hierarchy omits ``datadir``.
DEFAULT_DATA_DIR = "/etc/puppetlabs/code/environments/%{environment}/hieradata"


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
        """Yield the candidate source paths for this level in a given context."""
        return _resolve_level_paths(self, base_path, context)


def _read_base_config(base_config, base_path):
    """Load and validate the base configuration.

    Returns ``(base_config_dict, base_path)`` after reading and normalizing.
    """
    if isinstance(base_config, dict):
        base = base_config
        base_path = Path(os.getcwd() if base_path is None else base_path)
    else:
        if not hasattr(base_config, "read"):
            # Read once, as bytes, and hold no open handle: keeps the
            # caller's path in ``self.base_config``, lets YAML's own
            # UTF-8/UTF-16/BOM detection apply (matching Puppet's UTF-8
            # base-config reader instead of the locale encoding), and
            # leaves the file free to be replaced or pickled across.
            configpath = Path(base_config)
            base_path = configpath.parent
            base = YAMLBackend.load_ordered(configpath.read_bytes())
        else:
            base_path = Path(os.getcwd() if base_path is None else base_path)
            base = YAMLBackend.load_ordered(base_config)

    if not base:
        raise ConfigError("Failed to parse base Hiera configuration")

    if base_path is not None:
        base_path = Path(base_path)

    return base, base_path


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
    """Build HieraLevel instances from hierarchy configuration."""
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
            backend_cls = backends[data_hash]
        except KeyError:
            raise ConfigError(
                "Unknown backend {!r}; known: {}".format(
                    data_hash, ", ".join(sorted(backends))
                )
            )
        # Normalize datadir spelling for the backend.
        conf.setdefault("datadir", conf.get("data_dir"))
        backend = backend_cls(conf)
        backend.datadir = _normalize_source(backend.datadir)
        levels.append(HieraLevel.new(conf, backend))
    return levels
