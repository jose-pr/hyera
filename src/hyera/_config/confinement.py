"""Keeping data file locations inside the directory a hierarchy level names.

Used when ``Hiera(confine_locations=True)``: a location outside a level's
confinement root is treated as absent, and a HOCON ``include`` outside it fails.
"""

from __future__ import annotations

import contextvars
import logging
import os
import typing as _ty

from pathlib_next import Path

from ..exceptions import BackendError
from .pathname import _pathname_plus, _win_slash

if _ty.TYPE_CHECKING:
    from .hiera_config import HieraLevel

#: The confinement root of the data file being read, for the HOCON include
#: check; ``None`` when confinement is off. Set by the location store.
_INCLUDE_ROOT: "contextvars.ContextVar[_ty.Optional[str]]" = contextvars.ContextVar(
    "hiera_include_root", default=None
)

#: Distinct escaped locations remembered for the once-per-location warning; past
#: it a flood of values stops being logged rather than growing without bound.
_WARNED_LIMIT = 256

_LOGGER = logging.getLogger("hyera.core")


def confinement_root(level: "HieraLevel", base_path: "_ty.Any") -> str:
    """The directory a level's data files must stay inside: its ``datadir``,
    or, when the ``datadir`` holds an interpolation, the whole directories
    before the first ``%{``.

    :param level: the hierarchy entry.
    :param base_path: the layer's root, as :func:`resolve_locations` takes it.
    :returns: the root as a ``/``-separated string.
    """
    root = base_path if level.datadir_base is None else level.datadir_base
    config_root = Path(root).as_posix()
    template = _pathname_plus(config_root, _win_slash(level.datadir))
    cut = template.find("%{")
    if cut >= 0:
        template = template[:cut]
        template = template[: template.rfind("/") + 1] or config_root
    return template


def _normal(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def lexically_inside(path: str, root: str) -> bool:
    """Whether ``path`` is under ``root`` by its text alone (``..`` collapsed),
    without touching the filesystem. A path on another drive or share, or one
    that climbs out, is outside.

    :param path: the location.
    :param root: the confinement root.
    :returns: whether the text of ``path`` stays under ``root``.
    """
    return _is_within(_normal(path), _normal(root))


def _real_normal(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def is_inside(path: str, root: str) -> bool:
    """Whether ``path`` is under ``root`` with symbolic links resolved on
    both. The text is checked first, so a path outside it (a UNC path, another
    drive, a ``..`` climb) is never resolved or touched.

    :param path: the location.
    :param root: the confinement root.
    :returns: whether ``path`` stays inside.
    """
    return lexically_inside(path, root) and _is_within(
        _real_normal(path), _real_normal(root)
    )


def glob_stays_inside(root: str, pattern: str, confine_root: str) -> bool:
    """Whether walking ``pattern`` from the directory ``root`` can only list
    directories under ``confine_root``, decided on the text alone: the literal
    directories the pattern names before its first wildcard must lead inside it,
    and no later ``..`` segment may climb above it. A ``..`` inside a brace group
    or after an escape is refused, whatever it climbs.

    :param root: the literal directory the walk starts in.
    :param pattern: the glob pattern under ``root``.
    :param confine_root: the confinement root.
    :returns: whether the walk stays inside.
    """
    text = pattern.replace("\\", "")
    segments = text.split("/")
    literal = len(segments)
    for index, segment in enumerate(segments):
        if any(char in segment for char in "*?[{"):
            literal = index
            break
    start = root.rstrip("/") + "/" + "/".join(segments[:literal])
    if not lexically_inside(start, confine_root):
        return False
    rest = segments[literal:]
    tail = "/".join(rest)
    if ".." not in tail:
        return True
    if "{" in tail:
        return False
    relative = os.path.relpath(_normal(start), _normal(confine_root))
    depth = 0 if relative == "." else len(relative.split(os.sep))
    for segment in rest:
        if segment == "..":
            depth -= 1
            if depth < 0:
                return False
        elif segment not in ("", ".", "**"):
            depth += 1
    return True


class Confiner:
    """The state one :class:`~hyera.Hiera` keeps while it confines locations:
    each root's resolved form, the root a verified file lives under (for the
    HOCON include check) and the locations already warned about."""

    def __init__(self) -> None:
        self._real_roots: _ty.Dict[str, str] = {}
        self._path_roots: _ty.Dict[str, str] = {}
        self._warned: _ty.Set[str] = set()
        self._globs: _ty.Dict[_ty.Tuple[str, str, str], bool] = {}

    def clear(self) -> None:
        """Forget every resolved root, verified file, glob verdict and warning."""
        self._real_roots.clear()
        self._path_roots.clear()
        self._warned.clear()
        self._globs.clear()

    def glob_allowed(self, root: str, pattern: str, confine_root: str) -> bool:
        """Whether a glob walk may start: :func:`glob_stays_inside`, decided
        once per ``(root, pattern)`` and warned about once when refused.

        :param root: the literal directory the walk starts in.
        :param pattern: the glob pattern under ``root``.
        :param confine_root: the level's confinement root.
        :returns: whether the pattern may be walked at all.
        """
        key = (root, pattern, confine_root)
        ok = self._globs.get(key)
        if ok is None:
            ok = glob_stays_inside(root, pattern, confine_root)
            if len(self._globs) < _WARNED_LIMIT:
                self._globs[key] = ok
            if not ok and len(self._warned) < _WARNED_LIMIT:
                self._warned.add(root + "/" + pattern)
                _LOGGER.warning(
                    "Ignoring glob %r under %r: it leaves its data directory "
                    "(confine_locations)",
                    pattern,
                    root,
                )
        return ok

    def allowed(
        self, path: str, root: str, memo: "_ty.Optional[_ty.MutableMapping]" = None
    ) -> bool:
        """Whether ``path`` is inside ``root``, symbolic links resolved, at
        most one resolution per path for the lookup that owns ``memo``.
        An escape is warned about once per path.

        :param path: the location.
        :param root: its level's confinement root.
        :param memo: the lookup's filesystem memo, or ``None``.
        :returns: whether the location may be read.
        """
        key = ("inside", path, root)
        if memo is not None and key in memo:
            return memo[key]
        ok = lexically_inside(path, root)
        if ok:
            real_root = self._real_roots.get(root)
            if real_root is None:
                real_root = self._real_roots[root] = _real_normal(root)
            ok = _is_within(_real_normal(path), real_root)
        if ok:
            self._path_roots[path] = root
        elif path not in self._warned and len(self._warned) < _WARNED_LIMIT:
            self._warned.add(path)
            _LOGGER.warning(
                "Ignoring location %r: outside its data directory "
                "(confine_locations)",
                path,
            )
        if memo is not None:
            memo[key] = ok
        return ok

    def root_of(self, path: str) -> str:
        """The root a verified ``path`` lives under, else its own directory.

        :param path: a data file location.
        :returns: the root its HOCON includes must stay inside.
        """
        return self._path_roots.get(path) or os.path.dirname(path)


def check_include(filename: str) -> None:
    """Raise when a HOCON ``include`` targets a file outside the confinement
    root of the data file being read; do nothing when confinement is off.

    :param filename: the included file as the parser opens it.
    :raises BackendError: the file is outside the root.
    """
    root = _INCLUDE_ROOT.get()
    if root is not None and not is_inside(filename, root):
        raise BackendError(
            "HOCON include of a file outside the data directory "
            "(confine_locations): {!r}".format(os.fspath(filename))
        )
