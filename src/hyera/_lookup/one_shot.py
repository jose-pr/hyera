"""The module-level one-shot :func:`hyera.lookup`."""

from __future__ import annotations

import os
import typing as _ty

from .._scope.scope import Scope
from ..core import Hiera
from ..types import TypeLike
from .merge_strategy import MergeLike
from .navigation import _MISSING


def lookup(
    base_config: "_ty.Union[str, os.PathLike[str], _ty.IO[str], _ty.IO[bytes], _ty.Dict[str, _ty.Any], None]",
    name: "_ty.Union[str, _ty.Tuple[_ty.Union[str, int], ...], _ty.Sequence[_ty.Union[str, _ty.Tuple[_ty.Union[str, int], ...]]], _ty.Mapping[str, _ty.Any]]",
    value_type: "_ty.Union[str, TypeLike, _ty.Mapping[str, _ty.Any], None]" = None,
    merge: MergeLike = None,
    default_value: _ty.Any = _MISSING,
    *,
    default_values_hash: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    override: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    block: _ty.Optional[_ty.Callable[..., _ty.Any]] = None,
    facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    scope: _ty.Optional[Scope] = None,
) -> _ty.Any:
    """Look ``name`` up in ``base_config`` once: build a :class:`~hyera.Hiera`
    and return its :meth:`~hyera.Hiera.lookup`.

    A new instance is built on every call and nothing is cached between calls:
    use :class:`~hyera.Hiera` to look up more than one name.

    :param base_config: as :class:`~hyera.Hiera`'s first argument: a path, a
        file object, a dict, or ``None`` for Puppet's default configuration.
    :param name: as :meth:`~hyera.Hiera.lookup`.
    :param value_type: as :meth:`~hyera.Hiera.lookup`.
    :param merge: as :meth:`~hyera.Hiera.lookup`.
    :param default_value: as :meth:`~hyera.Hiera.lookup`.
    :param default_values_hash: as :meth:`~hyera.Hiera.lookup`.
    :param override: as :meth:`~hyera.Hiera.lookup`.
    :param block: as :meth:`~hyera.Hiera.lookup`.
    :param facts: node facts for the lookup, a mapping (a facts file is read
        with :func:`~hyera.load_facts`); not with ``scope``.
    :param scope: the lookup's scope; not with ``facts``.
    :returns: the found (or defaulted) value.
    :raises TypeError: both ``facts`` and ``scope`` were given, or the lookup
        arguments match none of the call forms of :meth:`~hyera.Hiera.lookup`.
    :raises ConfigError: ``base_config`` is missing or invalid.
    :raises KeyNotFoundError: no value was found and no default was given.
    :raises HieraLookupError: a type assertion failed or resolving the key
        failed, as :meth:`~hyera.Hiera.lookup` raises it.
    """
    if facts is not None:
        if scope is not None:
            raise TypeError("lookup(): pass facts or scope, not both")
        scope = Scope(facts=facts)
    return Hiera(base_config, scope=scope).lookup(
        name,
        value_type,
        merge,
        default_value,
        default_values_hash=default_values_hash,
        override=override,
        block=block,
    )
