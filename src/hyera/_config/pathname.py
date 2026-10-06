"""Ruby's ``Pathname#+`` and the Windows anchor rules host paths follow.

Joins and splits path strings lexically, the way ``pathname.rb`` does, and
reads ``\\`` as ``/`` on Windows only.
"""

from __future__ import annotations

import os
import re

_WINDOWS = os.name == "nt"


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
