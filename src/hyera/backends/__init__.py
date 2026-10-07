"""Data backends: a self-registering ``Backend`` registry.

Every format or provider is a :class:`Backend` subclass. Registration is by
subclassing (``NAMES``, keyed by namespace) rather than an explicit call;
lookup goes through :meth:`Backend.find`/:meth:`Backend.get`/:meth:`Backend.new`.
"""

from __future__ import annotations

from .._lookup.function_provider import LookupContext
from ..exceptions import BackendError
from ._base import Backend, BackendKind, NamePattern, default_backends
from ._psych import RubySymbol

# The order of these imports is the registration order that
# ``default_backends()`` reports.
from ._yaml import YAMLBackend
from ._json import JSONBackend
from ._hocon import HOCONBackend, has_hocon
from ._sops import DotenvBackend, SopsBackend
from ._eyaml import EyamlBackend

__all__ = [
    "Backend",
    "BackendKind",
    "NamePattern",
    "YAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "SopsBackend",
    "EyamlBackend",
    "DotenvBackend",
    "BackendError",
    "RubySymbol",
    "LookupContext",
    "has_hocon",
    "default_backends",
    "SOPS_TIMEOUT",
]

#: Seconds :class:`SopsBackend` waits for ``sops`` before killing it. Read
#: at each call, so assigning ``hyera.backends.SOPS_TIMEOUT`` takes effect;
#: ``SopsBackend(timeout=...)`` overrides it for one backend.
SOPS_TIMEOUT = 30
