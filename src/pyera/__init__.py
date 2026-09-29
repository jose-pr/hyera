"""pyera: a Python implementation of Puppet Hiera data lookup."""

from .backends import (
    Backend,
    BackendError,
    HOCONBackend,
    JSONBackend,
    SopsYAMLBackend,
    YAMLBackend,
    default_backends,
)
from .exceptions import ConfigError, HieraError, InterpolationError
from .core import (
    Hiera,
    ScopedHiera,
)
from ._hiera_config import HieraLevel
from ._merge_strategy import Merge, make_merge
from ._types import Sensitive
from .util import LookupDict, sym_lookup

__version__ = "0.1.0"

__all__ = [
    "Hiera",
    "ScopedHiera",
    "HieraLevel",
    "Merge",
    "Sensitive",
    "make_merge",
    "default_backends",
    "Backend",
    "YAMLBackend",
    "SopsYAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "LookupDict",
    "sym_lookup",
    "HieraError",
    "ConfigError",
    "BackendError",
    "InterpolationError",
    "__version__",
]
