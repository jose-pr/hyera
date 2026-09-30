"""Invocation: per-lookup state for interpolation.

Ports the part of Puppet's ``pops/lookup/invocation.rb`` the interpolation
engine (``_interpolation.py``) uses: the bound scope, the current sub-lookup
callable, and the recursion-detection name stack.
"""

import contextlib
import contextvars

from .exceptions import InterpolationError

#: The call-time ``strict`` default for a data file's non-hash rule
#: (``yaml_data.rb:31`` reads ``Puppet[:strict]`` per call, not at
#: construction, since one backend instance is shared across scopes). Set
#: from ``invocation.scope.strict`` at the single engine entry every public
#: call shares (``_lookup_function.lookup``) and reset in ``finally``;
#: :attr:`~hyera.backends.Backend.strict` falls back to it when the backend
#: has no explicit value.
_STRICT: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "hiera_strict", default="warning"
)

#: Sentinel for "not given" on :meth:`Invocation.derive`'s ``override_values``/
#: ``default_values`` keywords -- distinct from ``None``, which
#: :class:`Invocation` itself already treats as "empty".
_UNSET = object()


class Invocation:
    """Per-lookup state, threaded through one top-level lookup: the bound
    :class:`~hyera.Scope`, the current sub-lookup callable, and the
    recursion-detection name stack.

    Create exactly one per top-level lookup (``Hiera.lookup``/``.format``);
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
        _name_stack=None,
    ):
        self.scope = scope
        self._lookup = lookup
        self.override_values = {} if override_values is None else override_values
        self.default_values = {} if default_values is None else default_values
        self.lenient = lenient
        #: Recursion-detection stack, shared with every ``Invocation``
        #: :meth:`derive`d from this one (``invocation.rb:47-52``): the same
        #: list object, never copied, so a name pushed by one still guards a
        #: derived invocation's own lookups.
        self._name_stack = [] if _name_stack is None else _name_stack

    def lookup(self, key):
        """Resolve ``key`` through the host's sub-lookup callable.

        ``lookup_options`` and a ``"lookup_options."``-prefixed key are
        never visible to interpolation (``lookup_adapter.rb:46-52``); the
        host callable (``core.Hiera._sub_lookup``, via ``_search_and_
        merge``) is the single place that check is made, so it applies
        the same way to every caller, not only interpolation.
        """
        return self._lookup(key, self)

    def derive(
        self, lookup, *, override_values=_UNSET, default_values=_UNSET
    ) -> "Invocation":
        """A new :class:`Invocation` sharing this one's scope, ``lenient``
        and recursion stack, with a different sub-lookup callable.

        ``override_values``/``default_values`` default to this one's own
        (omit either to inherit it); pass an explicit value (``{}`` to gather
        with none at all, as Puppet's own ``lookup_options`` gather does with
        a bare ``Invocation.new(scope)``) to replace it instead.
        """
        return Invocation(
            self.scope,
            lookup,
            override_values=(
                self.override_values if override_values is _UNSET else override_values
            ),
            default_values=(
                self.default_values if default_values is _UNSET else default_values
            ),
            lenient=self.lenient,
            _name_stack=self._name_stack,
        )

    @contextlib.contextmanager
    def check(self, name):
        """Guard one named lookup against recursion (``invocation.rb:91-107``).

        Raises :class:`~hyera.InterpolationError` ("Recursive lookup
        detected in [a, b]", the current stack plus ``name``) if ``name`` is
        already on the stack; otherwise pushes it, yields, and pops it in
        ``finally`` -- even if the guarded block raises.
        """
        if name in self._name_stack:
            raise InterpolationError(
                "Recursive lookup detected in [{}]".format(", ".join(self._name_stack))
            )
        self._name_stack.append(name)
        try:
            yield
        finally:
            self._name_stack.pop()

    def report_text(self, producer) -> None:
        """Puppet's ``Context#explain``/``invocation.rb``'s ``report_text``:
        a no-op until an explain facility subscribes to it. ``producer`` is
        a zero-argument callable a caller (a ``LookupContext.explain``) would
        only ever invoke lazily, so nothing here calls it either."""
