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
from .inference import infer_set
from .types import (
    ANY,
    UNDEF,
    Enum,
    Float,
    Integer,
    NotUndef,
    Optional,
    Pattern,
    SensitiveType,
    String,
    _type_instance,
)
from .literal_format import puppet_quote
from .compound_types import (
    Array,
    Collection,
    Hash,
    Struct,
    Tuple,
    TypeAlias,
    TypeReference,
    Variant,
)
from .variant_merge import variant_of

__all__ = ["assert_instance_of"]

#: Types whose formatter/short_name keeps one bare level of their contained type's name
#: (``type_mismatch_describer.rb`` ``short_name``, ``type_formatter.rb``'s ``string_P*``
#: methods for these three; ``Type``, the fourth in Puppet, is unparameterized here).
_WRAPPER_TYPES = (Optional, NotUndef, SensitiveType)

_STRINGS = (String, Enum, Pattern)
#: The actual types each expected type's unparameterized form accepts
#: (``assignable_to_default?``): the default of Array takes a Tuple, of Hash a Struct.
_DEFAULT_TAKES = {
    String: _STRINGS,
    Enum: _STRINGS,
    Pattern: _STRINGS,
    Array: (Array, Tuple),
    Hash: (Hash, Struct),
    Collection: (Collection, Array, Tuple, Hash, Struct),
}


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


def _always_detailed(e, a):
    """``always_fully_detailed?``: the actual is the expected's own class (or
    a Tuple/Array, Struct/Hash pair), or either side is an alias."""
    if isinstance(e, str):
        return False
    return (
        type(e) is type(a)
        or isinstance(e, TypeAlias)
        or isinstance(a, TypeAlias)
        or (isinstance(e, Struct) and isinstance(a, Hash))
        or (isinstance(e, Tuple) and isinstance(a, Array))
    )


def _default_takes(e, a):
    """``assignable_to_default?``: the unparameterized form of ``e`` accepts ``a``."""
    if isinstance(e, str):
        return False
    takes = _DEFAULT_TAKES.get(type(e))
    if takes is None:
        takes = (Float, Integer) if type(e).TYPE_NAME == "Numeric" else (type(e),)
    return isinstance(a, takes)


def _report_detailed(types, a):
    return any(_always_detailed(t, a) or _default_takes(t, a) for t in types)


def _resolved(t):
    """``all_resolved``: an alias as the type it stands for."""
    while isinstance(t, TypeAlias):
        t = t.resolved_type
    return t


def _actual_text(types, a):
    """``detailed_actual_to_s``: the actual in full when an expected type is its own
    class or an unparameterized type that accepts it, else its bare name."""
    types = [_resolved(t) for t in types]
    if any(_always_detailed(t, a) for t in types):
        return str(a)
    if any(
        _default_takes(t, a) and not isinstance(t, str) and str(t) == t.name
        for t in types
    ):
        return str(a)
    return short_name(a)


class _VariantElement(str):
    """A ``variant N`` path element: left out of a mismatch's canonical path."""

    __slots__ = ()


def _canonical(path):
    return tuple(p for p in path if not isinstance(p, _VariantElement))


