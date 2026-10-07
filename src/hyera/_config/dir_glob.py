"""Ruby's ``Dir.glob`` over a literal root directory.

Brace expansion, the ``fnmatch`` matcher and the directory walk, with hooks
the location store uses to record and probe scandir results.
"""

from __future__ import annotations

import functools
import logging
import os
import stat
import typing as _ty

from ..exceptions import BackendError

_LOGGER = logging.getLogger(__name__)


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


def _expand_braces(
    pattern: str, max_patterns: "_ty.Optional[int]" = None
) -> "_ty.List[str]":
    """Ruby ``ruby_brace_expand``: the concatenation of ``pattern`` with
    each alternative of its first ``{...}`` group substituted in, in
    written order, recursing on every remaining group (so nested braces and
    a later sibling group both expand). A pattern with no unescaped,
    balanced ``{...}`` group -- including an unmatched ``{`` -- expands to
    itself, escapes and all: :func:`_has_magic`/:func:`_segment_matcher`
    are what later interpret ``\\``. Iterative, so nesting depth is bounded
    by memory, not the interpreter's recursion limit.

    With ``max_patterns``, expansion stops with :class:`BackendError` as soon
    as it has produced more patterns than that, before the rest are built.
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
            if max_patterns is not None and len(results) > max_patterns:
                raise BackendError(
                    "A glob expands to more than {} patterns "
                    "(limits.glob_patterns)".format(max_patterns)
                )
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


class _LiteralEntry:
    """A literal segment the operating system says exists although no listing
    entry has exactly that name (a case-insensitive filesystem), spelled as
    the pattern spells it. Never descended into by ``**``: its real entry is."""

    def __init__(self, name: str) -> None:
        self.name = name


def _segment_matches(kind, value, name: str) -> bool:
    if kind == "literal":
        return name == value
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
    child by name, and whether or not the directory exists -- which records
    what a listing depended on, for a later freshness check.

    ``probe_isdir``, when given, replaces the plain ``os.path.isdir(child)``
    check an intermediate literal segment uses to decide whether to descend
    -- a memo-aware caller (``_LocationStore.glob_matches``) passes one so this
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
        # A literal segment is an existence check through the OS and follows the
        # filesystem's case rule; only wildcards are always case-sensitive. A listing
        # that lacks the exact spelling is asked about that one child.
        names = {e.name for e in entries}
        wanted = {}
        for i in pending:
            kind, value = segments[i]
            if kind == "recursive":
                i, kind, value = i + 1, *segments[i + 1]
            if kind == "literal" and value not in names:
                wanted[value] = i + 1 == n
        for lit, final in wanted.items():
            child = os.path.join(path, lit)
            if final:
                found = os.path.lexists(child)
            elif probe_isdir is not None:
                found = probe_isdir(child)
            else:
                found = os.path.isdir(child)
            if found:
                entries.append(_LiteralEntry(lit))
        entries.sort(key=lambda e: os.fsencode(e.name))
        for entry in entries:
            nxt = set()
            for i in pending:
                kind, value = segments[i]
                if kind == "recursive":
                    if (
                        not entry.name.startswith(".")
                        and not isinstance(entry, _LiteralEntry)
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


def glob(
    root: str,
    pattern: str,
    on_scandir=None,
    probe_isdir=None,
    max_patterns: "_ty.Optional[int]" = None,
) -> "_ty.List[str]":
    """Ruby ``Dir.glob`` for ``pattern`` under the literal directory
    ``root``: the concatenation of :func:`_glob_one` over every brace
    alternative of ``pattern``, in written order (duplicates kept, as Ruby
    keeps them).
    Results are ``os.path.join``ed absolute strings; directories are
    included here (a caller wanting files only, as every Hiera glob level
    does, filters them out itself). ``on_scandir``/``probe_isdir``: see
    :func:`_glob_one`; ``max_patterns``: see :func:`_expand_braces`."""
    results = []
    for p in _expand_braces(pattern, max_patterns):
        results.extend(_glob_one(root, p, on_scandir, probe_isdir))
    return results
