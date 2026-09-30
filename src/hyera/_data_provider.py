"""Layer discovery and per-layer config loading: the global, environment and
module data providers behind Puppet's lookup provider stack
(``lookup_adapter.rb``'s ``PROVIDER_STACK``), built on the single-config
engine ``_hiera_config.py`` already provides.

Original code; no phiera/Puppet-source header -- this module implements the
*rules* described in Puppet's ``lookup_adapter.rb``, ``data_provider.rb``,
``module_data_provider.rb`` and ``environment_data_provider.rb``, not a
line-by-line translation of any one of them.
"""

import logging
import os
import re
import typing as _ty

from pathlib_next import Path

from ._hiera_config import (
    _build_hierarchies,
    _fill_v5_defaults,
    _read_base_config,
    _config_version,
    _validate_v5,
)
from ._lookup_adapter import LOOKUP_OPTIONS

_LOGGER = logging.getLogger(__name__)

#: ``node/environment.rb:127-129``.
_ENV_NAME_RE = re.compile(r"[A-Za-z0-9_]+")
#: ``module.rb:52-57``.
_MODULE_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")


class _Provider(_ty.NamedTuple):
    """One loaded, usable version-5 layer config."""

    #: ``"Global"``, ``"Environment"`` or ``"Module"``.
    place: str
    module_name: "_ty.Optional[str]"
    root: Path
    source: object
    hierarchy: list
    default_hierarchy: list
    version: int


class _IgnoredConfig(_ty.NamedTuple):
    """A version-3 (or missing-version) config at an environment or module
    root: parsed enough to know it exists, but not usable as a layer.

    Whether it is silently ignored (with a warning) or raises depends on the
    invocation's ``strict`` at the point it is actually consulted
    (:meth:`~hyera.core.Hiera._usable`), not at load time.
    """

    place: str
    source: object


def split_path_setting(value, name) -> "_ty.Optional[_ty.Tuple[Path, ...]]":
    """Puppet's path-setting shape: ``None``/``""``/empty -> ``None``; a
    ``str`` split on :data:`os.pathsep`; a single :class:`os.PathLike`; or any
    other iterable of ``str``/:class:`os.PathLike`. Every entry is resolved
    to an absolute path against the current working directory.
    """
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p for p in value.split(os.pathsep) if p]
    elif isinstance(value, os.PathLike):
        parts = [value]
    else:
        try:
            parts = list(value)
        except TypeError:
            raise TypeError(
                "{} must be a path, a list of paths, or a string separated "
                "by os.pathsep".format(name)
            ) from None
        for p in parts:
            if not isinstance(p, (str, os.PathLike)):
                raise TypeError(
                    "{} must be a path, a list of paths, or a string "
                    "separated by os.pathsep".format(name)
                )
    if not parts:
        return None
    return tuple(Path(p).absolute() for p in parts)


def find_environment(environmentpath, name) -> "_ty.Optional[Path]":
    """The first ``environmentpath`` entry listing a directory named exactly
    ``name`` (``puppet.rb:213-233``, ``environments.rb:255-263``): a directory
    LISTING is matched, never ``(entry / name).is_dir()``, so a
    case-insensitive filesystem (Windows/macOS) does not find ``MyMod`` for
    ``mymod`` the way Linux Puppet never would.
    """
    if not name or not _ENV_NAME_RE.fullmatch(name):
        return None
    for entry in environmentpath:
        try:
            names = os.listdir(str(entry))
        except OSError:
            continue
        if name in names:
            candidate = entry / name
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    return None


def module_dirs(modulepath) -> "_ty.Dict[str, Path]":
    """Every module directory reachable through ``modulepath``, in order;
    the first occurrence of a name wins (``node/environment.rb:344-376``)."""
    result: "_ty.Dict[str, Path]" = {}
    for entry in modulepath:
        try:
            names = os.listdir(str(entry))
        except OSError:
            continue
        for name in names:
            if name in result or not _MODULE_NAME_RE.fullmatch(name):
                continue
            candidate = entry / name
            try:
                if candidate.is_dir():
                    result[name] = candidate
            except OSError:
                continue
    return result


def module_name_of(root: str) -> "_ty.Optional[str]":
    """The module a root lookup key belongs to: everything before its first
    ``::``, or ``None`` (``lookup_key.rb:13-22``)."""
    i = root.find("::")
    if i <= 0:
        return None
    return root[:i]


def load_layer_provider(place, root, backends, *, module_name=None):
    """Load ``root / "hiera.yaml"`` as an environment or module layer.

    ``None`` when there is no ``hiera.yaml`` there at all. A version-3 (or
    missing-version) config becomes an :class:`_IgnoredConfig`, left for
    :meth:`~hyera.core.Hiera._usable` to warn or raise about, with the
    invocation's own ``strict``. A version-4 config raises immediately
    (version 4 is not supported yet). A version-5 config is validated
    and built exactly like the global config, with ``layer=place.lower()``
    threaded through so a per-layer rule (``hiera3_backend`` global-only,
    ``default_hierarchy`` module-only) applies.
    """
    hiera_yaml = root / "hiera.yaml"
    try:
        is_file = hiera_yaml.is_file()
    except OSError:
        is_file = False
    if not is_file:
        return None
    layer = place.lower()
    source, data = _read_base_config(hiera_yaml, None)
    version = _config_version(data, source, layer=layer)
    if version == 3:
        return _IgnoredConfig(place, source)
    _fill_v5_defaults(data)
    _validate_v5(data, source, layer=layer)
    hierarchy, default_hierarchy = _build_hierarchies(data, backends, source)
    return _Provider(
        place, module_name, root, source, hierarchy, default_hierarchy, version
    )


def prune_module_data(data, module_name, function_name, location) -> dict:
    """Puppet's module-data namespace rule (``module_data_provider.rb:47-
    60``): keep ``lookup_options`` and every key qualified with the module's
    own name; drop and warn about anything else. Never mutates ``data``."""
    prefix = module_name + "::"
    result = {}
    for key, value in data.items():
        if key == LOOKUP_OPTIONS or (isinstance(key, str) and key.startswith(prefix)):
            result[key] = value
        else:
            _LOGGER.warning(
                "Module '%s': Value returned from data_hash function '%s', "
                "when using location '%s', must use keys qualified with the "
                "name of the module; got %s",
                module_name,
                function_name,
                location,
                key,
            )
    return result


class _EnvironmentState:
    """One resolved environment: its root (or ``None``), its own layer
    provider (or ``None``/:class:`_IgnoredConfig`), its modulepath, and the
    per-module providers discovered/loaded from it so far.

    Cached on :class:`~hyera.core.Hiera` per environment name and shared with
    every :meth:`~hyera.core.Hiera.scoped` view.
    """

    __slots__ = (
        "name",
        "root",
        "provider",
        "modulepath",
        "_modules",
        "module_providers",
    )

    def __init__(self, name, root, provider, modulepath):
        self.name = name
        self.root = root
        self.provider = provider
        self.modulepath = modulepath
        self._modules: "_ty.Optional[_ty.Dict[str, Path]]" = None
        self.module_providers: dict = {}

    def modules(self) -> "_ty.Dict[str, Path]":
        if self._modules is None:
            self._modules = module_dirs(self.modulepath)
        return self._modules
