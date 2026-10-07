"""The operator's boundary around the MCP tool, set by two environment variables.

``HYERA_MCP_ROOT`` confines every path argument of a tool call, and the data
files a hierarchy reads, to one directory. ``HYERA_MCP_BACKENDS`` limits the
data functions a hierarchy may name. Both are read only while ``HYERA_MCP`` is
set; with neither set the tool is as trusted as the command-line user.
"""

from __future__ import annotations

import os as _os
import typing as _ty

from .._config.confinement import is_inside
from ..backends import Backend
from ._scope import _UsageError

_ROOT_VAR = "HYERA_MCP_ROOT"
_BACKENDS_VAR = "HYERA_MCP_BACKENDS"

#: The path-valued options of the command, by the flag the caller sees.
_PATH_OPTIONS = (
    ("facts", "--facts"),
    ("hiera_config", "--hiera_config"),
    ("environmentpath", "--environmentpath"),
    ("modulepath", "--modulepath"),
    ("basemodulepath", "--basemodulepath"),
    ("codedir", "--codedir"),
)


class Boundary:
    """The parsed operator settings: a root directory, a backend allow-list,
    or both."""

    def __init__(
        self,
        root: _ty.Optional[str],
        backends: "_ty.Optional[_ty.List[_ty.Type[Backend]]]",
    ) -> None:
        self.root = root
        self.backends = backends

    def check(self, opts: dict) -> None:
        """Refuse a call whose path arguments leave the root.

        :param opts: the call's option values, by field name.
        :raises _UsageError: an argument resolves outside the root; the text
            names the flag and nothing of what the path holds.
        """
        if self.root is None:
            return
        for field, flag in _PATH_OPTIONS:
            value = opts.get(field)
            if value is None:
                continue
            parts = value.split(_os.pathsep) if field.endswith("path") else [value]
            for part in parts:
                if part and not is_inside(_os.path.abspath(part), self.root):
                    raise _UsageError("{} is outside {}".format(flag, _ROOT_VAR))
        if opts.get("hiera_config") is None and not is_inside(_os.getcwd(), self.root):
            raise _UsageError("the working directory is outside {}".format(_ROOT_VAR))

    def hiera_options(self) -> dict:
        """The :class:`~hyera.Hiera` keyword arguments the boundary imposes.

        :returns: ``confine_locations`` and, when set, ``backends``.
        """
        options: dict = {}
        if self.root is not None:
            options["confine_locations"] = True
        if self.backends is not None:
            options["backends"] = self.backends
        return options


#: The boundary of the server this process started as; ``None`` otherwise.
#: The MCP library consumes ``HYERA_MCP`` when it starts, so a tool call
#: cannot read the variables itself.
_ACTIVE: _ty.Optional[Boundary] = None


def active() -> _ty.Optional[Boundary]:
    """The boundary :func:`activate` loaded, if any.

    :returns: the boundary of this process, or ``None``.
    """
    return _ACTIVE


def activate() -> None:
    """Load the boundary for this process before the command starts.

    :raises _UsageError: see :func:`load`.
    """
    global _ACTIVE
    _ACTIVE = None
    _ACTIVE = load()


def load() -> _ty.Optional[Boundary]:
    """Read the operator's variables, once, for an MCP server.

    :returns: the boundary, or ``None`` when ``HYERA_MCP`` is unset or neither
        variable is.
    :raises _UsageError: the root is not a directory or a backend name is
        unknown.
    """
    if not _os.environ.get("HYERA_MCP"):
        return None
    root = _os.environ.get(_ROOT_VAR) or None
    names = _os.environ.get(_BACKENDS_VAR) or None
    if root is None and names is None:
        return None
    if root is not None:
        if not _os.path.isdir(root):
            raise _UsageError("{} is not a directory".format(_ROOT_VAR))
        root = _os.path.abspath(root)
    backends = None
    if names is not None:
        backends = []
        for name in (n.strip() for n in names.split(",")):
            found = Backend.find(name, kind="function") if name else None
            if found is None:
                raise _UsageError("{} names an unknown function".format(_BACKENDS_VAR))
            if found not in backends:
                backends.append(found)
    return Boundary(root, backends)
