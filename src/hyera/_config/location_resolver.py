# Ported from Puppet 8 lib/puppet/pops/lookup/location_resolver.rb, the
# location half of hiera_config.rb (https://github.com/puppetlabs/puppet),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Ruby uri/lib/uri/rfc3986_parser.rb
# (https://github.com/ruby/uri), BSD-2-Clause. Modified by jose-pr.
# See NOTICE.
"""Location resolution: expanding hierarchy levels into candidate source paths.

Resolves a :class:`~hyera._config.hiera_config.HieraLevel`'s ``path``/``paths``/
``glob``/``globs``/``mapped_paths`` declarations into concrete filesystem
candidates, using the same ``%{...}`` interpolation engine as data values
(:mod:`hyera._lookup.interpolation`) instead of a private ``str.format`` grammar --
matching Puppet's own ``location_resolver.rb`` and the location half of
``hiera_config.rb``.
"""

import functools
import logging
import os
import re
import stat
import typing as _ty

from pathlib_next import Path

from .._lookup.interpolation import (
    _ruby_inspect_str,
    _scope_lookup,
    _to_puppet_str,
    interpolate,
)
from .._lookup.invocation import Invocation
from ..exceptions import ConfigError

_LOGGER = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"


class ResolvedLocation(_ty.NamedTuple):
    """One candidate source: the declared (uninterpolated) template, the
    resolved location, whether it is a ``uri``/``uris`` location (a plain
    string, never a filesystem path) and whether it exists.

    ``exist=False`` candidates are kept (never silently omitted) so a future
    ``explain`` can show them; callers that only want real files filter on
    ``.exist``. A ``uri`` location is always ``exist=True`` (Puppet never
    fetches or stats it -- a provider decides what it means). ``location`` is
    a plain ``str``: the joined path exactly as written (a trailing ``/`` or
    an inner ``//`` kept), or the normalized URI.
    """

    original: str
    location: str
    is_uri: bool
    exist: bool


def _no_lookup(key, invocation):
    """The ``lookup`` callable for a location's :class:`Invocation`.

    Unreachable in practice: locations resolve with ``allow_methods=False``,
    which rejects every method call (``%{hiera()}``/``%{lookup()}``/
    ``%{alias()}``) -- the only way a sub-lookup would ever be attempted --
    before it could reach this callable.
    """
    raise RuntimeError("hierarchy locations never perform a sub-lookup")


def _win_slash(s: str) -> str:
    """On Windows, a host-path-shaped string (``datadir``, a ``path``/
    ``paths``/mapped-template entry) may use ``\\`` as its separator; convert
    it to ``/`` before interpolation and joining. Left alone on every other
    OS, and never applied to a glob pattern, where ``\\`` is Ruby's escape
    character."""
    return s.replace("\\", "/") if _WINDOWS else s


def _native(path: str) -> str:
    """``path`` with the host's separator, every other character untouched
    (a trailing ``/`` or an inner ``//`` stays)."""
    return path.replace("/", os.sep) if _WINDOWS else path


#: A Windows drive-letter anchor (``C:/...``) and a UNC share anchor
#: (``//server/share``). Both are anchors only on Windows; anywhere else
#: ``c:/a.yaml`` is an ordinary relative path and ``//x`` an ordinary rooted one.
_DRIVE_RE = re.compile(r"[A-Za-z]:/")
_UNC_RE = re.compile(r"//[^/]+/[^/]+(?=/|$)")


def _anchor_of(s: str) -> str:
    """The leading Windows anchor of ``s`` (``C:`` or ``//server/share``), or
    ``""`` when there is none, always off Windows."""
    if not _WINDOWS:
        return ""
    unc = _UNC_RE.match(s)
    if unc:
        return unc.group()
    return s[:2] if _DRIVE_RE.match(s) else ""


def _is_rooted(s: str) -> bool:
    return s.startswith("/") or bool(_anchor_of(s))


def _split_anchor(s: str):
    """Split a ``/``-separated string into its leading anchor (``/``,
    ``C:/``, ``//server/share/`` or ``""``) and its non-empty components."""
    anchor = _anchor_of(s)
    if anchor:
        rest, anchor = s[len(anchor) :], anchor + "/"
    elif s.startswith("/"):
        rest, anchor = s, "/"
    else:
        rest = s
    return anchor, [p for p in rest.split("/") if p != ""]


def _basename(path: str) -> str:
    """Ruby ``File.basename``: the last component, ignoring trailing ``/``;
    ``/`` for a path of slashes only, ``""`` for the empty path."""
    if not path:
        return ""
    stripped = path.rstrip("/")
    return stripped[stripped.rfind("/") + 1 :] if stripped else "/"


def _chop_basename(path: str):
    """``pathname.rb``'s ``chop_basename``: ``(prefix, basename)`` with the
    prefix keeping its trailing separators, or ``None`` when ``path`` has no
    component left (empty, or only slashes)."""
    base = _basename(path)
    if base in ("", "/"):
        return None
    return path[: path.rindex(base)], base


