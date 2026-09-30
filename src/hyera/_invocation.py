"""Invocation: per-lookup state for interpolation.

Ports Puppet's ``pops/lookup/invocation.rb``: the bound scope, the current
sub-lookup callable, the recursion-detection name stack, and (``explainer``
set) the recording hooks a lookup's explain tree is built through.
"""

import contextlib
import contextvars

from ._cache import _ScopeRef, _freeze, _probe
from .exceptions import InterpolationError

#: The shared no-op context manager every recording hook uses when
#: ``explainer`` is ``None`` -- an ordinary lookup allocates no explain
#: nodes at all.
_NULL_CONTEXT = contextlib.nullcontext()

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
        scope_interpolations=None,
        explainer=None,
        _name_stack=None,
        _fs_memo=None,
        _lo_cache=None,
        global_only=False,
    ):
        self.scope = scope
        self._lookup = lookup
        self.override_values = {} if override_values is None else override_values
        self.default_values = {} if default_values is None else default_values
        self.lenient = lenient
        #: The :class:`~hyera._explain.Explainer` this lookup's recording
        #: hooks report to, or ``None`` (the overwhelming common case: no
        #: explanation was asked for, so every hook below is a no-op).
        #: Shared, unchanged, with every ``Invocation`` :meth:`derive`d from
        #: this one (``invocation.rb:61-62``).
        self.explainer = explainer
        #: ``Hiera.explain()``'s own per-call ``lookup_options`` memo (a
        #: fresh ``_ScopeKeyedCache``), or ``None`` (every ordinary lookup:
        #: ``core.Hiera._retrieve_lookup_options`` and friends then use the
        #: instance's real, persistent cache instead). Shared, unchanged,
        #: with every ``Invocation`` :meth:`derive`d from this one, so a
        #: nested sub-lookup made *during* one ``explain()`` call reuses the
        #: same never-persisted memo the top-level search already built,
        #: exactly like Puppet's own per-compilation ``LookupAdapter``
        #: cache would -- discarded once the call returns, never reaching
        #: the instance's own cache.
        self._lo_cache = _lo_cache
        #: Puppet's ``global_only`` (``invocation.rb:222-229``): set only on
        #: the invocation used to resolve a version 3 global layer's own
        #: data (``core.Hiera._lookup_layers``), when no environment
        #: provider of version 5 exists for the current scope. A nested
        #: ``%{lookup()}``/``%{hiera()}``/``%{alias()}`` reached while
        #: interpolating that data inherits it via :meth:`derive`, which is
        #: what confines it to the global layer and skips a module's
        #: ``default_hierarchy`` (``lookup_adapter.rb:76,266-269,332-339``).
        self.global_only = global_only
        #: Recursion-detection stack, shared with every ``Invocation``
        #: :meth:`derive`d from this one (``invocation.rb:47-52``): the same
        #: list object, never copied, so a name pushed by one still guards a
        #: derived invocation's own lookups.
        self._name_stack = [] if _name_stack is None else _name_stack
        #: ``None`` (the default -- most lookups never build a cache entry)
        #: or a list of ``(_ScopeRef, (undefined, frozen_value))`` pairs,
        #: shared with every ``Invocation`` :meth:`derive`d from this one,
        #: appended to by :meth:`remember_scope_lookup` (Puppet's
        #: ``ScopeLookupCollectingInvocation``). A caller building a cache
        #: entry (``core.Hiera._location_entry_for``/``_lookup_options_map``)
        #: passes its own fresh list here and reads it back afterwards.
        self.scope_interpolations = scope_interpolations
        #: Per-lookup filesystem probe memo (``path -> _Probe``), shared with
        #: every ``Invocation`` :meth:`derive`d from this one and with every
        #: other ``Invocation`` the same top-level lookup builds (the
        #: ``lookup_options`` gather, a hierarchy build's own interpolation
        #: invocations): one top-level lookup sees one filesystem snapshot
        #: and probes each path at most once. A fresh ``{}`` when not given
        #: (a bare ``Invocation()`` with no sharing intent -- ``sources()``).
        self._fs_memo = {} if _fs_memo is None else _fs_memo

    def _memo_probe(self, path):
        """The memoized :class:`~hyera._cache._Probe` for ``path``, probing
        (one real ``os.stat``) only the first time this lookup asks about
        it."""
        probe = self._fs_memo.get(path)
        if probe is None:
            probe = _probe(path)
            self._fs_memo[path] = probe
        return probe

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
        self,
        lookup,
        *,
        override_values=_UNSET,
        default_values=_UNSET,
        global_only=_UNSET,
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
            global_only=(self.global_only if global_only is _UNSET else global_only),
        )

    def remember_scope_lookup(self, key, root_key, segments, value, *, undefined):
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
        finally:
            self._name_stack.pop()

    def recording(self, kind: str, qualifier):
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

    @contextlib.contextmanager
    def without_explain(self):
        """Suspend explaining for the guarded block (``invocation.rb:151-
        163``): locations interpolate through this, so resolving a hierarchy
        path itself is never itself recorded (``hiera_config.rb:257``)."""
        saved, self.explainer = self.explainer, None
        try:
            yield
        finally:
            self.explainer = saved

    @property
    def only_explain_options(self) -> bool:
        return self.explainer is not None and self.explainer.only_explain_options

    def report_found(self, key, value):
        if self.explainer is not None:
            self.explainer.accept_found(key, value)
        return value

    def report_found_in_overrides(self, key, value):
        if self.explainer is not None:
            self.explainer.accept_found_in_overrides(key, value)
        return value

    def report_found_in_defaults(self, key, value):
        if self.explainer is not None:
            self.explainer.accept_found_in_defaults(key, value)
        return value

    def report_not_found(self, key) -> None:
        if self.explainer is not None:
            self.explainer.accept_not_found(key)

    def report_location_not_found(self) -> None:
        if self.explainer is not None:
            self.explainer.accept_location_not_found()

    def report_merge_source(self, source) -> None:
        if self.explainer is not None:
            self.explainer.accept_merge_source(source)

    def report_result(self, value):
        if self.explainer is not None:
            self.explainer.accept_result(value)
        return value

    def report_module_not_found(self, name) -> None:
        if self.explainer is not None:
            self.explainer.accept_module_not_found(name)

    def report_module_provider_not_found(self, name) -> None:
        if self.explainer is not None:
            self.explainer.accept_module_provider_not_found(name)

    def report_text(self, producer) -> None:
        """Puppet's ``Context#explain``/``invocation.rb``'s ``report_text``
        (``context.rb:186-188``): ``producer`` is a zero-argument callable,
        called only while :attr:`explainer` is set -- a plain backend-level
        ``context.explain(lambda: ...)`` call costs nothing when no one is
        explaining."""
        if self.explainer is not None:
            self.explainer.accept_text(producer())

    def emit_debug_info(self, preamble) -> None:
        """Puppet's ``Lookup.lookup``'s own debug emission
        (``pops/lookup.rb:62,66``): a no-op unless :attr:`explainer` is a
        :class:`~hyera._explain._DebugExplainer` -- checked by duck type
        (``hasattr``), not ``isinstance``, so this module never imports
        ``_explain`` at all."""
        emit = getattr(self.explainer, "emit_debug_info", None)
        if emit is not None:
            emit(preamble)
