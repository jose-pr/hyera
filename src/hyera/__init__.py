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
from . import _render  # noqa: F401  (side effect: registers s/json/yaml renderers)
from .exceptions import (
    ConfigError,
    HieraError,
    HieraLookupError,
    InterpolationError,
    KeyNotFoundError,
    MergeError,
)
from .core import Hiera
from ._explain import ExplainResult
from ._facts import facts_from_facter, load_facts
from ._hiera_config import HieraLevel
from ._scope import Scope
from ._types import Sensitive

__version__ = "0.0.0a0"

__all__ = [
    "Hiera",
    "ExplainResult",
    "HieraLevel",
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