def _plus(path1: str, path2: str) -> str:
    """``pathname.rb``'s ``plus``: ``path2`` joined onto ``path1`` lexically."""
    prefix2 = path2
    indexes, names = [], []
    while True:
        chopped = _chop_basename(prefix2)
        if chopped is None:
            break
        prefix2, name = chopped
        indexes.append(len(prefix2))
        names.append(name)
    if prefix2 != "":
        return path2
    indexes.reverse()
    names.reverse()
    first, count = 0, len(names)

    prefix1 = path1
    while True:
        while first < count and names[first] == ".":
            first += 1
        chopped = _chop_basename(prefix1)
        if chopped is None:
            break
        prefix1, name1 = chopped
        if name1 == ".":
            continue
        if name1 == ".." or first >= count or names[first] != "..":
            prefix1 += name1
            break
        first += 1

    has_base = _chop_basename(prefix1) is not None
    if not has_base and _basename(prefix1) == "/":
        has_base = True
        while first < count and names[first] == "..":
            first += 1
    if first < count:
        suffix = path2[indexes[first] :]
        if has_base and not prefix1.endswith("/"):
            return prefix1 + "/" + suffix
        return prefix1 + suffix
    return prefix1 if has_base else "."


def _pathname_plus(base: str, rel: str) -> str:
    """Ruby ``Pathname#+``: join ``rel`` onto ``base`` lexically, never
    touching the filesystem. ``..`` removes a preceding real component of
    ``base``, is dropped once a rooted ``base`` is exhausted and kept for a
    relative one; a rooted ``rel`` is returned unchanged; everything after
    the leading ``.``/``..`` handling in ``rel`` is appended verbatim, so an
    inner ``..``, ``//`` or trailing ``/`` survives. A Windows drive or UNC
    anchor on ``base`` is set aside and put back in front of the result.
    """
    if _is_rooted(rel):
        return rel
    anchor = _anchor_of(base)
    if anchor:
        return anchor + _plus(base[len(anchor) :] or "/", rel)
    return _plus(base, rel)


def _is_link(entry: "os.DirEntry") -> bool:
    """Whether ``**`` must never recurse into ``entry``: a POSIX symlink, or
    a Windows reparse point tagged as a symlink or junction (a mount point).
    ``entry.is_dir(follow_symlinks=False)`` alone is not enough on Windows:
    a junction reports as a plain directory even without following it, so a
    dedicated reparse-tag check is the only way to tell the two apart
    (``DirEntry.is_junction()`` exists only from Python 3.12, and this
    package's floor is 3.9). ``st_reparse_tag`` and the ``stat.
    IO_REPARSE_TAG_*`` constants that classify it are all Windows-only --
    referencing them anywhere POSIX might import this module (even inside a
    function body that runs only when ``os.name == "nt"``, since a module-
    level ``frozenset`` would still evaluate them at import time) would
    raise ``AttributeError`` there, so the reparse-tag check stays inside
    this ``os.name`` branch, evaluated lazily, every call.
    """
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        tag = entry.stat(follow_symlinks=False).st_reparse_tag
    except OSError:
        return False
    return tag in (stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT)


def _entry_is_dir(entry: "os.DirEntry") -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _parse_braces(pattern: str) -> list:
    """``pattern`` as a node list: literal ``str`` chunks and brace groups,
    each group a tuple of alternatives (themselves node lists). Follows Ruby's
    ``ruby_brace_expand``: ``\\`` skips the next character, a ``}`` with no open
    ``{`` is literal, and an unmatched ``{`` leaves everything from it onward
    literal, nested groups included.
    """
    # Each frame is [start index, alternatives so far, current nodes, chars].
    frames = [[-1, [], [], []]]

    def flush(frame):
        if frame[3]:
            frame[2].append("".join(frame[3]))
            frame[3] = []

    n = len(pattern)
    i = 0
    while i < n:
        c = pattern[i]
        frame = frames[-1]
        if c == "\\":
            frame[3].append(pattern[i : i + 2])
            i += 2
            continue
        if c == "{":
            flush(frame)
            frames.append([i, [], [], []])
        elif c == "}" and len(frames) > 1:
            flush(frame)
            frame[1].append(frame[2])
            frames.pop()
            parent = frames[-1]
            flush(parent)
            parent[2].append(tuple(frame[1]))
        elif c == "," and len(frames) > 1:
            flush(frame)
            frame[1].append(frame[2])
            frame[2] = []
        else:
            frame[3].append(c)
        i += 1

    if len(frames) > 1:
        root = frames[0]
        flush(root)
        root[2].append(pattern[frames[1][0] :])
        return root[2]
    flush(frames[0])
    return frames[0][2]


def _link(nodes: list, tail):
    """``nodes`` as a linked list ``(node, next)`` ending in ``tail``."""
    for node in reversed(nodes):
        tail = (node, tail)
    return tail