class _Mismatch:
    """One mismatch (Puppet's ``Mismatch`` family). ``kind`` is ``type``,
    ``pattern`` (both Puppet's TypeMismatch), ``size`` (``expected`` is a
    ``(from, to)`` pair and ``actual`` its text), ``missing_key``, ``extra_key``
    or ``unresolved``."""

    __slots__ = ("path", "kind", "expected", "actual", "key", "ref")

    def __init__(self, path, kind, expected=None, actual=None, key=None, ref=None):
        self.path = path
        self.kind = kind
        self.expected = expected
        self.actual = actual
        self.key = key
        self.ref = ref

    def same(self, other):
        """Puppet's ``Mismatch#==``: the kind, the path without its variant
        elements, and what that kind compares."""
        if self.kind != other.kind or _canonical(self.path) != _canonical(other.path):
            return False
        if self.kind in ("missing_key", "extra_key"):
            return self.key == other.key
        if self.kind == "unresolved":
            return self.ref == other.ref
        return self.expected == other.expected and self.actual == other.actual

    def merged(self, other):
        """Puppet's ``Mismatch#merge``: the least restrictive of the two."""
        if self.kind == "size":
            lo = min(self.expected[0], other.expected[0])
            highs = (self.expected[1], other.expected[1])
            hi = None if None in highs else max(highs)
            return _Mismatch(self.path, "size", (lo, hi), self.actual)
        expected = variant_of([self.expected, other.expected])
        return _Mismatch(self.path, self.kind, expected, self.actual)

    def chopped(self, index):
        """Puppet's ``chop_path``: a copy without the path element at ``index``."""
        if index >= len(self.path):
            return self
        path = self.path[:index] + self.path[index + 1 :]
        return _Mismatch(
            path, self.kind, self.expected, self.actual, self.key, self.ref
        )


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
            name, pos, _size_text(*m.expected), m.actual
        )
    expected = m.expected
    optional = isinstance(expected, Optional) and expected.contained is not None
    if optional:
        expected = expected.contained
    if m.kind == "pattern":
        prefix = "an undef value or a match" if optional else "a match"
        return "{}{} expects {} for {}, got {}".format(
            name, pos, prefix, expected, _actual_literal(m.actual)
        )
    # 'type'
    actual = m.actual
    if isinstance(expected, Variant) and expected.types:
        types = list(expected.types)
        if _report_detailed(types, actual):
            parts = [str(t) for t in types]
            actual_text = _actual_text(types, actual)
        else:
            parts = _unique([short_name(t) for t in types])
            actual_text = short_name(actual)
        if optional:
            parts.insert(0, "Undef")
        if len(parts) > 1:
            return "{}{} expects a value of type {}, got {}".format(
                name, pos, _join_or(parts), actual_text
            )
        expected_text = parts[0]
    else:
        if _report_detailed([expected], actual):
            expected_text = str(expected)
            actual_text = _actual_text([expected], actual)
        else:
            expected_text = short_name(expected)
            actual_text = short_name(actual)
        if optional:
            return "{}{} expects a value of type Undef or {}, got {}".format(
                name, pos, expected_text, actual_text
            )
    return "{}{} expects {} {} value, got {}".format(
        name, pos, _a_an(expected_text), expected_text, actual_text
    )


def _unique(parts):
    uniq = []
    for p in parts:
        if p not in uniq:
            uniq.append(p)
    return uniq


def _join_or(parts):
    if len(parts) == 2:
        return "{} or {}".format(parts[0], parts[1])
    return "{}, or {}".format(", ".join(parts[:-1]), parts[-1])


def _actual_literal(actual_type):
    """A ``PatternMismatch``'s "got" side is the value itself, quoted --
    never the type name (``string_PStringType`` never shows a value's own
    literal, but Puppet's ``PatternMismatch#message`` uses ``actual.value``
    directly)."""
    if isinstance(actual_type, String) and actual_type.literal is not None:
        return puppet_quote(actual_type.literal)
    return short_name(actual_type)


# ------------------------------------------------------------- describe


def _describe(expected, value, path, original=None):
    """Every reason ``value`` is not an instance of ``expected``, as a list
    of :class:`_Mismatch` (empty when it IS an instance). ``original`` is the
    type a whole-value mismatch names: the wrapping Optional or alias."""
    if original is None:
        original = expected
    if isinstance(expected, str):
        if value == expected:
            return []
        return [_Mismatch(path, "type", original, infer_set(value))]

    if isinstance(expected, Optional):
        if value is None:
            return []
        if expected.contained is None:
            return [_Mismatch(path, "type", expected, infer_set(value))]
        wrapper = original if isinstance(original, TypeAlias) else expected
        return _describe(expected.contained, value, path, wrapper)

    if isinstance(expected, NotUndef):
        if value is None:
            return [_Mismatch(path, "type", expected, infer_set(value))]
        if expected.contained is None:
            return []
        # NotUndef[T] on a non-None value describes exactly as T does (its
        # own instance set already excludes Undef): see AGENTS.md Gotchas.
        return _describe(expected.contained, value, path)

    if isinstance(expected, Variant):
        return _describe_variant(expected, value, path, original)

    if isinstance(expected, Array):
        return _describe_array(expected, value, path, original)

    if isinstance(expected, Tuple):
        return _describe_tuple(expected, value, path, original)

    if isinstance(expected, Hash):
        return _describe_hash(expected, value, path, original)

    if isinstance(expected, Struct):
        return _describe_struct(expected, value, path, original)

    if isinstance(expected, (Enum, Pattern)):
        if expected.instance(value):
            return []
        return [_Mismatch(path, "pattern", original, infer_set(value))]

    if isinstance(expected, TypeReference):
        return [_Mismatch(path, "unresolved", ref=expected.text)]

    if isinstance(expected, TypeAlias):
        if expected.instance(value):
            return []
        resolved = expected.resolved_type
        if isinstance(resolved, Variant):
            resolved = variant_of(resolved.types)
        return _describe(resolved, value, path, expected)

    if _type_instance(expected, value):
        return []
    return [_Mismatch(path, "type", original, infer_set(value))]


