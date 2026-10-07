"""Helpers for testing a :class:`~hyera.backends.Backend`: a hook runs the way
the engine runs it, from a unit test and without a hiera.yaml.

Nothing here imports pytest or an optional dependency.
"""

from __future__ import annotations

import typing as _ty

from ._lookup.function_provider import (
    LookupContext,
    _check_kind_implemented,
    _data_hash_not_found,
    _NotFound,
    _tuples_to_lists,
    _validate_data_hash,
    _validate_provider_value,
)
from ._lookup.lookup_adapter import validate_data_value
from .backends._base import Backend

__all__ = ["NOT_FOUND", "data_dig", "data_hash", "lookup_key"]


class _NotFoundType:
    """The type of :data:`NOT_FOUND`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "hyera.testing.NOT_FOUND"


#: What :func:`lookup_key` and :func:`data_dig` return when the hook called
#: ``context.not_found()``. Compare with ``is``.
NOT_FOUND = _NotFoundType()


def _instance(backend: _ty.Union[Backend, _ty.Type[Backend]]) -> Backend:
    return backend() if isinstance(backend, type) else backend


def lookup_key(
    backend: _ty.Union[Backend, _ty.Type[Backend]],
    key: str,
    options: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    *,
    context: _ty.Optional[LookupContext] = None,
) -> _ty.Any:
    """Call ``backend``'s ``lookup_key`` hook as the engine does.

    The hook must be implemented and its value must be one Hiera accepts; the
    value comes back with tuples read as lists.

    :param backend: a backend instance, or a class to instantiate with no
        arguments.
    :param key: the key the hook is asked for.
    :param options: the hierarchy entry's ``options``; none when omitted.
    :param context: the context passed to the hook;
        :meth:`LookupContext.for_testing() <hyera.LookupContext.for_testing>`
        when omitted.
    :returns: the hook's value, or :data:`NOT_FOUND` when it called
        ``context.not_found()``.
    :raises ConfigError: the backend does not implement ``lookup_key``.
    :raises BackendError: the hook returned a value outside Puppet's data types.
    """
    backend = _instance(backend)
    _check_kind_implemented(backend, "lookup_key")
    if context is None:
        context = LookupContext.for_testing()
    try:
        value = backend.lookup_key(key, dict(options or {}), context)
    except _NotFound:
        return NOT_FOUND
    return _validate_provider_value(value, "lookup_key", backend.name, None)


def data_dig(
    backend: _ty.Union[Backend, _ty.Type[Backend]],
    segments: _ty.Sequence[str],
    options: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    *,
    context: _ty.Optional[LookupContext] = None,
) -> _ty.Any:
    """Call ``backend``'s ``data_dig`` hook as the engine does.

    :param backend: a backend instance, or a class to instantiate with no
        arguments.
    :param segments: the already-split key the hook is asked for.
    :param options: the hierarchy entry's ``options``; none when omitted.
    :param context: the context passed to the hook;
        :meth:`LookupContext.for_testing() <hyera.LookupContext.for_testing>`
        when omitted.
    :returns: the hook's value, or :data:`NOT_FOUND` when it called
        ``context.not_found()``.
    :raises ConfigError: the backend does not implement ``data_dig``.
    :raises BackendError: the hook returned a value outside Puppet's data types.
    """
    backend = _instance(backend)
    _check_kind_implemented(backend, "data_dig")
    if context is None:
        context = LookupContext.for_testing()
    try:
        value = backend.data_dig(list(segments), dict(options or {}), context)
    except _NotFound:
        return NOT_FOUND
    return _validate_provider_value(value, "data_dig", backend.name, None)


def data_hash(
    backend: _ty.Union[Backend, _ty.Type[Backend]],
    path: _ty.Optional[str] = None,
    options: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    *,
    context: _ty.Optional[LookupContext] = None,
) -> _ty.Dict[str, _ty.Any]:
    """Call ``backend``'s ``data_hash`` hook as the engine does.

    The hook must return a hash whose values are Puppet data.

    :param backend: a backend instance, or a class to instantiate with no
        arguments.
    :param path: the location's file path, or ``None`` for a location-less
        entry.
    :param options: the hierarchy entry's ``options``; none when omitted. The
        hook receives them with ``path`` added, as the engine does for a located
        entry.
    :param context: the context passed to the hook;
        :meth:`LookupContext.for_testing() <hyera.LookupContext.for_testing>`
        when omitted.
    :returns: the hash, with tuples read as lists.
    :raises ConfigError: the backend does not implement ``data_hash``.
    :raises BackendError: the hook returned something other than a hash, or
        called ``context.not_found()``.
    :raises HieraLookupError: a value of the hash is outside Puppet's data
        types.
    """
    backend = _instance(backend)
    _check_kind_implemented(backend, "data_hash")
    if context is None:
        context = LookupContext.for_testing()
    label = None if path is None else str(path)
    merged = dict(options or {})
    if label is not None:
        merged["path"] = label
    try:
        data = backend.data_hash(path, merged, context)
    except _NotFound:
        raise _data_hash_not_found(backend.name, label) from None
    _validate_data_hash(data, backend.name, label)
    for key, value in data.items():
        validate_data_value(value, backend.name, label, key)
    return {key: _tuples_to_lists(value) for key, value in data.items()}