def _expand_braces(pattern: str) -> "_ty.List[str]":
    """Ruby ``ruby_brace_expand``: the concatenation of ``pattern`` with
    each alternative of its first ``{...}`` group substituted in, in
    written order, recursing on every remaining group (so nested braces and
    a later sibling group both expand). A pattern with no unescaped,
    balanced ``{...}`` group -- including an unmatched ``{`` -- expands to
    itself, escapes and all: :func:`_has_magic`/:func:`_segment_matcher`
    are what later interpret ``\\``. Iterative, so nesting depth is bounded
    by memory, not the interpreter's recursion limit.
    """
    if "{" not in pattern:
        return [pattern]
    results = []
    work = [("", _link(_parse_braces(pattern), None))]
    while work:
        prefix, rest = work.pop()
        parts = [prefix]
        while rest is not None:
            node, rest = rest
            if isinstance(node, str):
                parts.append(node)
                continue
            prefix = "".join(parts)
            for alternative in reversed(node):
                work.append((prefix, _link(alternative, rest)))
            break
        else:
            results.append("".join(parts))
    return results


def _has_magic(seg: str) -> bool:
    """Ruby ``has_magic``: an unescaped ``*``, ``?`` or ``[`` -- an unclosed
    ``[`` still counts (:func:`_segment_matcher` then never matches it)."""
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c in "*?[":
            return True
        i += 1
    return False


def _unescape(seg: str) -> str:
    """A literal segment's real filename: drop one ``\\`` before each
    escaped character, and a trailing lone ``\\``."""
    out = []
    i, n = 0, len(seg)
    while i < n:
        c = seg[i]
        if c == "\\":
            if i + 1 < n:
                out.append(seg[i + 1])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _bracket(pat: str, p: int, name: str, s: int):
    """Ruby ``fnmatch``'s bracket class: ``pat[p:]`` follows a ``[``; return
    the index after the closing ``]`` when ``name[s]`` is in the class, else
    ``None``. An unclosed class, or one whose ``]`` comes first, never
    matches. A range matches its two endpoints and every character between
    them, so a reversed range ``[z-a]`` matches only ``z`` and ``a``.
    """
    n = len(pat)
    if p >= n:
        return None
    negate = False
    if pat[p] in "!^":
        negate = True
        p += 1
    c = name[s]
    ok = False
    while p < n and pat[p] != "]":
        t1 = p + 1 if pat[p] == "\\" else p
        if t1 >= n:
            return None
        p = t1 + 1
        if p < n and pat[p] == "-" and (p + 1 >= n or pat[p + 1] != "]"):
            t2 = p + 1
            if t2 < n and pat[t2] == "\\":
                t2 += 1
            if t2 >= n:
                return None
            p = t2 + 1
            if not ok:
                lo, hi = pat[t1], pat[t2]
                ok = c == lo or c == hi or lo <= c <= hi
        else:
            if p >= n:
                return None
            if not ok:
                ok = c == pat[t1]
    if p >= n:
        return None
    return None if ok == negate else p + 1


def _fnmatch(pat: str, name: str) -> bool:
    """Ruby ``File.fnmatch`` of one path segment, without ``FNM_DOTMATCH``:
    ``*``, ``?``, ``[...]`` and ``\\`` escapes, and a leading ``.`` in ``name``
    needs a literal ``.`` at the start of ``pat``. A two-pointer match that
    remembers only the latest ``*``, so the cost is bounded by
    ``len(pat) * len(name)`` however many stars ``pat`` has.
    """
    plen, slen = len(pat), len(name)
    q = 1 if pat.startswith("\\") else 0
    if slen and name[0] == "." and pat[q : q + 1] != ".":
        return False
    p = s = 0
    star_p = star_s = -1
    while True:
        c = pat[p] if p < plen else ""
        if c == "*":
            while p < plen and pat[p] == "*":
                p += 1
            q = p + 1 if p < plen and pat[p] == "\\" else p
            if q >= plen:
                return True
            if s >= slen:
                return False
            star_p, star_s = p, s
            continue
        if c == "?":
            if s >= slen:
                return False
            p += 1
            s += 1
            continue
        if c == "[":
            if s >= slen:
                return False
            t = _bracket(pat, p + 1, name, s)
            if t is not None:
                p = t
                s += 1
                continue
        else:
            if c == "\\":
                p += 1
            if s >= slen:
                return p >= plen
            if p < plen and pat[p] == name[s]:
                p += 1
                s += 1
                continue
        if star_p < 0:
            return False
        p = star_p
        star_s += 1
        s = star_s


def _literal_chunks_matcher(seg: str):
    """The matcher for a ``seg`` with no ``?``, ``[`` or ``\\``: the text
    between its stars is found with ``str`` methods, left to right, which is
    exactly what :func:`_fnmatch` computes, at C speed."""
    first, *middle, last = seg.split("*")
    middle = [m for m in middle if m]
    dot_ok = seg.startswith(".")
    floor = len(first) + len(last)

    def match(name: str) -> bool:
        if not dot_ok and name.startswith("."):
            return False
        if len(name) < floor or not name.startswith(first):
            return False
        end = len(name) - len(last)
        if not name.endswith(last):
            return False
        pos = len(first)
        for chunk in middle:
            k = name.find(chunk, pos, end)
            if k < 0:
                return False
            pos = k + len(chunk)
        return True

    return match


