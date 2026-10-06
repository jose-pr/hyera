# Ported from Puppet 8 lib/puppet/pops/lookup/invocation.rb, lookup_adapter.rb,
# hiera_config.rb, context.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Invocation: per-lookup state for interpolation.

Ports Puppet's ``pops/lookup/invocation.rb`` (with the parts of
``lookup_adapter.rb``, ``hiera_config.rb`` and ``context.rb`` it
cites): the bound scope, the current
sub-lookup callable, the recursion-detection name stack, and (``explainer``
set) the recording hooks a lookup's explain tree is built through.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import typing as _ty

from .cache import _ScopeRef, _freeze, _probe
from .._scope.scope import Scope
from ..exceptions import InterpolationError

# : A lookup's host sub-lookup callable: ``(key, invocation) -> value`` or ``_MISSING``
# (in : ``hyera._lookup.navigation``) for a miss; resolves
# ``%{hiera()}``/``%{lookup()}``/``%{alias()}``.
_LookupFn = _ty.Callable[[str, "Invocation"], _ty.Any]

#: The shared no-op context manager every recording hook uses when
#: ``explainer`` is ``None`` -- an ordinary lookup allocates no explain
#: nodes at all.
_NULL_CONTEXT = contextlib.nullcontext()

#: Call-time ``strict`` default for a data file's non-hash rule (``yaml_data.rb:31``
#: reads ``Puppet[:strict]`` per call; one backend serves many scopes). Set from the
#: scope at engine entry, reset in ``finally``; ``Backend.strict`` falls back to it.
_STRICT: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "hiera_strict", default="warning"
)

#: Sentinel for "not given" on :meth:`Invocation.derive`'s ``override_values``/
#: ``default_values`` keywords -- distinct from ``None``, which
#: :class:`Invocation` itself already treats as "empty".
_UNSET = object()


_SERIAL = itertools.count(1)


class _LookupState:
    """What one top-level lookup shares between its invocations: ``serial``
    identifies the lookup; ``pending`` holds the ``lookup_options`` gathers
    currently running (the re-entrancy guard); ``hits`` counts how often a
    nested call was refused because its gather was already running (such a
    call composed its options without that layer)."""

    __slots__ = ("serial", "pending", "hits")

    def __init__(self) -> None:
        self.serial: int = next(_SERIAL)
        self.pending: _ty.Set[_ty.Any] = set()
        self.hits: int = 0


