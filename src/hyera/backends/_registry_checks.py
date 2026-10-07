"""The argument checks of the backend registry's lookup methods."""

from __future__ import annotations

import typing as _ty

from .._enums import _plain

__all__: "list[str]" = []


def check_kind(kind: _ty.Any, kinds: _ty.Sequence[str]) -> str:
    """``kind`` as a plain namespace name.

    :param kind: the argument given.
    :param kinds: the namespaces that exist.
    :returns: ``kind`` as a ``str``.
    :raises TypeError: ``kind`` is neither a ``BackendKind`` nor a ``str``.
    :raises ValueError: ``kind`` names no namespace.
    """
    kind = _plain(kind)
    if not isinstance(kind, str):
        raise TypeError(
            "kind must be a BackendKind or a str, not {}".format(type(kind).__name__)
        )
    if kind not in kinds:
        raise ValueError(
            "kind must be one of {!r}, not {!r}".format(tuple(kinds), kind)
        )
    return kind


def unknown_backend(kind: str, name: str, known: _ty.Iterable[str]) -> ValueError:
    """The error for a ``name`` registered in no namespace entry of ``kind``.

    :param kind: the namespace searched.
    :param name: the name asked for.
    :param known: the names registered in ``kind``.
    :returns: the exception to raise.
    """
    return ValueError(
        "Unknown {} backend {!r}; known: {}".format(kind, name, ", ".join(known))
    )