@functools.lru_cache(maxsize=256)
def _segment_matcher(seg: str):
    """A ``name -> bool`` matcher for one path segment pattern ``seg``: Ruby's
    ``File.fnmatch`` (see :func:`_fnmatch`), including its dotfile rule."""
    if "*" in seg and not any(c in seg for c in "?[\\"):
        return _literal_chunks_matcher(seg)
    return functools.partial(_fnmatch, seg)


def _prepare_segments(pattern: str):
    """``pattern`` (already brace-expanded, no ``/`` from a brace
    substitution understood as *not* a separator -- Ruby brace expansion
    happens first, so any ``/`` an alternative introduces is a real
    separator) split into ``(kind, value)`` pairs: ``("recursive", None)``
    for a whole ``**`` segment that is not last (consecutive ones
    collapse), ``("literal", name)`` for a non-magic segment with its
    escapes removed, or ``("magic", matcher)`` for one compiled by
    :func:`_segment_matcher`. A trailing ``**`` becomes plain ``*`` first,
    matching Ruby.
    """
    raw = [s for s in pattern.split("/") if s != ""]
    n_raw = len(raw)
    segments = []
    for idx, seg in enumerate(raw):
        if seg == "**" and idx != n_raw - 1:
            if segments and segments[-1][0] == "recursive":
                continue
            segments.append(("recursive", None))
            continue
        if seg == "**":
            seg = "*"
        if _has_magic(seg):
            segments.append(("magic", _segment_matcher(seg)))
        else:
            segments.append(("literal", _unescape(seg)))
    return segments


class _DotEntry:
    """The current directory as a listing entry: Ruby offers ``.`` to a
    wildcard segment that starts with a literal ``.``, ``readdir`` never lists
    it."""

    name = "."


def _segment_matches(kind, value, name: str) -> bool:
    if kind == "literal":
        # A literal is an existence check through the OS, so it follows the
        # filesystem's case rule; only wildcards are always case-sensitive.
        return name == value or (
            _WINDOWS and os.path.normcase(name) == os.path.normcase(value)
        )
    if kind == "magic":
        return value(name)
    return False


def _glob_one(
    root: str, pattern: str, on_scandir=None, probe_isdir=None
) -> "_ty.List[str]":
    """One already-brace-expanded pattern matched under the literal
    directory ``root``: a depth-first walk over sets of segment indices
    (:func:`_prepare_segments`), so ``**``'s "zero or more directories" and
    an ordinary wildcard share one filesystem pass. A pattern ending in
    ``/`` matches only directories, which Puppet rejects, so it is always
    ``[]``. The walk keeps its own stack, so tree depth is bounded by memory.

    ``on_scandir``, when given, is called with every directory path whose
    entries the walk consults, whether it lists them or tests a literal
    child by name, and whether or not the directory exists -- used to record
    what a listing depended on, for a later freshness check.

    ``probe_isdir``, when given, replaces the plain ``os.path.isdir(child)``
    check an intermediate literal segment uses to decide whether to descend
    -- a memo-aware caller (``core.Hiera._glob_matches``) passes one so this
    check costs a real probe at most once per lookup, same as
    ``on_scandir``. Never used for a *final* literal segment's own
    ``os.path.lexists`` check: that one deliberately does not follow
    symlinks (a dangling symlink still counts as a match, exactly like the
    wildcard path), which a ``os.stat``-based memo would get wrong.
    """
    if pattern.endswith("/"):
        return []
    segments = _prepare_segments(pattern)
    n = len(segments)
    if n == 0:
        return []
    results = []
    # (path, segment indices reached, whether a wildcard led here)
    stack = [(root, (0,), False)]
    while stack:
        path, active, by_wildcard = stack.pop()
        if n in active:
            results.append(path)
        pending = [i for i in active if i < n]
        if not pending:
            continue
        children = []
        if on_scandir is not None:
            on_scandir(path)
        if all(segments[i][0] == "literal" for i in pending):
            by_literal = {}
            for i in pending:
                by_literal.setdefault(segments[i][1], []).append(i)
            for lit, idxs in by_literal.items():
                child = os.path.join(path, lit)
                nxt = set()
                final_idxs = [i for i in idxs if i + 1 == n]
                mid_idxs = [i for i in idxs if i + 1 != n]
                if final_idxs and os.path.lexists(child):
                    nxt.update(i + 1 for i in final_idxs)
                if mid_idxs and (
                    probe_isdir(child)
                    if probe_isdir is not None
                    else os.path.isdir(child)
                ):
                    nxt.update(i + 1 for i in mid_idxs)
                if nxt:
                    children.append((child, tuple(sorted(nxt)), by_wildcard))
            stack.extend(reversed(children))
            continue
        try:
            entries = list(os.scandir(path))
        except OSError as e:
            _LOGGER.debug("Skipping %s: %s", path, e)
            continue
        # Ruby offers "." only below literal segments and never beside a "**".
        if (
            not by_wildcard
            and not any(segments[i][0] == "recursive" for i in pending)
            and any(segments[i][0] == "magic" and segments[i][1](".") for i in pending)
        ):
            entries.append(_DotEntry())
        entries.sort(key=lambda e: os.fsencode(e.name))
        for entry in entries:
            nxt = set()
            for i in pending:
                kind, value = segments[i]
                if kind == "recursive":
                    if (
                        not entry.name.startswith(".")
                        and _entry_is_dir(entry)
                        and not _is_link(entry)
                    ):
                        nxt.add(i)
                    nxt_kind, nxt_value = segments[i + 1]
                    if _segment_matches(nxt_kind, nxt_value, entry.name):
                        nxt.add(i + 2)
                elif _segment_matches(kind, value, entry.name):
                    nxt.add(i + 1)
            if nxt:
                children.append(
                    (os.path.join(path, entry.name), tuple(sorted(nxt)), True)
                )
        stack.extend(reversed(children))
    return results


