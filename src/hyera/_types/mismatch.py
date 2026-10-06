# Ported from Puppet 8 lib/puppet/pops/types/type_mismatch_describer.rb,
# type_asserter.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Puppet's type mismatch messages and instance assertion.

Ports the subset of ``type_mismatch_describer.rb`` and ``type_asserter.rb``
hyera's supported type tiers exercise: path elements (entry/key of
entry/index/variant), the Array/Hash/Struct/Tuple/Variant/Optional/
Enum/Pattern describers, and ``assert_instance_of``.
Callable/signature describing is not ported: hiera asserts
values, never function signatures.
"""

from __future__ import annotations

from ..exceptions import HieraLookupError
from .types import (
    ANY,
    Array,
    Enum,
    Hash,
    NotUndef,
    Optional,
    Pattern,
    SensitiveType,
    String,
    Struct,
    Tuple,
    TypeAlias,
    TypeReference,
    Variant,
    _type_instance,
    puppet_quote,
    infer_set,
)

__all__ = ["assert_instance_of"]

#: Types whose own formatter/short_name keeps one bare level of their
#: contained type's name (``type_mismatch_describer.rb`` ``short_name``,
#: also ``type_formatter.rb``'s dedicated ``string_P*`` methods for these
#: three -- ``Type`` is the fourth in Puppet but unparameterized in this
#: subset, so it never reaches here with a contained type).
_WRAPPER_TYPES = (Optional, NotUndef, SensitiveType)


def _is_any(t):
    return type(t) is type(ANY)


def _bare_name(t):
    if isinstance(t, str):
        return "String"
    return t.name


def _a_an(label):
    first = ""
    for ch in label:
        if ch not in "'\"":
            first = ch
            break
    return "an" if first and first.lower() in "aeiouy" else "a"


def short_name(t):
    if isinstance(t, str):
        return "String"
    if isinstance(t, _WRAPPER_TYPES) and t.contained is not None:
        if isinstance(t.contained, str) or not _is_any(t.contained):
            return "{}[{}]".format(t.name, _bare_name(t.contained))
    return t.name


def _detailed(expected, actual):
    """Puppet's ``always_fully_detailed?`` simplified to the one condition
    every case in this subset's goldens turns on: same Ruby/Python class."""
    if isinstance(expected, str):
        return False
    return (
        type(expected) is type(actual)
        or (isinstance(expected, Struct) and isinstance(actual, Hash))
        or (isinstance(expected, Tuple) and isinstance(actual, Array))
    )


def _render_pair(expected, actual):
    """The (expected, actual) strings for one leaf type mismatch, per
    ``report_detailed?``/``short_name``."""
    if _detailed(expected, actual):
        return str(expected), str(actual)
    return short_name(expected), short_name(actual)


class _Mismatch:
    __slots__ = ("path", "kind", "expected", "actual", "optional", "key", "ref")

    def __init__(
        self, path, kind, expected=None, actual=None, optional=False, key=None, ref=None
    ):
        self.path = path
        self.kind = kind
        self.expected = expected
        self.actual = actual
        self.optional = optional
        self.key = key
        self.ref = ref


def _path_prefix(path):
    return (" " + " ".join(path)) if path else ""


def _format_one(name, m):
    pos = _path_prefix(m.path)
    if m.kind == "missing_key":
        return "{}{} expects a value for key {}".format(name, pos, puppet_quote(m.key))
    if m.kind == "extra_key":
        return "{}{} unrecognized key {}".format(name, pos, puppet_quote(m.key))
    if m.kind == "unresolved":
        return "{}{} references an unresolved type {}".format(
            name, pos, puppet_quote(m.ref)
        )
    if m.kind == "size":
        return "{}{} expects size to be {}, got {}".format(
            name, pos, m.expected, m.actual
        )
    if m.kind == "pattern":
        e_render = (
            str(m.expected)
            if not isinstance(m.expected, list)
            else _join_or([str(t) for t in m.expected])
        )
        prefix = "an undef value or a match" if m.optional else "a match"
        return "{}{} expects {} for {}, got {}".format(
            name, pos, prefix, e_render, _actual_literal(m.actual)
        )
    # 'type'
    if isinstance(m.expected, list):
        parts = [_render_pair(t, m.actual)[0] for t in m.expected]
        if m.optional:
            parts = ["Undef"] + parts
        e_render = _join_or(parts)
        detailed_any = any(_detailed(t, m.actual) for t in m.expected)
        a_render = str(m.actual) if detailed_any else short_name(m.actual)
        return "{}{} expects a value of type {}, got {}".format(
            name, pos, e_render, a_render
        )
    e_render, a_render = _render_pair(m.expected, m.actual)
    if m.optional:
        return "{}{} expects a value of type Undef or {}, got {}".format(
            name, pos, e_render, a_render
        )
    return "{}{} expects {} {} value, got {}".format(
        name, pos, _a_an(e_render), e_render, a_render
    )


