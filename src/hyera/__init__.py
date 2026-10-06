"""hyera: a Python implementation of Puppet Hiera data lookup."""

from __future__ import annotations

from .backends import (
    Backend,
    BackendError,
    BackendKind,
    EyamlBackend,
    HOCONBackend,
    JSONBackend,
    LookupContext,
    SopsBackend,
    YAMLBackend,
    default_backends,
)
from ._output import render as _render  # noqa: F401 (registers s/json/yaml renderers)
from ._output.render import RenderAs
from .exceptions import (
    BackendTimeoutError,
    ConfigError,
    HieraError,
    HieraLookupError,
    InterpolationError,
    KeyNotFoundError,
    MergeError,
)
from .core import Hiera
from ._output.explain import ExplainResult
from ._scope.facts import facts_from_facter, load_facts
from ._config.hiera_config import FunctionKind, HieraLevel
from ._lookup.merge_strategy import Merge, MergeSpec
from ._scope.scope import Scope, Strict
from ._types.types import Sensitive

__version__ = "0.0.0"

__all__ = [
    "Hiera",
    "ExplainResult",
    "HieraLevel",
    "Merge",
    "MergeSpec",
    "Strict",
    "FunctionKind",
    "BackendKind",
    "RenderAs",
    "Scope",
    "Sensitive",
    "load_facts",
    "facts_from_facter",
    "default_backends",
    "Backend",
    "YAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "SopsBackend",
    "EyamlBackend",
    "LookupContext",
    "HieraError",
    "ConfigError",
    "BackendError",
    "BackendTimeoutError",
    "HieraLookupError",
    "InterpolationError",
    "MergeError",
    "KeyNotFoundError",
    "__version__",
]