def glob(root: str, pattern: str, on_scandir=None, probe_isdir=None) -> "_ty.List[str]":
    """Ruby ``Dir.glob`` for ``pattern`` under the literal directory
    ``root``: the concatenation of :func:`_glob_one` over every brace
    alternative of ``pattern``, in written order (duplicates kept, as Ruby
    keeps them).
    Results are ``os.path.join``ed absolute strings; directories are
    included here (a caller wanting files only, as every Hiera glob level
    does, filters them out itself). ``on_scandir``/``probe_isdir``: see
    :func:`_glob_one`."""
    results = []
    for p in _expand_braces(pattern):
        results.extend(_glob_one(root, p, on_scandir, probe_isdir))
    return results


def _glob_root_and_pattern(config_root: str, datadir: str, g: str):
    """The literal root directory and the (still glob-magic) pattern to
    hand :func:`glob` for one interpolated glob string ``g`` against a
    level's (also interpolated) ``datadir``. A rooted ``g`` wins outright;
    otherwise a rooted ``datadir`` anchors the walk and ``g`` joins onto
    its remainder; otherwise both join onto ``config_root``, so glob
    metacharacters in ``datadir`` are live (Puppet's own behavior) while
    the config root itself -- an absolute host path -- never is.
    """
    dd = _win_slash(datadir)
    if _is_rooted(g):
        anchor, parts = _split_anchor(g)
        return anchor, "/".join(parts)
    if _is_rooted(dd):
        anchor, parts = _split_anchor(dd)
        return anchor, _pathname_plus("/" + "/".join(parts), g).lstrip("/")
    return config_root, _pathname_plus(dd, g)


def _interpolate_path(text, invocation):
    """``text`` interpolated (methods disallowed) as a host path: on Windows
    a ``\\`` separator, written or interpolated, reads as ``/``."""
    return _win_slash(interpolate(_win_slash(text), invocation, allow_methods=False))


def _resolve_paths(datadir, declared, invocation, extension=None):
    """``path``/``paths`` (``location_resolver.rb:56-66``): each entry
    interpolates (methods disallowed), gets ``extension`` appended unless it
    already ends with it, and joins onto ``datadir``.

    Existence is checked through ``invocation``'s filesystem memo
    (:meth:`~hyera._lookup.invocation.Invocation._memo_probe`), not a bare
    ``os.path.exists``: when ``invocation`` shares a memo with the rest of
    the current top-level lookup (``core.Hiera._location_entry_for``), this
    is the one real probe of ``loc`` that lookup ever makes, and a caller
    checking the same path again (to tell a directory from a plain miss)
    reads the cached result instead of probing twice.
    """
    results = []
    for d in declared:
        p = _interpolate_path(d, invocation)
        if extension and not p.endswith(extension):
            p = p + extension
        loc = _pathname_plus(datadir, p)
        exists = invocation._memo_probe(loc).kind != "absent"
        results.append(ResolvedLocation(d, _native(loc), False, exists))
    return results


class _GlobSpec(_ty.NamedTuple):
    """One ``glob``/``globs`` declared string, interpolated and rooted, but
    not yet walked -- the interpolation half of ``_expand_globs``, split out
    so a caller (``core.Hiera._location_entry_for``) can defer the actual
    ``Dir.glob`` match listing to per-lookup materialization instead of
    paying for it on every hierarchy build."""

    original: str
    root: str
    pattern: str


def _glob_specs(config_root, datadir, declared, invocation) -> "_ty.List[_GlobSpec]":
    """Interpolate and root every declared ``glob``/``globs`` string
    (``location_resolver.rb:43-47``'s pattern half), without walking the
    filesystem."""
    base = _pathname_plus(config_root, datadir)
    specs = []
    for raw in declared:
        # Never _win_slash a glob string: '\' is Ruby's escape character
        # here, not a Windows path separator.
        interp = interpolate(raw, invocation, allow_methods=False)
        root, pattern = _glob_root_and_pattern(config_root, datadir, interp)
        original = _pathname_plus(base, interp)
        specs.append(_GlobSpec(original, root, pattern))
    return specs


