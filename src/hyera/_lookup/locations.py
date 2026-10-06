# Ported from Puppet 8 lib/puppet/pops/lookup/data_hash_function_provider.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Resolved hierarchy locations and parsed data files, shared by an instance
and every view of it: one lock, the location, glob and file caches, and the
intern table for the path strings they hold."""

from __future__ import annotations

import json
import logging
import os
import typing as _ty

from .cache import _LRU, _FileEntry, _ScopeKeyedCache, _probe
from .function_provider import _validate_data_hash
from .navigation import _MISSING
from .._config.dir_glob import glob as _dir_glob
from .._config.location_resolver import resolve_glob_specs, resolve_locations
from ..exceptions import BackendError, HieraError

#: The engine's own logger, shared with ``hyera.core``.
_LOGGER = logging.getLogger("hyera.core")

#: The intern table is pruned to the paths live entries hold once it grows
#: past this many entries (or twice the live count, if larger).
_PATHS_FLOOR = 1024


class _Location(_ty.NamedTuple):
    """One resolved, non-glob hierarchy location: Puppet's
    ``ResolvedLocation`` (``original``, ``location``, ``is_uri``, ``exist``),
    with ``location`` always an interned ``str`` for a plain path (never a
    ``Path``: a consumer that needs one builds it from ``.location``) or the
    normalized ``uri`` string when ``is_uri``. Field names match
    :class:`~hyera._config.location_resolver.ResolvedLocation` exactly, so a
    :class:`~hyera._lookup.provider_classes._FunctionProvider` (built from either
    kind) never has to tell them apart.
    """

    original: str
    location: str
    is_uri: bool
    exist: bool


class _GlobLocation(_ty.NamedTuple):
    """One ``glob``/``globs`` declared string, resolved and rooted but not
    yet walked -- stored in a :class:`_LocationEntry` in place of a
    :class:`_Location`; :meth:`_LocationStore.materialize` expands it into
    :class:`_Location` matches at lookup time (:meth:`_LocationStore.glob_matches`),
    never during the hierarchy build itself."""

    original: str
    root: str
    pattern: str


class _LocationEntry:
    """One cached, resolved hierarchy: ``key`` is this entry's own
    :class:`~hyera._lookup.cache._ScopeKeyedCache` key (reused as the
    ``lookup_options`` cache's ``extra``, so a ``lookup_options`` entry is
    invalidated whenever its locations are); ``levels`` is one resolved
    locations tuple -- or ``None`` for a location-less entry -- per
    hierarchy level, aligned by index with the ``hierarchy`` list it was
    built from, each tuple holding :class:`_Location`/:class:`_GlobLocation`.
    ``materialized`` caches this entry's own lookup-time expansion (globs
    walked, ``exist`` refreshed) when ``revalidate=False``, so it is
    computed at most once per entry for the instance's life; ``None`` until
    then, and always ``None`` (never read) when ``revalidate=True``, which
    recomputes it fresh every lookup. A plain mutable class, not a
    ``NamedTuple``: ``materialized`` is the one field a cached entry updates
    in place after it was already stored.
    """

    __slots__ = ("key", "levels", "materialized")

    def __init__(self, key, levels):
        self.key = key
        self.levels = levels
        self.materialized = None


class _GlobEntry(_ty.NamedTuple):
    """One cached glob listing: ``matches`` is the interned-string tuple of
    matched files (directories already dropped); ``dirs`` is the
    ``((directory, state), ...)`` pairs for every directory the walk
    consulted (:func:`_dir_state`), which decide whether the listing is
    still fresh (:meth:`_LocationStore.glob_matches`, ``revalidate=True``) without
    re-walking unless one of them changed."""

    matches: tuple
    dirs: tuple


def _probe_for(invocation, path):
    """The probe of ``path`` through ``invocation``'s per-lookup memo, or a
    one-off probe when there is no invocation."""
    return invocation._memo_probe(path) if invocation is not None else _probe(path)


def _dir_state(probe) -> _ty.Optional[tuple]:
    """What a glob listing depends on in one directory: its
    ``(st_ino, st_mtime_ns)``, or ``None`` when it is not a directory (an
    absent directory is consulted too -- creating it must invalidate)."""
    return probe.sig[:2] if probe.kind == "dir" else None


class _LocationStore:
    """The caches an instance and its views share for locations and data
    files, behind the one lock every cache of the instance uses.

    ``lock`` is that shared lock; ``revalidate`` mirrors the owning
    instance's setting (``False``: nothing read is ever checked again until
    :meth:`clear`).
    """

    def __init__(self, lock, cache_size, revalidate: bool) -> None:
        self.lock = lock
        self.revalidate = revalidate
        #: Resolved hierarchy locations for one ``(tag, base_path)`` layer,
        #: keyed on the values of the variables their own interpolation
        #: reads, not on the whole scope -- see :meth:`location_entry_for`.
        self._location_cache = _ScopeKeyedCache(lock, cache_size)
        #: Glob listings: ``(root, pattern) -> _GlobEntry``; a listing
        #: depends on directory contents, not on scope. See
        #: :meth:`glob_matches`.
        self._glob_cache = _LRU(lock, cache_size)
        # : Parsed data files: ``(path, backend.strict, options) -> _FileEntry``, see
        # :meth:`load_file`. : Unbounded, as Puppet's per-environment file cache is: its
        # size follows the data tree.
        self._file_cache: dict = {}
        #: Every plain path ever loaded successfully into ``_file_cache``,
        #: under any ``strict``/``options`` variant.
        self._loaded_paths: set = set()
        #: Interned path strings, shared by every location entry and by
        #: ``_file_cache``: ``s -> s`` so equal paths from independent
        #: builds share one string object.
        self._paths: dict = {}
        self._paths_limit = [_PATHS_FLOOR]

    def clear(self) -> None:
        """Drop every cached location, glob listing, parsed file and
        interned path."""
        self._location_cache.clear()
        self._glob_cache.clear()
        with self.lock:
            self._file_cache.clear()
            self._loaded_paths.clear()
            self._paths.clear()
            self._paths_limit[0] = _PATHS_FLOOR

    def intern(self, p) -> str:
        """The canonical interned ``str`` for a path-like ``p``: equal paths
        from independent hierarchy builds (and independent glob matches)
        share one string object, which is what makes a :class:`_Location`
        cheap to hold in every cache entry that resolves to it."""
        s = os.fspath(p)
        paths = self._paths
        interned = paths.setdefault(s, s)
        if len(paths) > self._paths_limit[0]:
            self._prune_paths()
        return interned

    def _prune_paths(self) -> None:
        """Shrink the intern table to the paths a live location entry or
        glob listing holds, so it does not outgrow the bounded caches whose
        entries it was filled for. The next prune waits until the table
        has doubled."""
        live: dict = {}
        with self.lock:
            entries = list(self._location_cache._entries.values())
            listings = list(self._glob_cache._entries.values())
        for entry in entries:
            for locations in entry.levels:
                for loc in locations or ():
                    if isinstance(loc, _Location) and not loc.is_uri:
                        live[loc.location] = loc.location
        for listing in listings:
            for match in listing.matches:
                live[match] = match
        with self.lock:
            self._paths.clear()
            self._paths.update(live)
            self._paths_limit[0] = max(_PATHS_FLOOR, 2 * len(live))

    def load_file(self, path, backend, options, invocation=None):
        """Load ``path`` via ``backend.data_hash(path, options)``, returning
        the parsed, cached data, or :data:`~hyera._lookup.navigation._MISSING` when
        ``revalidate=True`` and ``path`` has vanished since it was last
        cached (a materialized location whose ``exist`` was true earlier in
        this same lookup, per its own memoized probe, but is now false --
        caught here rather than treated as a read error, the same way an
        always-absent location is).

        With ``revalidate=True`` (the default), one probe -- through
        ``invocation``'s memo when given, a fresh one-off otherwise -- either
        confirms a cached parse is still current (its signature unchanged)
        or triggers a re-read, logged at debug level; ``invocation=None``
        (``sources()``) still revalidates, just without sharing the probe
        with anything else. With
        ``revalidate=False``, a cached entry is returned untouched, and a
        first read is cached with no signature at all, so it is never
        reconsidered short of :meth:`~hyera.Hiera.clear_cache`.

        A read failure (``OSError``, e.g. the file vanished between the
        directory walk and here) becomes ``Unable to read (<path>): ...``; a
        parse failure (a :class:`BackendError` without ``.path`` set --
        ``Backend.load`` already sets it) becomes ``Unable to parse
        (<path>): ...``; any other non-:class:`HieraError` exception is
        wrapped the same way, naming its type. An already-pathed
        ``BackendError`` (or any other :class:`HieraError`) propagates
        unchanged. There is no directory check here: a location
        that is a directory is caught once, when it is resolved/materialized
        (:meth:`location_entry_for`/:meth:`materialize`/
        :meth:`require_not_dir`), never reaching this method at all --
        repeating the check here would just be a second probe of the same
        path for the same answer.

        Puppet's own Hash check on the result
        (``data_hash_function_provider.rb:70-76``) runs here too, so every
        backend -- third-party ones included -- gets it.

        Cached per ``(path, backend.strict, options)``, not per bare
        ``path``: a data file's own non-hash rule (``YAMLBackend.
        _as_data_hash``'s ``strict``-sensitive raise-or-warn) must run again
        for a call whose effective ``strict`` differs from a previous one,
        never reuse a result computed under a different strictness;
        ``options`` joins the key too, so a cache hit never skips a file
        function's own options check (``Backend._require_path_only``) --
        ``options`` is Puppet ``Data``, so it always serializes.
        ``self._loaded_paths`` separately tracks which plain paths were ever
        read successfully, for :func:`~hyera._lookup.providers.files_for`'s
        "was this location loaded" check, independent of which
        ``strict``/``options`` variant did the loading. This is the only place
        a location is actually read: a hierarchy build only resolves and
        records locations, so every location -- even one visited many times
        across many lookups -- is parsed here at most once per
        probe-confirmed version.
        """
        options_key = json.dumps(options, sort_keys=True)
        cache_key = (path, backend.strict, options_key)
        probe = None
        with self.lock:
            entry = self._file_cache.get(cache_key)

        if self.revalidate:
            probe = _probe_for(invocation, path)
            if probe.kind == "absent":
                if entry is not None:
                    # A path cached earlier that has since vanished reads as absent, as
                    # Puppet's next compilation would see it, not as an error.
                    with self.lock:
                        self._file_cache.pop(cache_key, None)
                        self._loaded_paths.discard(path)
                    return _MISSING
                # Never cached, and materialization still says this is a location to
                # read (a glob match's `exist` is not a fresh probe: a dangling symlink
                # matches by name): attempt the read and let it fail naturally.
            elif entry is not None and entry.signature == probe.sig:
                return entry.data
        elif entry is not None:
            return entry.data

        try:
            data = backend.data_hash(path, dict(options))
        except BackendError as e:
            if e.path is None:
                raise BackendError(
                    "Unable to parse ({}): {}".format(path, e), path=str(path)
                ) from e
            raise
        except HieraError:
            raise
        except OSError as e:
            raise BackendError(
                "Unable to read ({}): {}".format(path, e.strerror or e), path=str(path)
            ) from e
        except Exception as e:
            raise BackendError(
                "Unable to parse ({}): {}: {}".format(path, type(e).__name__, e),
                path=str(path),
            ) from e

        _validate_data_hash(data, backend.name, path)
        if entry is not None:
            _LOGGER.debug("File at '%s' was changed, reloading", path)
        with self.lock:
            self._file_cache[cache_key] = _FileEntry(probe.sig if probe else None, data)
            self._loaded_paths.add(path)
        return data

    def location_entry_for(
        self, hierarchy, base_path, scope, tag, invocation=None
    ) -> _LocationEntry:
        """The cached :class:`_LocationEntry` for one layer's ``hierarchy``
        -- valid for every scope that reads the same values from the
        variables the hierarchy's own interpolation reads (Puppet's
        ``scope_interpolations_stable?``), not just the exact scope it was
        built for.

        Shared by every view derived from this instance (unlike
        ``self._providers``, see :meth:`Hiera._view <hyera.core.Hiera._view>`):
        ``base_path`` -- the owning layer's own root -- disambiguates a layer's
        hierarchy from any other's the same way
        :func:`~hyera._lookup.providers.provider_for`'s own cache key already
        does, so two providers never collide even under the same ``tag``.

        This only resolves locations, and probes each non-glob, non-uri one
        (through ``invocation``'s filesystem memo when given, else a
        one-off probe) to tell a directory -- which raises
        :class:`~hyera.BackendError` right here, the same way a later lookup
        that finds one would -- from an existing or absent plain location;
        it never reads a file's *content* (:meth:`load_file` is what a
        ``data_hash`` provider calls, lazily, the first time a location's
        data is actually needed). Building an entry is therefore
        ``strict``-independent: two scopes reading the same variables share
        one entry regardless of ``strict``, and a data file's own
        strict-sensitive non-hash rule always sees whichever ``strict`` the
        lookup that actually reads it is running under.

        A ``glob``/``globs`` level's declared strings are only interpolated
        and rooted here (:class:`_GlobLocation`); the actual match listing
        is deferred to :meth:`materialize`, once per lookup, so a file
        added to (or removed from) a glob-matched directory is seen by a
        later lookup even when this entry itself is reused unchanged. A
        hierarchy entry with no location key at all resolves to ``None``
        (:func:`~hyera._config.location_resolver.resolve_locations`), distinct from
        one that resolves to zero candidates.
        """
        kind = ("locations", tag, base_path, id(hierarchy))
        cached = self._location_cache.get(kind, scope)
        if cached is not _MISSING:
            return cached

        fs_memo = invocation._fs_memo if invocation is not None else None
        refs = []
        levels = []
        for level in hierarchy:
            if level.location_key in ("glob", "globs"):
                specs = resolve_glob_specs(level, base_path, scope, refs, fs_memo)
                locations = tuple(
                    _GlobLocation(spec.original, spec.root, spec.pattern)
                    for spec in specs
                )
            else:
                resolved = resolve_locations(level, base_path, scope, refs, fs_memo)
                if resolved is None:
                    locations = None
                else:
                    built = []
                    for loc in resolved:
                        if loc.is_uri:
                            built.append(
                                _Location(loc.original, loc.location, True, True)
                            )
                        else:
                            path = self.intern(loc.location)
                            built.append(
                                _Location(
                                    loc.original,
                                    path,
                                    False,
                                    self.require_not_dir(path, invocation),
                                )
                            )
                    locations = tuple(built)
            levels.append(locations)
        key = self._location_cache.key_for(kind, refs)
        entry = _LocationEntry(key, tuple(levels))
        self._location_cache.put(key, entry)
        return entry

    def require_not_dir(self, path: str, invocation) -> bool:
        """The memo-aware probe every plain (non-glob, non-uri) location
        goes through, both at build time and, with ``revalidate=True``,
        again at every materialization: ``True`` if ``path`` is currently a
        regular file, ``False`` if absent, :class:`~hyera.BackendError`
        ("Is a directory") if it is one -- caught here, once, rather than
        left for :meth:`load_file` to rediscover with a second, redundant
        probe.
        """
        probe = _probe_for(invocation, path)
        if probe.kind == "dir":
            raise BackendError(
                "Unable to read ({}): Is a directory".format(path), path=path
            )
        return probe.kind == "file"

    def materialize(self, entry: _LocationEntry, invocation):
        """This entry's ``levels``, with every :class:`_GlobLocation`
        expanded into its current file matches (:meth:`glob_matches`) and,
        with ``revalidate=True``, every plain location's ``exist`` refreshed
        through a fresh probe -- Puppet re-globs and re-checks on every
        compilation, and a library has no compilation of its own, so one
        top-level lookup is the unit that gets one filesystem snapshot. A
        ``uri`` location is never probed at all (Puppet never fetches or
        stats one; it always "exists").

        With ``revalidate=False`` this is computed only the first time for
        a given entry and cached on it (``entry.materialized``), exactly as
        first seen, until :meth:`~hyera.Hiera.clear_cache`.
        """
        if not self.revalidate and entry.materialized is not None:
            return entry.materialized
        # One lookup sees one snapshot: the memo shares the invocation's
        # probe memo, so every level's provider reuses the first expansion.
        memo = invocation._fs_memo if invocation is not None else None
        memo_key = ("materialized", id(entry))
        if memo is not None:
            held = memo.get(memo_key)
            if held is not None:
                return held[1]

        levels = []
        for locations in entry.levels:
            if locations is None:
                levels.append(None)
                continue
            resolved = []
            for loc in locations:
                if isinstance(loc, _GlobLocation):
                    for match in self.glob_matches(loc.root, loc.pattern, invocation):
                        resolved.append(_Location(loc.original, match, False, True))
                elif loc.is_uri:
                    resolved.append(loc)
                elif self.revalidate:
                    exist = self.require_not_dir(loc.location, invocation)
                    resolved.append(_Location(loc.original, loc.location, False, exist))
                else:
                    resolved.append(loc)
            levels.append(tuple(resolved))
        materialized = tuple(levels)
        if not self.revalidate:
            entry.materialized = materialized
        elif memo is not None:
            memo[memo_key] = (entry, materialized)
        return materialized

    def glob_matches(self, root: str, pattern: str, invocation) -> tuple:
        """The current file matches for one rooted glob pattern, memoized by
        ``(root, pattern)`` in ``self._glob_cache`` (never by scope: a
        listing depends on directory contents alone).

        With ``revalidate=True``, a cached listing is reused only while
        every directory the walk consulted (listed, or tested a child of by
        name, including one that was absent) still probes to the state it
        had when listed (:func:`_dir_state`); otherwise (or on a first
        call) the walker re-lists, recording the directories it consults
        this time via ``on_scandir``. A matched
        directory is dropped the same memo-aware way a plain location's
        existence is checked, so a match already probed while listing is
        never probed again by :meth:`load_file`. With ``revalidate=False``
        a cached listing is reused unconditionally.
        """
        key = (root, pattern)
        cached = self._glob_cache.get(key)
        if cached is not _MISSING:
            if not self.revalidate:
                return cached.matches
            if all(
                _dir_state(_probe_for(invocation, d)) == state
                for d, state in cached.dirs
            ):
                return cached.matches

        dirs = []

        def on_scandir(d):
            dirs.append((d, _dir_state(_probe_for(invocation, d))))

        def probe_isdir(d):
            return _probe_for(invocation, d).kind == "dir"

        raw = _dir_glob(
            root,
            pattern,
            on_scandir if self.revalidate else None,
            probe_isdir if self.revalidate else None,
        )
        matches = []
        for m in raw:
            # Only a directory is dropped (``reject(&:directory?)``, as the eager
            # `_expand_globs` does); a dangling symlink match is kept, and `_load_file`
            # raises for it when something reads it.
            if self.revalidate:
                is_dir = _probe_for(invocation, m).kind == "dir"
            else:
                is_dir = os.path.isdir(m)
            if not is_dir:
                matches.append(self.intern(m))
        matches = tuple(matches)
        self._glob_cache.put(key, _GlobEntry(matches, tuple(dirs)))
        return matches
