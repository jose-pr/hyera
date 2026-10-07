# Ported from Puppet 8 lib/puppet/pops/types/types.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Type normalization, the way Puppet reduces a type before it describes it.

Ports ``PVariantType.maybe_create`` and ``PAnyType#normalize`` with the
overrides a mismatch message can show (``pops/types/types.rb``): ``NotUndef[T]``
and ``Optional[T]`` collapse into ``T`` when ``T`` already decides undef, Undef
joins an Optional, string literals and Enums become one Enum, Patterns one
Pattern and overlapping or adjacent Integer ranges one range.
"""

from __future__ import annotations

from .compound_types import Array, Hash, Struct, StructElement, Tuple, Variant
from .types import (
    Enum,
    Integer,
    NotUndef,
    Optional,
    Pattern,
    SensitiveType,
    String,
    Undef,
)

__all__ = ["normalize", "variant_of"]

_INF = float("inf")


def variant_of(types):
    """The one type ``types`` reduce to: a lone type as itself, else a
    normalized :class:`Variant`.

    :param types: the types, each a Variant (flattened) or any other type
    :return: the single remaining type, or a ``Variant`` of the merged ones
    """
    flat = []
    for t in types:
        for member in t.types if isinstance(t, Variant) else (t,):
            if member not in flat:
                flat.append(member)
    if len(flat) == 1:
        return flat[0]
    return _normalize_members(flat)


def normalize(t):
    """``t`` reduced as Puppet's ``normalize`` reduces it (``types.rb:441``,
    ``:567``, ``:2948``, ``:3284``): the contained and member types first, then
    the wrapper or Variant over them.

    :param t: any type; a raw string contained in an Optional or NotUndef is a
        literal String
    :return: the reduced type, ``t`` itself when nothing changes
    """
    if isinstance(t, NotUndef):
        return _normalize_not_undef(t)
    if isinstance(t, Optional):
        return _normalize_optional(t)
    if isinstance(t, Variant):
        return variant_of([normalize(m) for m in t.types]) if t.types else t
    if isinstance(t, SensitiveType):
        inner = None if t.contained is None else normalize(t.contained)
        return t if inner is t.contained else SensitiveType(inner)
    if isinstance(t, Array):
        elem = None if t.element_type is None else normalize(t.element_type)
        if elem is t.element_type:
            return t
        return Array(elem, t.size_from, t.size_to)
    if isinstance(t, Hash):
        key = None if t.key_type is None else normalize(t.key_type)
        value = None if t.value_type is None else normalize(t.value_type)
        if key is t.key_type and value is t.value_type:
            return t
        return Hash(key, value, t.size_from, t.size_to)
    if isinstance(t, Tuple):
        members = [normalize(m) for m in t.types]
        if all(a is b for a, b in zip(members, t.types)):
            return t
        return Tuple(members, t.size_from, t.size_to)
    if isinstance(t, Struct):
        values = [normalize(e.value_type) for e in t.elements]
        if all(a is e.value_type for a, e in zip(values, t.elements)):
            return t
        return Struct(
            StructElement(e.key, e.optional, v) for e, v in zip(t.elements, values)
        )
    return t


def _accepts_undef(t):
    return not isinstance(t, str) and t.instance(None)


def _normalize_not_undef(t):
    inner = t.contained
    if inner is None:
        return t
    if isinstance(inner, str):
        return String(None, None, inner)
    n = normalize(inner)
    if isinstance(n, Optional):
        return normalize(NotUndef(n.contained))
    if not _accepts_undef(n):
        return n
    return t if n is inner else NotUndef(n)


def _normalize_optional(t):
    inner = t.contained
    if inner is None or isinstance(inner, str):
        return t
    n = normalize(inner)
    if isinstance(n, NotUndef):
        return normalize(Optional(n.contained))
    if _accepts_undef(n):
        return n
    return t if n is inner else Optional(n)


def _swap_not_undefs(types):
    """Several ``NotUndef[T]`` become one ``NotUndef[Variant[T...]]``
    (``types.rb:3030``)."""
    wrappers = [t for t in types if isinstance(t, NotUndef)]
    if (
        len(types) < 2
        or len(wrappers) < 2
        or any(w.contained is None for w in wrappers)
    ):
        return types
    others = [t for t in types if not isinstance(t, NotUndef)]
    return others + [NotUndef(variant_of([w.contained for w in wrappers]))]


def _normalize_members(types):
    if any(isinstance(t, (Undef, Optional)) for t in types):
        rest = [
            t.contained if isinstance(t, Optional) else t
            for t in types
            if not isinstance(t, Undef)
        ]
        if any(t is None for t in rest):
            return Variant(types)
        return normalize(Optional(variant_of(rest) if rest else Variant([])))
    merged = _merge_integers(_merge_patterns(_merge_enums(_swap_not_undefs(types))))
    if len(merged) == 1:
        return merged[0]
    # A merge that removed nothing keeps the given order.
    return Variant(merged if len(merged) != len(types) else types)


def _is_literal(t):
    return (
        isinstance(t, str)
        or (isinstance(t, Enum) and bool(t.values) and not t.case_insensitive)
        or (isinstance(t, String) and t.literal is not None)
    )


def _literal_values(t):
    if isinstance(t, str):
        return (t,)
    return t.values if isinstance(t, Enum) else (t.literal,)


def _merge_enums(types):
    literals = [t for t in types if _is_literal(t)]
    if len(types) < 2 or len(literals) < 2:
        return types
    others = [t for t in types if not _is_literal(t)]
    values = set()
    for t in literals:
        values.update(_literal_values(t))
    return others + [Enum(sorted(values))]


def _merge_patterns(types):
    patterns = [t for t in types if isinstance(t, Pattern)]
    if len(types) < 2 or len(patterns) < 2:
        return types
    others = [t for t in types if not isinstance(t, Pattern)]
    sources = []
    for t in patterns:
        sources.extend(s for s in t.sources if s not in sources)
    return others + [Pattern(sources)]


def _bounds(t):
    return (
        -_INF if t.from_ is None else t.from_,
        _INF if t.to is None else t.to,
    )


def _merge_pair(a, b):
    """The range spanning ``a`` and ``b`` when they overlap or touch, else None."""
    lo, hi = _bounds(a)
    olo, ohi = _bounds(b)
    if (lo <= ohi and olo <= hi) or hi + 1 == olo or ohi + 1 == lo:
        lo, hi = min(lo, olo), max(hi, ohi)
        return Integer(None if lo == -_INF else lo, None if hi == _INF else hi)
    return None


def _merge_integers(types):
    ranges = [t for t in types if isinstance(t, Integer)]
    if len(types) < 2 or len(ranges) < 2:
        return types
    merged = []
    while ranges:
        unmerged = []
        memo = ranges.pop()
        for other in ranges:
            joined = _merge_pair(memo, other)
            if joined is None:
                unmerged.append(other)
            else:
                memo = joined
        merged.append(memo)
        ranges = unmerged
    return merged + [t for t in types if not isinstance(t, Integer)]
