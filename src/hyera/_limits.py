"""Opt-in bounds on what one document or pattern may cost."""

from __future__ import annotations

import contextvars
import typing as _ty

__all__ = ["Limits"]

_FIELDS = ("yaml_alias_nodes", "glob_patterns", "hocon_substitution_size")


class Limits:
    """Ceilings on three costs a data file or a location pattern can impose,
    for a caller that passes scope values or data it does not control.

    Every field is ``None`` (unbounded, Puppet's behaviour) or a positive
    ``int``. An instance is immutable, hashable and compares by value.

    :param yaml_alias_nodes: the most nodes one YAML document may yield
        through alias references (``*name``) before the load fails.
    :param glob_patterns: the most patterns one ``glob`` entry may expand to
        through ``{a,b}`` alternatives before the lookup fails.
    :param hocon_substitution_size: the largest value one HOCON ``${...}``
        substitution may insert before the load fails: characters of a
        string, nodes plus characters of a list or object.
    :raises TypeError: a field is not an ``int`` or ``None``.
    :raises ValueError: a field is not positive.
    """

    __slots__ = _FIELDS

    yaml_alias_nodes: _ty.Optional[int]
    glob_patterns: _ty.Optional[int]
    hocon_substitution_size: _ty.Optional[int]

    def __init__(
        self,
        *,
        yaml_alias_nodes: _ty.Optional[int] = None,
        glob_patterns: _ty.Optional[int] = None,
        hocon_substitution_size: _ty.Optional[int] = None,
    ) -> None:
        for name, value in (
            ("yaml_alias_nodes", yaml_alias_nodes),
            ("glob_patterns", glob_patterns),
            ("hocon_substitution_size", hocon_substitution_size),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    "{} must be an int or None, not {}".format(
                        name, type(value).__name__
                    )
                )
            if value < 1:
                raise ValueError("{} must be positive".format(name))
        object.__setattr__(self, "yaml_alias_nodes", yaml_alias_nodes)
        object.__setattr__(self, "glob_patterns", glob_patterns)
        object.__setattr__(self, "hocon_substitution_size", hocon_substitution_size)

    def __setattr__(self, name: str, value: _ty.Any) -> None:
        raise AttributeError("Limits is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Limits is immutable")

    def __reduce__(self) -> _ty.Tuple[_ty.Any, ...]:
        return (_rebuild, self._key())

    def _key(self) -> _ty.Tuple[_ty.Optional[int], ...]:
        return (
            self.yaml_alias_nodes,
            self.glob_patterns,
            self.hocon_substitution_size,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Limits):
            return NotImplemented
        return self._key() == other._key()

    def __hash__(self) -> int:
        return hash(self._key())

    def __repr__(self) -> str:
        return (
            "Limits(yaml_alias_nodes={!r}, glob_patterns={!r}, "
            "hocon_substitution_size={!r})"
        ).format(*self._key())


def _rebuild(yaml_alias_nodes, glob_patterns, hocon_substitution_size) -> Limits:
    return Limits(
        yaml_alias_nodes=yaml_alias_nodes,
        glob_patterns=glob_patterns,
        hocon_substitution_size=hocon_substitution_size,
    )


#: The limits of the lookup in progress; ``Backend.limits`` reads it. Set around
#: every read of a data file by the location store, reset in ``finally``.
_LIMITS: "contextvars.ContextVar[_ty.Optional[Limits]]" = contextvars.ContextVar(
    "hiera_limits", default=None
)