def resolve_glob_specs(level, base_path, scope, refs=None, fs_memo=None):
    """The rooted, interpolated :class:`_GlobSpec` list for one ``glob``/
    ``globs`` hierarchy level -- the location half of :func:`resolve_locations`
    for this one location kind, kept separate because its own caller
    resolves the actual matches lazily. See :func:`resolve_locations` for
    ``refs``/``fs_memo``."""
    strict_inv = Invocation(
        scope, _no_lookup, scope_interpolations=refs, _fs_memo=fs_memo
    )
    lenient_inv = Invocation(
        scope, _no_lookup, lenient=True, scope_interpolations=refs, _fs_memo=fs_memo
    )
    config_root = Path(base_path).as_posix()
    datadir = _interpolate_path(level.datadir, strict_inv)
    return _glob_specs(config_root, datadir, level.locations, lenient_inv)


def _expand_globs(config_root, datadir, declared, invocation):
    """``glob``/``globs`` (``location_resolver.rb:43-47``): each pattern
    interpolates (methods disallowed) and matches through hyera's own Ruby
    ``Dir.glob`` port (:func:`glob`), rooted by :func:`_glob_root_and_pattern`
    -- live ``datadir`` metacharacters, a rooted pattern or ``datadir``
    anchoring the walk instead. Any match that is a directory is dropped
    (``reject(&:directory?)``); a missing or unreadable directory simply
    contributes no matches.

    Eager, unlike :func:`resolve_glob_specs`: used by
    :meth:`~hyera._config.hiera_config.HieraLevel.paths`, which has no lazy
    materialization step to defer the walk to.
    """
    results = []
    for original, root, pattern in _glob_specs(
        config_root, datadir, declared, invocation
    ):
        for match in glob(root, pattern):
            if os.path.isdir(match):
                continue
            results.append(ResolvedLocation(original, match, False, True))
    return results


# --- Ruby URI() acceptance and normalization (uri/rfc3986_parser.rb) -------
#
# A hand port of Ruby's RFC 3986 grammar (`RFC3986_Parser::HOST`, `USERINFO`,
# `SCHEME`, `SEG`, `SEG_NC`, `FRAGMENT`, `RFC3986_URI`,
# `RFC3986_relative_ref`), translated to Python `re` with only the changes
# the language forces: `\h` -> `[0-9A-Fa-f]`; every `\g<name>` subroutine
# call (a re-invocation of the named pattern, which `re` has no equivalent
# for) inlined as that pattern's own text; possessive `*+`/`++` -> greedy
# `*`/`+` (neither 3.9 nor 3.14 has possessive quantifiers, and a backtracking
# greedy quantifier accepts exactly the same language here); group names
# without a `-` (a Python identifier); `\A`/`\z` -> `fullmatch`. Only the
# groups this module actually reads (`scheme`, `port`, `query`) are named;
# everything else is a plain, non-capturing `(?:...)`.
_HEXDIG = "[0-9A-Fa-f]"
_PCT = "%" + _HEXDIG + _HEXDIG
_USERINFO = "(?:" + _PCT + "|[!$&-.0-9:;=A-Z_a-z~])*"
_SCHEME_CHARS = "[A-Za-z][+\\-.0-9A-Za-z]*"
_SEG = "(?:" + _PCT + "|[!$&-.0-9:;=@A-Z_a-z~/])"
_SEG_NC = "(?:" + _PCT + "|[!$&-.0-9;=@A-Z_a-z~])"
_FRAGMENT = "(?:" + _PCT + "|[!$&-.0-9:;=@A-Z_a-z~/?])*"

_DEC_OCTET = "(?:[1-9]\\d|1\\d{2}|2[0-4]\\d|25[0-5]|\\d)"
_IPV4ADDRESS = _DEC_OCTET + "\\." + _DEC_OCTET + "\\." + _DEC_OCTET + "\\." + _DEC_OCTET
_LS32 = "(?:" + _HEXDIG + "{1,4}:" + _HEXDIG + "{1,4}|" + _IPV4ADDRESS + ")"
_H4 = _HEXDIG + "{1,4}"
_IPV6ADDRESS = (
    "(?:"
    + ("(?:" + _H4 + ":){6}" + _LS32)
    + ("|::(?:" + _H4 + ":){5}" + _LS32)
    + ("|" + _H4 + "?::(?:" + _H4 + ":){4}" + _LS32)
    + ("|(?:(?:" + _H4 + ":)?" + _H4 + ")?::(?:" + _H4 + ":){3}" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,2}" + _H4 + ")?::(?:" + _H4 + ":){2}" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,3}" + _H4 + ")?::" + _H4 + ":" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,4}" + _H4 + ")?::" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,5}" + _H4 + ")?::" + _H4)
    + ("|(?:(?:" + _H4 + ":){0,6}" + _H4 + ")?::")
    + ")"
)
_IPVFUTURE = "v" + _HEXDIG + "+\\.[!$&-.0-9:;=A-Z_a-z~]+"
_IP_LITERAL = "\\[(?:" + _IPV6ADDRESS + "|" + _IPVFUTURE + ")\\]"
_REG_NAME = "(?:" + _PCT + "|[!$&-.0-9;=A-Z_a-z~])*"
_HOST = "(?:" + _IP_LITERAL + "|" + _IPV4ADDRESS + "|" + _REG_NAME + ")"

