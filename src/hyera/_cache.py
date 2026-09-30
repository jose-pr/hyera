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

import collections
import os
import stat as _stat
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
    instance (one lock per instance, not per cache). ``maxsize`` bounds the
    number of entries: least-recently-used dropped once a ``put`` would
    exceed it; ``None`` never evicts; ``0`` makes every ``put`` a no-op, so
    every ``get`` misses and every lookup rebuilds. Only dict/``_known``/
    ``_last`` mutation happens under the lock -- a rebuild (the caller's own
    work between a miss and its ``put``) never holds it.
    """

    def __init__(self, lock, maxsize=256):
        self._lock = lock
        self.maxsize = maxsize
        self._entries = collections.OrderedDict()
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
            if key not in self._entries:
                return _MISSING
            value = self._entries[key]
            self._entries.move_to_end(key)
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
        if self.maxsize == 0:
            return
        kind, _extra, refs, _sig = key
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            if self.maxsize is not None:
                while len(self._entries) > self.maxsize:
                    self._entries.popitem(last=False)
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


class _LRU:
    """A plain least-recently-used cache, ``maxsize``-bounded the same way
    as :class:`_ScopeKeyedCache` (``None`` never evicts, ``0`` disables),
    for a cache with no scope-stability question to answer -- a glob
    listing is memoized by ``(root, pattern)`` alone, never by scope.
    """

    def __init__(self, lock, maxsize):
        self._lock = lock
        self.maxsize = maxsize
        self._entries = collections.OrderedDict()

    def get(self, key, default=_MISSING):
        with self._lock:
            if key not in self._entries:
                return default
            self._entries.move_to_end(key)
            return self._entries[key]

    def put(self, key, value):
        if self.maxsize == 0:
            return
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            if self.maxsize is not None:
                while len(self._entries) > self.maxsize:
                    self._entries.popitem(last=False)

    def clear(self):
        with self._lock:
            self._entries.clear()

    def __len__(self):
        with self._lock:
            return len(self._entries)


class _Probe(_ty.NamedTuple):
    """One ``os.stat`` result, classified: ``kind`` is ``"file"``, ``"dir"``
    or ``"absent"``; ``sig`` is :func:`_signature` for ``"file"``/``"dir"``,
    ``None`` for ``"absent"``."""

    kind: str
    sig: "_ty.Optional[tuple]"


def _signature(st) -> tuple:
    """Puppet's ``cached_file_data`` validity triple (``context.rb:22``):
    ``(st_ino, st_mtime_ns, st_size)``, always from a real ``os.stat`` --
    never ``DirEntry.stat()``, whose ``st_ino`` is ``0`` on Windows."""
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _probe(path) -> _Probe:
    """One ``os.stat(path)``, classified as ``("absent", None)`` for
    anything ``os.path.exists`` would also call missing: a vanished path
    (``FileNotFoundError``, ``NotADirectoryError``), or one ``os.stat``
    cannot even ask about (``OSError`` for a malformed name -- Windows
    raises this, not ``FileNotFoundError``, for a scope-interpolated
    candidate holding a character reserved in a Windows path, e.g. a
    literal ``"``; ``ValueError`` for an embedded NUL byte). Matching
    ``os.path.exists``'s own leniency here matters: a mapped_paths/glob
    candidate is built from arbitrary interpolated scope data, and a
    genuinely unrepresentable candidate must read as "does not exist", not
    blow up the lookup that happens to generate it.
    """
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return _Probe("absent", None)
    if _stat.S_ISDIR(st.st_mode):
        return _Probe("dir", _signature(st))
    return _Probe("file", _signature(st))


class _FileEntry(_ty.NamedTuple):
    """One cached, parsed data file: ``signature`` is :func:`_signature` of
    the ``os.stat`` taken just before the read (``revalidate=True``), or
    ``None`` (``revalidate=False``: never re-checked once read); ``data`` is
    the parsed result."""

    signature: "_ty.Optional[tuple]"
    data: object