class Invocation:
    """Per-lookup state, threaded through one top-level lookup: the bound
    :class:`~hyera.Scope`, the current sub-lookup callable, and the
    recursion-detection name stack.

    Create exactly one per top-level lookup (``Hiera.lookup``/``.format``);
    never share one across threads or across independent lookups. ``lookup``
    is the host's sub-lookup callable, ``(key, invocation) -> value |
    hyera._lookup.navigation._MISSING``, which resolves ``%{hiera()}``/
    ``%{lookup()}``/``%{alias()}``.
    """

    def __init__(
        self,
        scope: Scope,
        lookup: _LookupFn,
        *,
        override_values: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        default_values: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        lenient: bool = False,
        scope_interpolations: "_ty.Optional[_ty.List[_ty.Any]]" = None,
        explainer: "_ty.Optional[_ty.Any]" = None,
        _name_stack: _ty.Optional[_ty.List[str]] = None,
        _fs_memo: "_ty.Optional[_ty.Dict[_ty.Any, _ty.Any]]" = None,
        _lo_cache: _ty.Optional[_ty.Any] = None,
        _state: "_ty.Optional[_LookupState]" = None,
        global_only: bool = False,
    ) -> None:
        self.scope = scope
        self._lookup = lookup
        self.override_values: _ty.Mapping[str, _ty.Any] = (
            {} if override_values is None else override_values
        )
        self.default_values: _ty.Mapping[str, _ty.Any] = (
            {} if default_values is None else default_values
        )
        self.lenient = lenient
        # : The :class:`~hyera._output.explain.Explainer` this lookup's recording hooks
        # report to, or : ``None`` (no explanation asked: every hook is a no-op). Shared
        # with every derived : ``Invocation`` (``invocation.rb:61-62``).
        self.explainer = explainer
        #: ``Hiera.explain()``'s per-call ``lookup_options`` memo (a fresh
        #: ``_ScopeKeyedCache``), shared with derived invocations; ``None`` for an
        #: ordinary lookup, which uses the instance's cache.
        self._lo_cache = _lo_cache
        #: This top-level lookup's identity and ``lookup_options``
        #: re-entrancy guard, shared with every ``Invocation`` derived from or
        #: built around it, so nothing reaches another thread's lookup.
        self._state = _LookupState() if _state is None else _state
        #: ``global_only`` (``invocation.rb:222-229``): set for a version 3 global layer
        #: with no v5 environment provider and inherited by derived invocations,
        #: confining nested lookups to it (``lookup_adapter.rb:76,266-269,332-339``).
        self.global_only = global_only
        # : Recursion-detection stack, shared (the same list, never copied) with every
        # derived : ``Invocation`` (``invocation.rb:47-52``), so a name pushed by one
        # guards the others.
        self._name_stack = [] if _name_stack is None else _name_stack
        #: ``None`` or a list of ``(_ScopeRef, (undefined, frozen_value))`` pairs shared
        #: with derived invocations; :meth:`remember_scope_lookup` appends (Puppet's
        #: ``ScopeLookupCollectingInvocation``). A cache builder passes a fresh list.
        self.scope_interpolations = scope_interpolations
        #: Per-lookup filesystem probe memo (``path -> _Probe``), shared with derived
        #: invocations and the lookup's other invocations: one filesystem snapshot, each
        #: path probed once. A fresh ``{}`` when not given (``sources()``).
        self._fs_memo = {} if _fs_memo is None else _fs_memo

    def _memo_probe(self, path):
        """The memoized :class:`~hyera._lookup.cache._Probe` for ``path``, probing
        (one real ``os.stat``) only the first time this lookup asks about
        it."""
        probe = self._fs_memo.get(path)
        if probe is None:
            probe = _probe(path)
            self._fs_memo[path] = probe
        return probe

    def lookup(self, key: str) -> _ty.Any:
        """Resolve ``key`` through the host's sub-lookup callable.

        ``lookup_options`` and a ``"lookup_options."``-prefixed key are
        never visible to interpolation (``lookup_adapter.rb:46-52``); the
        host callable (``core.Hiera._sub_lookup``, via ``_search_and_
        merge``) is the single place that check is made, so it applies
        the same way to every caller, not only interpolation.
        """
        return self._lookup(key, self)

    def derive(
        self,
        lookup: _LookupFn,
        *,
        override_values: _ty.Any = _UNSET,
        default_values: _ty.Any = _UNSET,
        global_only: _ty.Any = _UNSET,
    ) -> "Invocation":
        """A new :class:`Invocation` sharing this one's scope, ``lenient``
        and recursion stack, with a different sub-lookup callable.

        ``override_values``/``default_values``/``global_only`` default to
        this one's own (omit any to inherit it, which is how
        ``global_only`` propagates to a nested lookup -- ``invocation.
        rb:57``); pass an explicit value (``{}`` for
        ``override_values``/``default_values``, to gather with none at all,
        as Puppet's own ``lookup_options`` gather does with a bare
        ``Invocation.new(scope)``) to replace it instead.
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
            scope_interpolations=self.scope_interpolations,
            explainer=self.explainer,
            _name_stack=self._name_stack,
            _fs_memo=self._fs_memo,
            _lo_cache=self._lo_cache,
            _state=self._state,
            global_only=(self.global_only if global_only is _UNSET else global_only),
        )

    def remember_scope_lookup(
        self,
        key: str,
        root_key: str,
        segments: _ty.Sequence[_ty.Any],
        value: _ty.Any,
        *,
        undefined: bool,
    ) -> None:
        """Record one scope read (Puppet's ``remember_scope_lookup``,
        ``invocation.rb:119-126``/``hiera_config.rb:11-36``): a no-op unless
        this invocation (or the one it was :meth:`derive`d from) was given a
        ``scope_interpolations`` list to record into.
        """
        if self.scope_interpolations is None:
            return
        self.scope_interpolations.append(
            (
                _ScopeRef(key, root_key, tuple(segments), self.lenient),
                (undefined, _freeze(value)),
            )
        )

    @contextlib.contextmanager
    def with_local_memory_eluding(self, name):
        """Puppet's ``with_local_memory_eluding`` (``hiera_config.rb:28-35``):
        while the guarded block runs, any :meth:`remember_scope_lookup` call
        is recorded as usual; once it exits, every entry recorded *during*
        the block whose root is ``name`` is dropped again -- used around a
        ``mapped_paths`` item's own local scope layer, so the item variable
        itself is never treated as a reference the cache depends on (only
        the collection variable, read before this context, is). A no-op
        when this invocation was not given a ``scope_interpolations`` list.
        """
        lst = self.scope_interpolations
        if lst is None:
            yield
            return
        start = len(lst)
        try:
            yield
        finally:
            added = lst[start:]
            del lst[start:]
            lst.extend(entry for entry in added if entry[0].root != name)

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
        except RecursionError as exc:
            # The innermost guard sees the deepest stack; the entry point
            # that finally catches the error names it.
            if not hasattr(exc, "_hyera_keys"):
                exc._hyera_keys = tuple(self._name_stack)
            raise
        finally:
            self._name_stack.pop()

    def recording(self, kind: str, qualifier: _ty.Any) -> "_ty.ContextManager[None]":
        """Push an explain node of ``kind`` for the guarded block, popping it
        (even on an exception) when the block exits -- the shared
        :data:`_NULL_CONTEXT` while :attr:`explainer` is ``None``, so an
        ordinary lookup allocates no explain nodes at all."""
        if self.explainer is None:
            return _NULL_CONTEXT
        return self._recording(kind, qualifier)

    @contextlib.contextmanager
    def _recording(self, kind: str, qualifier):
        self.explainer.push(kind, qualifier)
        try:
            yield
        finally:
            self.explainer.pop()

    @property
    def only_explain_options(self) -> bool:
        return self.explainer is not None and self.explainer.only_explain_options

    def report_found(self, key: _ty.Any, value: _ty.Any) -> _ty.Any:
        """Record ``value`` as found for ``key`` in ordinary hierarchy
        data, if :attr:`explainer` is set. Returns ``value`` unchanged."""
        if self.explainer is not None:
            self.explainer.accept_found(key, value)
        return value

    def report_found_in_overrides(self, key: _ty.Any, value: _ty.Any) -> _ty.Any:
        """Record ``value`` as found for ``key`` in ``lookup()``'s
        ``override`` argument. Returns ``value`` unchanged."""
        if self.explainer is not None:
            self.explainer.accept_found_in_overrides(key, value)
        return value

    def report_found_in_defaults(self, key: _ty.Any, value: _ty.Any) -> _ty.Any:
        """Record ``value`` as found for ``key`` in ``lookup()``'s
        ``default_values_hash`` argument. Returns ``value`` unchanged."""
        if self.explainer is not None:
            self.explainer.accept_found_in_defaults(key, value)
        return value

    def report_not_found(self, key: _ty.Any) -> None:
        """Record a miss for ``key``, if :attr:`explainer` is set."""
        if self.explainer is not None:
            self.explainer.accept_not_found(key)

    def report_location_not_found(self) -> None:
        """Record that the current hierarchy location does not exist, if
        :attr:`explainer` is set."""
        if self.explainer is not None:
            self.explainer.accept_location_not_found()

    def report_merge_source(self, source: _ty.Any) -> None:
        """Record which ``lookup_options`` source a merge strategy came
        from, if :attr:`explainer` is set."""
        if self.explainer is not None:
            self.explainer.accept_merge_source(source)

    def report_result(self, value: _ty.Any) -> _ty.Any:
        """Record the top-level lookup's final result, if
        :attr:`explainer` is set. Returns ``value`` unchanged."""
        if self.explainer is not None:
            self.explainer.accept_result(value)
        return value

    def report_module_not_found(self, name: str) -> None:
        """Record that module ``name`` does not exist, if
        :attr:`explainer` is set."""
        if self.explainer is not None:
            self.explainer.accept_module_not_found(name)

    def report_module_provider_not_found(self, name: str) -> None:
        """Record that module ``name`` has no ``hiera.yaml`` provider, if
        :attr:`explainer` is set."""
        if self.explainer is not None:
            self.explainer.accept_module_provider_not_found(name)

    def report_text(self, producer: _ty.Callable[[], str]) -> None:
        """Puppet's ``Context#explain``/``invocation.rb``'s ``report_text``
        (``context.rb:186-188``): ``producer`` is a zero-argument callable,
        called only while :attr:`explainer` is set -- a plain backend-level
        ``context.explain(lambda: ...)`` call costs nothing when no one is
        explaining."""
        if self.explainer is not None:
            self.explainer.accept_text(producer())

    def emit_debug_info(self, preamble: str) -> None:
        """Puppet's ``Lookup.lookup``'s own debug emission
        (``pops/lookup.rb:62,66``): a no-op unless :attr:`explainer` is a
        :class:`~hyera._output.explain._DebugExplainer` -- checked by duck type
        (``hasattr``), not ``isinstance``, so this module never imports
        ``_explain`` at all."""
        emit = getattr(self.explainer, "emit_debug_info", None)
        if emit is not None:
            emit(preamble)
