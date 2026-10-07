# Ported from Puppet 8 lib/puppet/pops/types/types.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""One type from several, the way Puppet normalizes a Variant.

Ports ``PVariantType.maybe_create`` and the merges of ``PVariantType#normalize``
(``pops/types/types.rb``) that a mismatch message can show: Undef into Optional,
string literals and Enums into one Enum, Patterns into one Pattern and
overlapping or adjacent Integer ranges into one range.
"""

from __future__ import annotations

from .compound_types import Variant
from .types import Enum, Integer, Optional, Pattern, String, Undef

__all__ = ["variant_of"]

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
    return _normalize(flat)


def _normalize(types):
    if any(isinstance(t, (Undef, Optional)) for t in types):
        rest = [
            t.contained if isinstance(t, Optional) else t
            for t in types
            if not isinstance(t, Undef)
        ]
        if not rest or any(t is None for t in rest):
            return Variant(types)
        return Optional(variant_of(rest))
    merged = _merge_integers(_merge_patterns(_merge_enums(types)))
    if len(merged) == 1:
        return merged[0]
    # A merge that removed nothing keeps the given order.
    return Variant(merged if len(merged) != len(types) else types)


def _is_literal(t):
    return (isinstance(t, Enum) and bool(t.values) and not t.case_insensitive) or (
        isinstance(t, String) and t.literal is not None
    )


def _merge_enums(types):
    literals = [t for t in types if _is_literal(t)]
    if len(types) < 2 or len(literals) < 2:
        return types
    others = [t for t in types if not _is_literal(t)]
    values = set()
    for t in literals:
        values.update(t.values if isinstance(t, Enum) else (t.literal,))
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