_AUTHORITY = "(?:" + _USERINFO + "@)?" + _HOST + "(?::(?P<port>\\d*))?"
_PATH_ABEMPTY = "(?:/" + _SEG + "*)?"
_QUERY = "(?P<query>[^#]*)"

_RFC3986_URI = re.compile(
    "(?P<scheme>"
    + _SCHEME_CHARS
    + "):"
    + "(?:"
    + "//"
    + _AUTHORITY
    + _PATH_ABEMPTY
    + "|/(?:(?!/)"
    + _SEG
    + "+)?"
    + "|(?!/)"
    + _SEG
    + "+"
    + "|"
    + ")"
    + "(?:\\?"
    + _QUERY
    + ")?"
    + "(?:#(?:"
    + _FRAGMENT
    + "))?"
)

_RFC3986_RELATIVE_REF = re.compile(
    "(?:"
    + "//"
    + _AUTHORITY
    + _PATH_ABEMPTY
    + "|/"
    + _SEG
    + "*"
    + "|"
    + _SEG_NC
    + "+(?:/"
    + _SEG
    + "*)?"
    + "|"
    + ")"
    + "(?:\\?"
    + _QUERY
    + ")?"
    + "(?:#(?:"
    + _FRAGMENT
    + "))?"
)

#: Default port dropped by ``URI#to_s`` when it exactly matches the
#: scheme's own default (measured against Ruby 4.0.7; only the schemes
#: Puppet's own oracle exercises are covered).
_DEFAULT_PORTS = {
    "http": "80",
    "ws": "80",
    "https": "443",
    "wss": "443",
    "ftp": "21",
    "ldap": "389",
}


def _ruby_uri(text: str) -> str:
    """Validate ``text`` against Ruby's ``URI()`` grammar and reproduce its
    ``#to_s`` normalization (measured, not derivable from the grammar
    alone): lowercase the scheme; drop an empty port or one equal to the
    scheme's own default; percent-encode a literal space in the query as
    ``%20``. Everything else -- host case, path, fragment -- passes through
    verbatim. Raises :class:`~hyera.ConfigError` with Ruby's own message
    when neither the absolute nor the relative grammar matches.
    """
    match = _RFC3986_URI.fullmatch(text) or _RFC3986_RELATIVE_REF.fullmatch(text)
    if match is None:
        raise ConfigError("bad URI (is not URI?): " + _ruby_inspect_str(text))

    edits = []  # (start, end, replacement), applied right-to-left.
    scheme = match.groupdict().get("scheme")
    if scheme is not None:
        lowered = scheme.lower()
        if lowered != scheme:
            edits.append((match.start("scheme"), match.end("scheme"), lowered))
    port = match.groupdict().get("port")
    if port is not None:
        drop = port == "" or (
            scheme is not None and port == _DEFAULT_PORTS.get(scheme.lower())
        )
        if drop:
            # Drop the port digits and the ':' immediately before them.
            edits.append((match.start("port") - 1, match.end("port"), ""))
    query = match.groupdict().get("query")
    if query is not None and " " in query:
        edits.append(
            (match.start("query"), match.end("query"), query.replace(" ", "%20"))
        )

    result = text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _expand_uris(declared, invocation):
    """``uri``/``uris`` (``location_resolver.rb:71-76``'s ``expand_uris``):
    each declared string interpolates like any other location (lenient, no
    method calls), is validated/normalized by :func:`_ruby_uri`, and always
    "exists" -- Puppet never fetches or stats a ``uri`` location; a provider
    decides what it means."""
    results = []
    for declared_uri in declared:
        interp = interpolate(declared_uri, invocation, allow_methods=False)
        normalized = _ruby_uri(interp)
        results.append(ResolvedLocation(declared_uri, normalized, True, True))
    return results


def _mapped_collection_items(collection, collection_var, level_name):
    """The items a mapped_paths collection variable expands to:
    ``None``/``""``/``[]``/``{}`` -> no items; a ``str`` -> itself, alone; a
    ``list`` -> itself; a ``dict`` -> its ``[key, value]`` pairs, in order.
    Anything else (``bool``, then ``int``, then ``float`` -- checked in that
    order since ``bool`` is a Python ``int`` subclass) raises
    :class:`~hyera.ConfigError`, mirroring the Ruby ``NoMethodError`` Puppet
    itself raises calling ``.empty?`` on a non-collection value.
    """
    if collection is None or collection in ("", [], {}):
        return []
    if isinstance(collection, str):
        return [collection]
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        return [[k, v] for k, v in collection.items()]
    if isinstance(collection, bool):
        type_name, rendered = "Boolean", _to_puppet_str(collection)
    elif isinstance(collection, int):
        type_name, rendered = "Integer", str(collection)
    elif isinstance(collection, float):
        type_name, rendered = "Float", _to_puppet_str(collection)
    else:
        type_name, rendered = type(collection).__name__, str(collection)
    raise ConfigError(
        "mapped_paths collection '{}' in hierarchy '{}' must be a String, an "
        "Array or a Hash, got {} {}".format(
            collection_var, level_name, type_name, rendered
        )
    )


