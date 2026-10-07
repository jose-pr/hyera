"""Backends installed by other distributions, found through the entry-point
group ``hyera.backends``.

Loading an entry imports its module, and defining a :class:`~hyera.backends.Backend`
subclass registers it. Every entry loads once, the first time the registry is asked
anything, never when ``hyera`` is imported.
"""

from __future__ import annotations

import importlib.metadata as _metadata
import logging
import threading
import typing as _ty

_LOGGER = logging.getLogger("hyera.backends")

_GROUP = "hyera.backends"

#: Reentrant: a plugin module may ask the registry while it is being imported.
_LOCK = threading.RLock()
#: Set when loading begins, so that a question asked from inside a plugin's import
#: returns instead of waiting on itself.
_STARTED = False
#: Set when every entry has been tried; read without the lock.
_DONE = False


def _entries() -> _ty.List[_ty.Any]:
    """The entry points of the group: the mapping of group to entries that Python 3.9
    returns, or the selectable collection of 3.10 and later."""
    found = _metadata.entry_points()
    select = getattr(found, "select", None)
    if select is not None:
        return list(select(group=_GROUP))
    return list(found.get(_GROUP, ()))


def _describe(entry: _ty.Any) -> str:
    dist = getattr(getattr(entry, "dist", None), "name", None)
    return "{!r} of distribution {}".format(
        getattr(entry, "name", "?"), dist or "unknown"
    )


def load() -> None:
    """Import every entry of the group once; a no-op afterwards.

    An entry whose module does not import, or whose backend cannot register, is
    logged once at WARNING (its entry, its distribution and the exception class, never
    the exception's text) and skipped.
    """
    global _STARTED, _DONE
    if _DONE:
        return
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
        try:
            try:
                entries = _entries()
            except Exception as e:
                _LOGGER.warning(
                    "Could not list the %s entry points: %s", _GROUP, type(e).__name__
                )
                return
            for entry in entries:
                try:
                    entry.load()
                except Exception as e:
                    _LOGGER.warning(
                        "Skipping the %s entry point %s: %s",
                        _GROUP,
                        _describe(entry),
                        type(e).__name__,
                    )
        finally:
            _DONE = True
