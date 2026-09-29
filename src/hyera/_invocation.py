"""Invocation: per-lookup state for interpolation.

Ports the part of Puppet's ``pops/lookup/invocation.rb`` the interpolation
engine (``_interpolation.py``) uses: the bound scope, the current sub-lookup
callable, and (from ``recursion_and_fanout``) the recursion-detection name
stack.
"""

import contextvars

from ._navigation import _MISSING

#: The call-time ``strict`` default for a data file's non-hash rule
#: (``yaml_data.rb:31`` reads ``Puppet[:strict]`` per call, not at
#: construction, since one backend instance is shared across scopes). Set
#: from ``invocation.scope.strict`` at the top-level lookup entry
#: (``core.Hiera._get``) and reset in ``finally``; :attr:`~hyera.backends.
#: Backend.strict` falls back to it when the backend has no explicit value.
_STRICT: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "hiera_strict", default="warning"
)


class Invocation:
    """Per-lookup state, threaded through one top-level lookup: the bound
    :class:`~hyera.Scope`, the current sub-lookup callable, and the
    recursion-detection name stack.

    Create exactly one per top-level lookup (``Hiera.get``/``.format``);
    never share one across threads or across independent lookups. ``lookup``
    is the host's sub-lookup callable, ``(key, invocation) -> value |
    hyera._navigation._MISSING``, used to resolve ``%{hiera()}``/
    ``%{lookup()}``/``%{alias()}``.
    """

    def __init__(
        self,
        scope,
        lookup,
        *,
        override_values=None,
        default_values=None,
        lenient=False,
    ):
        self.scope = scope
        self._lookup = lookup
        self.override_values = {} if override_values is None else override_values
        self.default_values = {} if default_values is None else default_values
        self.lenient = lenient

    def lookup(self, key):
        """Resolve ``key`` through the host's sub-lookup callable.

        ``key == "lookup_options"`` or a ``"lookup_options."``-prefixed key
        always misses (:data:`~hyera._navigation._MISSING`) without
        reaching the host at all -- ``lookup_options`` is never visible to
        interpolation (``lookup_adapter.rb:46-52``).
        """
        if key == "lookup_options" or key.startswith("lookup_options."):
            return _MISSING
        return self._lookup(key, self)

    def derive(self, lookup) -> "Invocation":
        """A new :class:`Invocation` sharing this one's scope, overrides,
        defaults and ``lenient``, with a different sub-lookup callable."""
        return Invocation(
            self.scope,
            lookup,
            override_values=self.override_values,
            default_values=self.default_values,
            lenient=self.lenient,
        )
