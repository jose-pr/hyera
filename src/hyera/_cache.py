"""Scope-keyed caching: Puppet's scope-interpolation stability check.

Ports the part of Puppet's ``pops/lookup/hiera_config.rb`` that decides
*when* a cached data provider (here: a resolved hierarchy location list, or
a merged ``lookup_options`` mapping) is still valid for a scope that was
never seen before, by replaying only the handful of scope reads that went
into building it (``ScopeLookupCollectingInvocation``/
``scope_interpolations_stable?``) instead of keying on the whole scope
value.

Original code: no upstream header, no ``NOTICE`` line.
"""

import typing as _ty

from ._navigation import _MISSING, sub_lookup
from .exceptions import HieraLookupError

__all__ = []

#: A recorded reference read as ``_UNSTABLE`` during replay: something about
#: it makes this reference set impossible to judge (a sub-lookup segment
#: walk hit a genuine type-mismatch error), so this ref tuple is skipped
#: rather than treated as a match or a miss -- the caller falls through to
#: rebuilding.
_UNSTABLE = object()

#: At most this many distinct reference sets are kept per cache "kind": the
#: set of scope reads a build makes can itself depend on values (a
#: re-interpolated value, a mapped collection), so more than one set can be
#: valid for the same kind at once.
_MAX_KNOWN = 16


def _freeze(value):
    """A recursive, type-tagged, order-preserving, hashable rendering of a
    Puppet Data value, so ``True``, ``1`` and ``1.0`` never compare or hash
    equal (mirroring Ruby ``eql?``) and dict/list order is preserved (a
    rendered ``%{hash}`` depends on it). Anything not Puppet Data (should
    never happen for a scope/sub-lookup value, but never trusted) freezes to
    a value that never compares equal to another, so its entry never
    spuriously matches and simply never gets reused.
    """
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, int):
        return ("i", value)
    if isinstance(value, float):
        return ("f", value)
    if isinstance(value, str):
        return ("s", value)
    if value is None:
        return ("n",)
    if isinstance(value, dict):
        return ("d", tuple((_freeze(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return ("l", tuple(_freeze(v) for v in value))
    return ("o", object())


class _ScopeRef(_ty.NamedTuple):
    """One scope read made while building a cached entry: the reference as
    written (``key``), its root variable name, its dotted sub-navigation
    segments (a tuple), and whether it was read leniently (a location, vs.
    ``datadir``'s strict read) -- :meth:`_ScopeKeyedCache._replay_and_fetch`
    treats a non-lenient undefined ref as a miss (the rebuild then warns or
    raises as it would have originally) and replays a lenient one's warning
    on a hit.

    A ``NamedTuple``, deliberately: two refs recorded by two different
    builds (for two different scopes) that name the same variable the same
    way must compare and hash equal, or the same logical reference set would
    never converge onto one cache entry across scopes -- the entire point of
    keying on referenced variables instead of the whole scope.
    """

    key: str
    root: str
    segments: tuple
    lenient: bool


def _read_ref(scope, ref):
    """Replay one :class:`_ScopeRef` against ``scope``, side-effect free
    (Puppet's ``scope_interpolations_stable?`` per entry, without the
    warnings a real interpolation would emit).

    Returns ``(undefined, frozen_value)``, or :data:`_UNSTABLE` when a
    sub-lookup segment walk hits a genuine type mismatch (the rebuild will
    raise the real error).
    """
    v = scope.lookup(ref.root)
    if v is _MISSING:  # Scope.UNDEFINED is hyera._navigation._MISSING.
        return (True, _freeze(None))
    if ref.segments and v is not None:
        try:
            v = sub_lookup(ref.key, ref.segments, v)
        except HieraLookupError:
            return _UNSTABLE
        if v is _MISSING:
            v = None
    return (False, _freeze(v))


class _ScopeKeyedCache:
    """A cache whose entries are valid for every scope that reads the same
    values from the handful of variables its own build read, not just the
    exact scope it was built for.

    ``lock`` is shared with every other cache on the same :class:`~hyera.Hiera`
    instance (one lock per instance, not per cache).
    """

    def __init__(self, lock):
        self._lock = lock
        self._entries = {}
        #: kind -> [refs tuple, ...], most recent first, at most
        #: :data:`_MAX_KNOWN`.
        self._known = {}
        #: kind -> (scope, extra, key): an identity fast path, one scope
        #: kept alive per kind.
        self._last = {}

    def get(self, kind, scope, extra=()):
        last = self._last.get(kind)
        if last is not None and last[0] is scope and last[1] == extra:
            hit = self._replay_and_fetch(last[2], scope)
            if hit is not _MISSING:
                return hit
        with self._lock:
            known = list(self._known.get(kind, ()))
        for refs in known:
            sig = []
            stable = True
            for ref in refs:
                s = _read_ref(scope, ref)
                if s is _UNSTABLE:
                    stable = False
                    break
                sig.append(s)
            if not stable:
                continue
            key = (kind, extra, refs, tuple(sig))
            hit = self._replay_and_fetch(key, scope)
            if hit is not _MISSING:
                self._last[kind] = (scope, extra, key)
                return hit
        return _MISSING

    def _replay_and_fetch(self, key, scope):
        _kind, _extra, refs, sig = key
        for ref, (undefined, _frozen) in zip(refs, sig):
            if undefined and not ref.lenient:
                return _MISSING
        with self._lock:
            value = self._entries.get(key, _MISSING)
        if value is _MISSING:
            return _MISSING
        for ref, (undefined, _frozen) in zip(refs, sig):
            if undefined and ref.lenient:
                scope.lookupvar(ref.root, lenient=True)
        return value

    def key_for(self, kind, recorded, extra=()):
        """Build the cache key a fresh build's own ``recorded`` list (an
        iterable of ``(ref, (undefined, frozen))`` pairs, as
        :meth:`~hyera._invocation.Invocation.remember_scope_lookup` appends
        them) should be stored under: deduped by ``ref.key``, first kept.
        """
        seen = set()
        refs = []
        sig = []
        for ref, value in recorded:
            if ref.key in seen:
                continue
            seen.add(ref.key)
            refs.append(ref)
            sig.append(value)
        return (kind, extra, tuple(refs), tuple(sig))

    def put(self, key, value):
        kind, _extra, refs, _sig = key
        with self._lock:
            self._entries[key] = value
            known = self._known.setdefault(kind, [])
            if refs in known:
                known.remove(refs)
            known.insert(0, refs)
            del known[_MAX_KNOWN:]

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._known.clear()
            self._last.clear()

    def __len__(self):
        with self._lock:
            return len(self._entries)