def _join_or(parts):
    uniq = []
    for p in parts:
        if p not in uniq:
            uniq.append(p)
    if len(uniq) == 1:
        return uniq[0]
    if len(uniq) == 2:
        return "{} or {}".format(uniq[0], uniq[1])
    return "{}, or {}".format(", ".join(uniq[:-1]), uniq[-1])


def _actual_literal(actual_type):
    """A ``PatternMismatch``'s "got" side is the value itself, quoted --
    never the type name (``string_PStringType`` never shows a value's own
    literal, but Puppet's ``PatternMismatch#message`` uses ``actual.value``
    directly)."""
    if isinstance(actual_type, String) and actual_type.literal is not None:
        return puppet_quote(actual_type.literal)
    return short_name(actual_type)


# ------------------------------------------------------------- describe


def _describe(expected, value, path):
    """Every reason ``value`` is not an instance of ``expected``, as a list
    of :class:`_Mismatch` (empty when it IS an instance)."""
    if isinstance(expected, str):
        if value == expected:
            return []
        return [_Mismatch(path, "type", expected, infer_set(value))]

    if isinstance(expected, Optional):
        if value is None:
            return []
        if expected.contained is None:
            return [_Mismatch(path, "type", expected, infer_set(value))]
        sub = _describe(expected.contained, value, path)
        if not sub:
            return []
        m = sub[0]
        m.optional = True
        return [m]

    if isinstance(expected, NotUndef):
        if value is None:
            return [_Mismatch(path, "type", expected, infer_set(value))]
        if expected.contained is None:
            return []
        # NotUndef[T] on a non-None value describes exactly as T does (its
        # own instance set already excludes Undef): see AGENTS.md Gotchas.
        return _describe(expected.contained, value, path)

    if isinstance(expected, Variant):
        return _describe_variant(expected, value, path)

    if isinstance(expected, Array):
        return _describe_array(expected, value, path)

    if isinstance(expected, Tuple):
        return _describe_tuple(expected, value, path)

    if isinstance(expected, Hash):
        return _describe_hash(expected, value, path)

    if isinstance(expected, Struct):
        return _describe_struct(expected, value, path)

    if isinstance(expected, (Enum, Pattern)):
        if expected.instance(value):
            return []
        return [_Mismatch(path, "pattern", expected, infer_set(value))]

    if isinstance(expected, TypeReference):
        return [_Mismatch(path, "unresolved", ref=expected.text)]

    if isinstance(expected, TypeAlias):
        # Puppet's own special case (``describe_PVariantType``): once every
        # branch of an aliased Variant fails, it reports one mismatch on
        # the alias itself, never the branches' own structural detail.
        if expected.instance(value):
            return []
        return [_Mismatch(path, "type", expected, infer_set(value))]

    if _type_instance(expected, value):
        return []
    return [_Mismatch(path, "type", expected, infer_set(value))]


def _variant(i):
    return "variant {}".format(i)


def _describe_variant(expected, value, path):
    if not expected.types:
        return [_Mismatch(path, "type", expected, infer_set(value))]
    per_branch = []
    for i, t in enumerate(expected.types):
        sub = _describe(t, value, path)
        if not sub:
            return []
        per_branch.append((i, t, sub))

    # Every branch failed. When every failure landed at THIS level (no
    # deeper path), Puppet collapses them into one combined message; a
    # pattern-shaped branch set renders as "a match for Variant[...]",
    # anything else as "a value of type A, B, or C" (each rendered with
    # its own detailed-vs-short rule).
    immediate = [
        (i, t, sub) for i, t, sub in per_branch if len(sub) == 1 and sub[0].path == path
    ]
    if len(immediate) == len(per_branch):
        actual = infer_set(value)
        if any(isinstance(t, (Enum, Pattern)) for _, t, _ in per_branch):
            return [_Mismatch(path, "pattern", expected, actual)]
        types = [t for _, t, _ in per_branch]
        return [_Mismatch(path, "type", types, actual)]

    # A mix of immediate and nested failures: report the first branch that
    # failed deeper in the structure, prefixed with its "variant N" path
    # element (the shallow, whole-value mismatches on the other branches
    # are the less informative ones -- a simplification of Puppet's own
    # ``merge_descriptions``, see AGENTS.md Gotchas).
    #
    # `immediate` is exactly the per_branch entries where
    # `len(sub) == 1 and sub[0].path == path`, so `len(immediate) !=
    # len(per_branch)` (the only way to reach here) guarantees at least
    # one entry fails that same condition -- `next()` below can never
    # exhaust the generator. Written with `next()` rather than a `for`
    # loop so that guarantee is structural (no reachable "ran out of
    # entries" path for a branch-coverage tool to ever ask about).
    i, t, sub = next(
        (i, t, sub) for i, t, sub in per_branch if len(sub) != 1 or sub[0].path != path
    )
    m = sub[0]
    m.path = path + [_variant(i)] + m.path[len(path) :]
    return [m]


