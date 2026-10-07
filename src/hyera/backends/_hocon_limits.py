"""The bound on what one HOCON substitution may insert
(``Limits.hocon_substitution_size``), applied inside the private parser copy.
"""

from __future__ import annotations

import typing as _ty

from .._limits import _LIMITS
from ..exceptions import BackendError


def _size(value: _ty.Any, limit: int) -> int:
    """Characters of a string, or nodes plus characters of a list or object;
    stops counting once past ``limit``."""
    if isinstance(value, str):
        return len(value)
    total = 0
    stack = [value]
    while stack:
        item = stack.pop()
        total += 1
        if total > limit:
            break
        if isinstance(item, str):
            total += len(item)
        elif isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
        elif hasattr(item, "tokens"):
            stack.extend(item.tokens)
    return total


def install_substitution_bound(mod: _ty.Any) -> None:
    """Make ``mod.ConfigParser`` refuse a substitution whose value is larger
    than the active limit, before it is copied into the document."""
    substitute = mod.ConfigParser._do_substitute.__func__

    def bounded(cls, substitution, resolved_value, is_optional_resolved=True):
        limits = _LIMITS.get()
        limit = None if limits is None else limits.hocon_substitution_size
        if limit is not None and resolved_value is not None:
            if _size(resolved_value, limit) > limit:
                raise BackendError(
                    "hocon_data: a substitution would insert more than {} "
                    "characters or nodes (limits.hocon_substitution_size)".format(limit)
                )
        return substitute(cls, substitution, resolved_value, is_optional_resolved)

    mod.ConfigParser._do_substitute = classmethod(bounded)