def _expand_mapped_paths(datadir, level, invocation):
    """``mapped_paths`` (``location_resolver.rb:78-98``): the collection is
    a scope reference (dotted, ``::``-qualified, lenient); each item binds
    as one local variable layer (so an unqualified template reference reads
    the item, and ``%{::x}`` still reaches the top scope) while the template
    interpolates (methods disallowed).

    The collection read (via ``invocation``, outside the loop) is recorded
    as usual -- a cache keyed on referenced variables must rebuild when the
    collection itself changes. Each item's own interpolation runs inside
    ``invocation.with_local_memory_eluding(item_var)``, so any reference to
    the per-item local variable it binds is dropped again once the item is
    done: the item is a derived, per-iteration value, never itself a
    variable the cache should key on (``location_resolver.rb:90``).
    """
    collection_var, item_var, template = level.locations
    collection = _scope_lookup(collection_var, invocation, "mapped_path[0]")
    items = _mapped_collection_items(collection, collection_var, level.name)

    results = []
    for item in items:
        with invocation.with_local_memory_eluding(item_var):
            child_scope = invocation.scope.with_local_scope({item_var: item})
            child_inv = Invocation(
                child_scope,
                _no_lookup,
                lenient=True,
                scope_interpolations=invocation.scope_interpolations,
                _fs_memo=invocation._fs_memo,
            )
            p = _interpolate_path(template, child_inv)
            loc = _pathname_plus(datadir, p)
            exists = child_inv._memo_probe(loc).kind != "absent"
            results.append(ResolvedLocation(template, _native(loc), False, exists))
    return results


def resolve_locations(level, base_path, scope, refs=None, fs_memo=None):
    """The candidate :class:`ResolvedLocation` list for one hierarchy level
    in a bound :class:`~hyera.Scope` (``hiera_config.rb:664-687``).

    ``level.datadir`` interpolates first, under ``strict`` (no shield) --
    Puppet does this outside ``Puppet.override(avoid_hiera_interpolation_
    errors: true)``. Every other location interpolates leniently (an
    undefined variable becomes ``''`` plus the strict-mode warning, never an
    error) through the same engine as data values, with method-call syntax
    (``%{lookup(...)}`` etc.) rejected outright -- Puppet's own restriction
    on this context.

    Never called for a ``glob``/``globs`` level from the main lookup
    pipeline (see :func:`resolve_glob_specs`); still handles that kind
    itself (eagerly) for :meth:`~hyera._config.hiera_config.HieraLevel.paths`,
    which has no lazy materialization step of its own.

    ``refs``, when given, is a list every scope read made while resolving
    this level appends itself to (:meth:`~hyera._lookup.invocation.Invocation.
    remember_scope_lookup`), shared across every level of one hierarchy
    build by the caller (``core.Hiera._location_entry_for``) so the whole
    hierarchy's build is keyed on one combined reference set, matching
    Puppet's own single ``scope_interpolations_stable?`` check per rebuild.
    Omitted (``None``, the default), nothing is recorded -- used by
    :meth:`~hyera._config.hiera_config.HieraLevel.paths`, which has no cache to key.

    ``fs_memo``, when given, is the current top-level lookup's filesystem
    probe memo (:attr:`~hyera._lookup.invocation.Invocation._fs_memo`), shared so
    every location this level (and the rest of the same hierarchy build)
    probes is probed at most once for the whole lookup. Omitted, each
    location probed here gets its own, unshared one-entry memo.
    """
    strict_inv = Invocation(
        scope, _no_lookup, scope_interpolations=refs, _fs_memo=fs_memo
    )
    lenient_inv = Invocation(
        scope, _no_lookup, lenient=True, scope_interpolations=refs, _fs_memo=fs_memo
    )

    root = base_path if level.datadir_base is None else level.datadir_base
    config_root = Path(root).as_posix()
    if level.datadir_literal:
        # A version 4 datadir is joined onto the config root as written,
        # with no interpolation at all (`hiera_config.rb:525`) -- unlike
        # every other level, which interpolates it strictly (methods
        # disallowed) just below.
        datadir = _win_slash(level.datadir)
    else:
        datadir = _interpolate_path(level.datadir, strict_inv)
    base = _pathname_plus(config_root, datadir)

    key = level.location_key
    if key is None:
        # No location key at all: the caller (a function provider) calls
        # its function once, with no location -- distinct from a location
        # key that expands to zero candidates.
        return None
    if key in ("path", "paths"):
        return _resolve_paths(
            base, level.locations, lenient_inv, extension=level.extension
        )
    if key in ("glob", "globs"):
        return _expand_globs(config_root, datadir, level.locations, lenient_inv)
    if key == "mapped_paths":
        return _expand_mapped_paths(base, level, lenient_inv)
    # key in ("uri", "uris")
    return _expand_uris(level.locations, lenient_inv)
