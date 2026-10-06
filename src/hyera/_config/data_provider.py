# Ported from Puppet 8 lib/puppet/pops/lookup/{lookup_adapter,data_provider,
# module_data_provider,environment_data_provider,lookup_key}.rb, lib/puppet.rb,
# node/environment.rb, module.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Layer discovery and per-layer config loading: the global, environment and
module data providers behind Puppet's lookup provider stack
(``lookup_adapter.rb``'s ``PROVIDER_STACK``), built on the single-config
engine ``hiera_config.py`` already provides.

Follows Puppet's ``lookup_adapter.rb``, ``data_provider.rb``,
``module_data_provider.rb`` and ``environment_data_provider.rb`` rule by
rule, citing the line of each rule it mirrors.
"""

from __future__ import annotations

import logging
import os
import re
import typing as _ty

from pathlib_next import Path

from .hiera_config import (
    _fill_v5_defaults,
    _read_base_config,
    _warn_deprecated,
)
from .config_source import _config_error, _config_version
from .config_v3 import _fill_v3_defaults, _read_v3, _validate_v3
from .config_v4 import _read_v4
from .config_v5 import _validate_v5
from .level_builder import _build_hierarchies
from .._lookup.navigation import LOOKUP_OPTIONS
from ..exceptions import ConfigError, HieraError, _one_line

_LOGGER = logging.getLogger(__name__)
#: The engine's own logger, shared with ``hyera.core``.
_CORE_LOGGER = logging.getLogger("hyera.core")

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
    (:func:`usable_provider`), not at load time.
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


def load_layer_provider(place, root, backends, scope=None, *, module_name=None):
    """Load ``root / "hiera.yaml"`` as an environment or module layer.

    ``None`` when there is no ``hiera.yaml`` there at all; one that is a
    directory raises :class:`~hyera.ConfigError` like the global layer's.
    A version-3 (or
    missing-version) config is still read in full against Puppet's own v3
    schema -- a schema error surfaces here regardless of layer, exactly as
    Puppet's own ``HieraConfigV3#validate_config`` always runs before any
    layer-appropriateness check does -- then becomes an
    :class:`_IgnoredConfig` once it validates, left for
    :func:`usable_provider` to warn or raise about, with the
    invocation's own ``strict``. A version-4 config is read in full
    (Puppet accepts it only in the environment/module layers). A version-5
    config is validated and built exactly like the global config, with
    ``layer=place.lower()`` threaded through so a per-layer rule
    (``hiera3_backend`` global-only, ``default_hierarchy`` module-only)
    applies.
    """
    hiera_yaml = root / "hiera.yaml"
    try:
        present = hiera_yaml.exists()
    except OSError:
        present = False
    if not present:
        return None
    layer = place.lower()
    source, data = _read_base_config(hiera_yaml, None)
    version = _config_version(data, source)
    if version == 3:
        _warn_deprecated(source, 3, scope)
        _fill_v3_defaults(data)
        _validate_v3(data, source)
        return _IgnoredConfig(place, source)
    if version == 4:
        hierarchy, default_hierarchy = _read_v4(data, source, scope, backends)
        return _Provider(
            place, module_name, root, source, hierarchy, default_hierarchy, 4
        )
    _fill_v5_defaults(data)
    _validate_v5(data, source, layer=layer)
    hierarchy, default_hierarchy = _build_hierarchies(
        data, backends, source, scope=scope
    )
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


def environment_for(hiera, name):
    """The cached :class:`~hyera._config.data_provider._EnvironmentState` for
    environment ``name`` (``puppet.rb:213-233``): discovered on first
    use, then reused by every later lookup and by every
    :meth:`~hyera.Hiera.scoped` view (``hiera._environments`` is shared, since
    :meth:`Hiera._view <hyera.core.Hiera._view>` copies ``__dict__`` without
    deep-copying it).

    With no ``environmentpath`` configured, every name resolves with no
    environment root and no error (a documented difference from Puppet,
    which always has one). With one configured, a name other than
    ``"production"`` that is not found raises :class:`~hyera.ConfigError`
    with Puppet's own text; a missing ``"production"`` directory is not
    an error (Puppet's static default environment).
    """
    state = hiera._environments.get(name)
    if state is not None:
        return state

    root = None
    if hiera._environmentpath:
        root = find_environment(hiera._environmentpath, name)
        if root is None and name != "production":
            raise ConfigError(
                "Could not find a directory environment named '{}' "
                "anywhere in the path: {}. Does the directory exist?".format(
                    name,
                    os.pathsep.join(str(p) for p in hiera._environmentpath),
                )
            )

    provider = (
        load_layer_provider("Environment", root, hiera._backends, hiera.scope)
        if root is not None
        else None
    )
    if hiera._modulepath_override is not None:
        modulepath = hiera._modulepath_override
    elif root is not None:
        modulepath = (root / "modules",) + hiera._basemodulepath
    else:
        modulepath = hiera._basemodulepath

    state = _EnvironmentState(name, root, provider, modulepath)
    hiera._environments[name] = state
    return state


def module_provider_for(hiera, state, module_name):
    """The cached layer provider for ``module_name`` in environment
    ``state`` (``module.rb:303-312``): ``None`` when no module of that
    name is on the modulepath, or it has no ``hiera.yaml``."""
    cache = state.module_providers
    if module_name in cache:
        return cache[module_name]
    result = None
    module_dir = state.modules().get(module_name)
    if module_dir is not None:
        result = load_layer_provider(
            "Module",
            module_dir,
            hiera._backends,
            hiera.scope,
            module_name=module_name,
        )
    cache[module_name] = result
    return result


def usable_provider(hiera, provider, invocation):
    """A layer provider ready to be walked, or ``None``.

    ``None``/a real :class:`~hyera._config.data_provider._Provider` pass
    through unchanged. An :class:`~hyera._config.data_provider._IgnoredConfig`
    (a version-3, or missing-version, config outside the global layer)
    is Puppet's own per-use decision (``environment_data_provider.
    rb:15-26``/``module_data_provider.rb:64-75``): under
    ``strict="error"`` it raises; otherwise it warns once per config
    path and the layer contributes nothing.
    """
    if provider is None or isinstance(provider, _Provider):
        return provider
    if provider.place == "Environment":
        noun, warn_text = "an environment", "the environment root"
    else:
        noun, warn_text = "a module", "module root"
    if invocation.scope.strict == "error":
        raise _config_error(
            provider.source,
            "hiera.yaml version 3 cannot be used in {}".format(noun),
        )
    path = provider.source.path
    if path not in hiera._v3_warned_paths:
        hiera._v3_warned_paths.add(path)
        _CORE_LOGGER.warning("hiera.yaml version 3 found at %s was ignored", warn_text)
    return None


def global_only_for(hiera, scope) -> bool:
    """Whether a lookup into the global layer's own data must stay
    confined to it (``lookup_adapter.rb:266-269``): the global layer is
    version 3, and there is no *version 5* environment provider for
    ``scope.environment`` -- an absent environment, an ignored version
    3 one, and a version 4 one all count as none (only a real
    :class:`~hyera._config.data_provider._Provider` with ``version == 5``
    disqualifies global-only)."""
    if hiera._global.version != 3:
        return False
    provider = environment_for(hiera, scope.environment).provider
    return not (isinstance(provider, _Provider) and provider.version == 5)


def load_global_layer(base_config, base_path, backends, scope, codedir):
    """Load and validate the base configuration, returning ``(backends,
    base, hierarchy, default_hierarchy, provider)``: the backend allow-list
    as a list, the parsed config, its two hierarchies, and the global layer
    wrapped for the provider-aware stack walk.

    Raises :class:`ConfigError` for a missing, unreadable or invalid
    ``hiera.yaml``.
    """
    backends = list(backends)
    # Captured before reading the config: a relative version 3 datadir follows the cwd
    # at construction (``location_resolver.rb:56-66``), never a later lookup's cwd.
    cwd = Path(os.getcwd())

    source, base = _read_base_config(base_config, base_path)
    version = _config_version(base, source)

    if not backends:
        raise ConfigError("No backends could be loaded")

    if version == 3:
        # Global-layer version 3 (or versionless) config: validated in full against the
        # v3 schema. Outside the global layer it is ignored or raised about by
        # :func:`usable_provider`.
        hierarchy, default_hierarchy = _read_v3(
            base, source, scope, backends, codedir, cwd
        )
    elif version == 4:
        # Puppet validates a version 4 config's schema before checking that the layer
        # allows version 4, so a schema-invalid file at the global layer raises its
        # schema error.
        _read_v4(base, source, scope, backends)
        raise ConfigError(
            "hiera.yaml version 4 cannot be used in the global layer",
            path=source.path,
        )
    else:
        _fill_v5_defaults(base)
        _validate_v5(base, source)
        try:
            hierarchy, default_hierarchy = _build_hierarchies(
                base, backends, source, scope=scope
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

    provider = _Provider(
        "Global",
        None,
        source.root,
        source,
        hierarchy,
        default_hierarchy,
        version,
    )
    return backends, base, hierarchy, default_hierarchy, provider
