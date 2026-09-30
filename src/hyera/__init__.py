"""hyera: a Python implementation of Puppet Hiera data lookup."""

from .backends import (
    Backend,
    BackendError,
    EyamlBackend,
    HOCONBackend,
    JSONBackend,
    LookupContext,
    SopsBackend,
    YAMLBackend,
    default_backends,
)
from ._output import render as _render  # noqa: F401 (registers s/json/yaml renderers)
from .exceptions import (
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
from ._config.hiera_config import HieraLevel
from ._lookup.merge_strategy import MergeSpec
from ._scope.scope import Scope
from ._types.types import Sensitive

__version__ = "0.0.0a0"

__all__ = [
    "Hiera",
    "ExplainResult",
    "HieraLevel",
    "MergeSpec",
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
    "HieraLookupError",
    "InterpolationError",
    "MergeError",
    "KeyNotFoundError",
    "__version__",
]