def _size_mismatch(path, from_, to_, actual_n):
    return _Mismatch(
        path, "size", expected=_size_text(from_, to_), actual=str(actual_n)
    )


def _size_text(from_, to_):
    low = from_ or 0
    if to_ is None:
        return "unlimited" if low == 0 else "at least {}".format(low)
    if low == to_:
        return str(low)
    if low == 0:
        return "at most {}".format(to_)
    return "between {} and {}".format(low, to_)


def _describe_array(expected, value, path):
    if not isinstance(value, (list, tuple)):
        return [_Mismatch(path, "type", expected, infer_set(value))]
    n = len(value)
    lo = expected.size_from if expected.size_from is not None else 0
    hi = expected.size_to
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        return [_size_mismatch(path, expected.size_from, expected.size_to, n)]
    element_type = expected.element_type if expected.element_type is not None else ANY
    descriptions = []
    for idx, v in enumerate(value):
        descriptions.extend(_describe(element_type, v, path + [_idx(idx)]))
    return descriptions


def _describe_tuple(expected, value, path):
    if not isinstance(value, (list, tuple)):
        return [_Mismatch(path, "type", expected, infer_set(value))]
    lo, hi = expected._bounds()
    n = len(value)
    if n < lo or (hi is not None and n > hi):
        return [_size_mismatch(path, lo, hi, n)]
    descriptions = []
    for idx, v in enumerate(value):
        t = (
            expected.types[idx]
            if idx < len(expected.types)
            else (expected.types[-1] if expected.types else ANY)
        )
        descriptions.extend(_describe(t, v, path + [_idx(idx)]))
    return descriptions


def _idx(i):
    return "index {}".format(i)


def _describe_hash(expected, value, path):
    if not isinstance(value, dict):
        return [_Mismatch(path, "type", expected, infer_set(value))]
    n = len(value)
    lo = expected.size_from if expected.size_from is not None else 0
    hi = expected.size_to
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        return [_size_mismatch(path, expected.size_from, expected.size_to, n)]
    key_type = expected.key_type if expected.key_type is not None else ANY
    value_type = expected.value_type if expected.value_type is not None else ANY
    descriptions = []
    for k, v in value.items():
        descriptions.extend(_describe(key_type, k, path + [_key_of(k)]))
        descriptions.extend(_describe(value_type, v, path + [_entry(k)]))
    return descriptions


def _entry(k):
    return "entry {}".format(puppet_quote(k))


def _key_of(k):
    return "key of entry {}".format(puppet_quote(k))


def _common_type(types):
    """One type for several: itself when they are all equal, else a Variant."""
    unique = []
    for t in types:
        if t not in unique:
            unique.append(t)
    return unique[0] if len(unique) == 1 else Variant(unique)


def _describe_struct(expected, value, path):
    if not isinstance(value, dict):
        return [_Mismatch(path, "type", expected, infer_set(value))]
    if expected.instance(value):
        return []
    if not value or not all(isinstance(k, str) and k for k in value):
        # Not struct-shaped (empty, or a non-string key): Puppet compares
        # sizes, then reports the type mismatch against a plain Hash.
        required = sum(1 for e in expected.elements if not e.optional)
        total = len(expected.elements)
        if not required <= len(value) <= total:
            return [_size_mismatch(path, required, total, len(value))]
        hash_type = Hash(
            _common_type(infer_set(k) for k in value),
            _common_type(infer_set(v) for v in value.values()),
        )
        return [_Mismatch(path, "type", expected, hash_type)]
    keys = {e.key for e in expected.elements}
    descriptions = []
    for k in value:
        if k not in keys:
            descriptions.append(_Mismatch(path, "extra_key", key=k))
    for e in expected.elements:
        if e.key in value:
            descriptions.extend(
                _describe(e.value_type, value[e.key], path + [_entry(e.key)])
            )
        elif not e.optional:
            descriptions.append(_Mismatch(path, "missing_key", key=e.key))
    return descriptions


# ---------------------------------------------------------------- public


def assert_instance_of(subject, expected, value, nil_ok=False):
    """Puppet's ``TypeAsserter.assert_instance_of``: return ``value`` if it
    is an instance of ``expected`` (or ``None`` with ``nil_ok``), else raise
    :class:`hyera.HieraLookupError` with Puppet's mismatch text."""
    if value is None and nil_ok:
        return value
    mismatches = _describe(expected, value, [])
    if not mismatches:
        return value
    name = subject + " has wrong type,"
    if len(mismatches) == 1:
        raise HieraLookupError(_format_one(name, mismatches[0]).strip())
    text = "\n ".join(_format_one(name, m) for m in mismatches)
    raise HieraLookupError(text)
