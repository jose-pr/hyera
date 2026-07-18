"""hiera: a Python implementation of Puppet Hiera data lookup."""

from .backends import (
    Backend,
    BackendError,
    JSONBackend,
    SopsYAMLBackend,
    YAMLBackend,
)
from .exceptions import ConfigError, HieraError, InterpolationError
from .phiera import Hiera, HieraLevel, Merge, ScopedHiera
from .util import LookupDict, sym_lookup

__version__ = "0.1.0"

__all__ = [
    "Hiera",
    "ScopedHiera",
    "HieraLevel",
    "Merge",
    "Backend",
    "YAMLBackend",
    "SopsYAMLBackend",
    "JSONBackend",
    "LookupDict",
    "sym_lookup",
    "HieraError",
    "ConfigError",
    "BackendError",
    "InterpolationError",
    "__version__",
]
