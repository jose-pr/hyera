"""hyera: a Python implementation of Puppet Hiera data lookup."""

from .backends import (
    Backend,
    BackendError,
    HOCONBackend,
    JSONBackend,
    SopsBackend,
    YAMLBackend,
    default_backends,
)
from .exceptions import (
    ConfigError,
    HieraError,
    HieraLookupError,
    InterpolationError,
    KeyNotFoundError,
    MergeError,
)
from .core import (
    Hiera,
    ScopedHiera,
)
from ._facts import facts_from_facter, load_facts
from ._hiera_config import HieraLevel
from ._merge_strategy import Merge, make_merge
from ._scope import Scope
from ._types import Sensitive

__version__ = "0.1.0"

__all__ = [
    "Hiera",
    "ScopedHiera",
    "HieraLevel",
    "Merge",
    "Scope",
    "Sensitive",
    "load_facts",
    "facts_from_facter",
    "make_merge",
    "default_backends",
    "Backend",
    "YAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "SopsBackend",
    "HieraError",
    "ConfigError",
    "BackendError",
    "HieraLookupError",
    "InterpolationError",
    "MergeError",
    "KeyNotFoundError",
    "__version__",
]