def _describe_variant(expected, value, path, original):
    if not expected.types:
        return [_Mismatch(path, "type", original, infer_set(value))]
    types = list(expected.types)
    if isinstance(original, Optional):
        types.insert(0, UNDEF)
    per_branch = []
    for i, t in enumerate(types):
        found = _describe(t, value, path + [_VariantElement("variant {}".format(i))])
        if not found:
            return []
        per_branch.append(found)
    descriptions = _merge_descriptions(len(path), per_branch)
    if isinstance(original, TypeAlias) and len(descriptions) == 1:
        # Every branch of an aliased Variant failed: one mismatch on the alias.
        return [_Mismatch(path, "type", original, infer_set(value))]
    return descriptions


def _merge_descriptions(position, per_branch):
    """Puppet's ``merge_descriptions``: when every branch fails with one mismatch of
    the same kind at the same path, report their least restrictive merge."""
    descriptions = [d for found in per_branch for d in found]
    for kinds in (("size",), ("type", "pattern")):
        mismatches = [d for d in descriptions if d.kind in kinds]
        if len(mismatches) != len(per_branch):
            continue
        generic = mismatches[0]
        for other in mismatches[1:]:
            if _canonical(generic.path) != _canonical(other.path):
                generic = None
                break
            generic = generic.merged(other)
        if generic is not None:
            descriptions = [generic]
            break
    unique = []
    for d in descriptions:
        if not any(d.same(u) for u in unique):
            unique.append(d)
    return [unique[0].chopped(position)] if len(unique) == 1 else unique


def _size_mismatch(path, from_, to_, actual_n):
    return _Mismatch(path, "size", expected=(from_ or 0, to_), actual=str(actual_n))


def _size_text(from_, to_):
    low = from_ or 0
    if to_ is None:
        return "unlimited" if low == 0 else "at least {}".format(low)
    if low == to_:
        return str(low)
    if low == 0:
        return "at most {}".format(to_)
    return "between {} and {}".format(low, to_)


def _describe_array(expected, value, path, original):
    if not isinstance(value, (list, tuple)):
        return [_Mismatch(path, "type", original, infer_set(value))]
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


def _describe_tuple(expected, value, path, original):
    if not isinstance(value, (list, tuple)):
        return [_Mismatch(path, "type", original, infer_set(value))]
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


def _struct_shaped(value):
    """Whether Puppet infers a Struct for ``value``: non-empty, every key a
    non-empty String."""
    return bool(value) and all(isinstance(k, str) and k for k in value)


def _hash_type_of(value):
    """The Hash type Puppet infers for a dict that is not struct-shaped."""
    return Hash(
        variant_of([infer_set(k) for k in value]),
        variant_of([infer_set(v) for v in value.values()]),
    )


def _describe_hash(expected, value, path, original):
    if not isinstance(value, dict):
        return [_Mismatch(path, "type", original, infer_set(value))]
    n = len(value)
    lo = expected.size_from if expected.size_from is not None else 0
    hi = expected.size_to
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        return [_size_mismatch(path, expected.size_from, expected.size_to, n)]
    if value and not _struct_shaped(value):
        if expected.instance(value):
            return []
        return [_Mismatch(path, "type", original, _hash_type_of(value))]
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


def _describe_struct(expected, value, path, original):
    if not isinstance(value, dict):
        return [_Mismatch(path, "type", original, infer_set(value))]
    if expected.instance(value):
        return []
    if not _struct_shaped(value):
        # Not struct-shaped (empty, or a non-string key): Puppet compares
        # sizes, then reports the type mismatch against a plain Hash.
        required = sum(1 for e in expected.elements if not e.optional)
        total = len(expected.elements)
        if not required <= len(value) <= total:
            return [_size_mismatch(path, required, total, len(value))]
        return [_Mismatch(path, "type", original, _hash_type_of(value))]
    keys = {e.key for e in expected.elements}
    descriptions = []
    for e in expected.elements:
        if e.key in value:
            descriptions.extend(
                _describe(e.value_type, value[e.key], path + [_entry(e.key)])
            )
        elif not e.optional:
            descriptions.append(_Mismatch(path, "missing_key", key=e.key))
    for k in value:
        if k not in keys:
            descriptions.append(_Mismatch(path, "extra_key", key=k))
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
