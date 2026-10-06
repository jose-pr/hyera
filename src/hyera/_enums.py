"""Shared base for hyera's public string enums (``Merge``, ``Strict``,
``FunctionKind``, ``BackendKind``, ``RenderAs``): each lives in the module
that owns its concept (``_lookup/merge_strategy.py``, ``_scope/scope.py``,
``_config/hiera_config.py``, ``backends``, ``_output/render.py``) and is
re-exported from ``hyera`` itself; this module holds only what they share.

Every hyera enum is a ``str``: ``Merge.DEEP == "deep"``, so every parameter
that took the plain string still takes the member -- nothing already written
against these APIs needs to change. ``str()``/``format()``/an f-string all
give the member's *value* on every supported Python: the 3.9 floor has no
``enum.StrEnum``, and a bare ``(str, enum.Enum)`` mixin's own ``str()``/
``format()`` differ between 3.9-3.10 (``"ClassName.MEMBER"``) and 3.11+
(the value, matching ``StrEnum``) -- overriding both here makes every
supported interpreter behave the same way.
"""

from __future__ import annotations

import enum as _enum
import typing as _ty


class _StrEnum(str, _enum.Enum):
    """A ``str``-mixin ``Enum`` whose ``str()``/``format()`` always give the
    member's value, identically on every supported Python."""

    def __str__(self) -> str:
        """The member's value (never ``"ClassName.MEMBER"``)."""
        return str(self.value)

    def __format__(self, format_spec: str) -> str:
        """Format the member's value, not the member itself."""
        return format(str(self.value), format_spec)


def _plain(value: _ty.Any) -> _ty.Any:
    """Collapse a hyera string-enum member to a bare ``str``; anything else
    (including a plain ``str`` already) passes through unchanged.

    Called at every public entry point that accepts one of these enums, so
    nothing downstream -- an internal dict key, a stored attribute, an
    exception message, a rendered/explained value -- ever sees the member
    itself: every stored/returned value stays a plain ``str``.
    """
    return value.value if isinstance(value, _enum.Enum) else value
