# Derived from phiera/exceptions.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Exception hierarchy for hiera."""


class HieraError(Exception):
    """Base class for all hiera errors."""


class ConfigError(HieraError):
    """The base hiera configuration is missing or invalid."""


class BackendError(HieraError):
    """A backend failed to load or decode a data file."""


class InterpolationError(HieraError):
    """A ``%{...}`` interpolation or function call could not be resolved."""
