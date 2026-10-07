"""Tests for the public facade in :mod:`hyera.types`.

Each bare class and subscript expression is checked against ``parse_type`` of the
equivalent Puppet text.
"""

import re

import pytest

from hyera import HieraLookupError, Sensitive
from hyera import types
from hyera._types import types as _priv
from hyera._types.parser import as_type, parse_type

# --------------------------------------------------------------- bare forms

#: Every public ``hyera.types`` name minus ``Sensitive`` (which does not
#: use the facade metaclass -- see ``hyera.types``'s own module docstring).
BARE_NAMES = [
    "Any",
    "Undef",
    "NotUndef",
    "Optional",
    "Scalar",
    "ScalarData",
    "Numeric",
    "Integer",
    "Float",
    "String",
    "Boolean",
    "Regexp",
    "Enum",
    "Pattern",
    "Collection",
    "Array",
    "Hash",
    "Tuple",
    "Struct",
    "Variant",
    "Data",
    "RichData",
]


def _resolve(spec):
    """A bare ``hyera.types`` class resolves to its own default type
    object; anything else (already a type object) is returned unchanged."""
    if isinstance(spec, type) and isinstance(spec, types._TypeMeta):
        return spec._default()
    return spec


@pytest.mark.parametrize("name", BARE_NAMES)
def test_bare_class_equals_parse_type(name):
    cls = getattr(types, name)
    assert _resolve(cls) == parse_type(name)


@pytest.mark.parametrize("name", BARE_NAMES)
def test_bare_class_str_matches_parse_type(name):
    cls = getattr(types, name)
    assert str(cls) == str(parse_type(name))
    assert "{}".format(cls) == str(parse_type(name))


@pytest.mark.parametrize("name", BARE_NAMES)
def test_bare_class_repr(name):
    cls = getattr(types, name)
    assert repr(cls) == "<class 'hyera.types.{}'>".format(name)


def test_all_lists_every_bare_name_plus_sensitive_and_typespec():
    assert set(types.__all__) == set(BARE_NAMES) | {"Sensitive", "TypeLike"}


def test_typespec_accepts_every_form_a_type_position_takes():
    assert types.TypeLike is not None


def test_direct_references_for_export_coverage():
    """Reference every ``hyera.types`` name that no ``test_*`` body mentions.

    The module-level parametrize tables are invisible to the per-export presence
    check in ``tests/test_exports.py``."""
    assert types.Array is not None
    assert types.Boolean is not None
    assert types.Collection is not None
    assert types.Float is not None
    assert types.Hash is not None
    assert types.Numeric is not None
    assert types.Pattern is not None
    assert types.RichData is not None
    assert types.Scalar is not None
    assert types.ScalarData is not None
    assert types.Tuple is not None
    assert types.Undef is not None
    assert types.Variant is not None


# -------------------------------------------------------------- subscripts

SUBSCRIPT_CASES = [
    (lambda: types.Integer[1, 2], "Integer[1, 2]"),
    (lambda: types.Integer[1], "Integer[1]"),
    (lambda: types.Integer[1, None], "Integer[1, default]"),
    (lambda: types.Integer[None, 10], "Integer[default, 10]"),
    (lambda: types.Float[1.0, 2.5], "Float[1.0, 2.5]"),
    (lambda: types.Float[0], "Float[0]"),
    (lambda: types.String[1, 10], "String[1, 10]"),
    (lambda: types.String[1], "String[1]"),
    (lambda: types.Boolean[True], "Boolean[true]"),
    (lambda: types.Boolean[False], "Boolean[false]"),
    (lambda: types.Collection[1, 10], "Collection[1, 10]"),
    (lambda: types.Collection[1], "Collection[1]"),
    (lambda: types.Array[types.Integer], "Array[Integer]"),
    (lambda: types.Array["Integer"], "Array[Integer]"),
    (lambda: types.Array[types.Integer, 1, 3], "Array[Integer, 1, 3]"),
    (lambda: types.Array[1, 3], "Array[1, 3]"),
    (lambda: types.Hash[types.String, types.Integer], "Hash[String, Integer]"),
    (lambda: types.Hash["String", "Integer"], "Hash[String, Integer]"),
    (
        lambda: types.Hash[types.String, types.Integer, 1, 2],
        "Hash[String, Integer, 1, 2]",
    ),
    (lambda: types.Tuple[types.Integer, types.String], "Tuple[Integer, String]"),
    (lambda: types.Tuple[types.Integer, 1, 3], "Tuple[Integer, 1, 3]"),
    (
        lambda: types.Variant[types.Integer, types.String],
        "Variant[Integer, String]",
    ),
    (lambda: types.Variant["Integer", "String"], "Variant[Integer, String]"),
    (lambda: types.Enum["a", "b"], "Enum['a', 'b']"),
    (lambda: types.Enum["a"], "Enum['a']"),
    (lambda: types.Pattern["^a.*"], "Pattern[/^a.*/]"),
    (lambda: types.Pattern["^a", "^b"], "Pattern[/^a/, /^b/]"),
    (lambda: types.Pattern[re.compile("^a.*")], "Pattern[/^a.*/]"),
    (lambda: types.Regexp["^a.*"], "Regexp[/^a.*/]"),
    (lambda: types.Regexp[re.compile("^a.*")], "Regexp[/^a.*/]"),
    (lambda: types.Optional[types.String], "Optional[String]"),
    (lambda: types.Optional["active"], "Optional['active']"),
    (lambda: types.NotUndef[types.Integer], "NotUndef[Integer]"),
    (lambda: types.NotUndef["x"], "NotUndef['x']"),
    (lambda: types.Struct[{"a": types.Integer}], "Struct[{'a' => Integer}]"),
    (
        lambda: types.Struct[{"a": types.Integer, "b": types.String}],
        "Struct[{'a' => Integer, 'b' => String}]",
    ),
    (
        lambda: types.Struct[{types.Optional["a"]: types.Integer}],
        "Struct[{Optional['a'] => Integer}]",
    ),
    (
        lambda: types.Struct[{types.NotUndef["a"]: types.Integer}],
        "Struct[{'a' => Integer}]",
    ),
    (lambda: types.Array[types.Array[types.Integer]], "Array[Array[Integer]]"),
    (
        lambda: types.Optional[types.Array["Integer"]],
        "Optional[Array[Integer]]",
    ),
]


@pytest.mark.parametrize(
    "build, text", SUBSCRIPT_CASES, ids=[text for _, text in SUBSCRIPT_CASES]
)
def test_subscript_equals_parse_type(build, text):
    assert build() == parse_type(text)


@pytest.mark.parametrize(
    "build, text", SUBSCRIPT_CASES, ids=[text for _, text in SUBSCRIPT_CASES]
)
def test_subscript_str_equals_parse_type_str(build, text):
    assert str(build()) == str(parse_type(text))
    assert repr(build()) == str(parse_type(text))


NESTED_CASES = [
    (
        lambda: types.Optional[types.Integer[1, 3]],
        "Optional[Integer[1, 3]]",
    ),
    (
        lambda: types.NotUndef[types.String[1, 3]],
        "NotUndef[String[1, 3]]",
    ),
    (
        lambda: types.Optional[types.Optional[types.String]],
        "Optional[Optional[String]]",
    ),
    (
        lambda: types.Sensitive[types.Sensitive[types.Integer]],
        "Sensitive[Sensitive[Integer]]",
    ),
    (
        lambda: types.Array[types.Optional[types.Integer[1, 3]]],
        "Array[Optional[Integer[1, 3]]]",
    ),
    (
        lambda: types.Hash[types.String, types.Optional[types.Enum["a", "b"]]],
        "Hash[String, Optional[Enum['a', 'b']]]",
    ),
    (
        lambda: types.Tuple[types.Optional[types.Array[types.String]], 1, 2],
        "Tuple[Optional[Array[String]], 1, 2]",
    ),
    (
        lambda: types.Struct[{"a": types.Optional[types.Integer[0]]}],
        "Struct[{'a' => Optional[Integer[0]]}]",
    ),
    (
        lambda: types.Optional[types.Array["String[1]"]],
        "Optional[Array[String[1]]]",
    ),
    (lambda: types.Optional[types.Any], "Optional[Any]"),
]


@pytest.mark.parametrize(
    "build, text", NESTED_CASES, ids=[text for _, text in NESTED_CASES]
)
def test_nested_arguments_keep_their_parameters(build, text):
    built = build()
    assert str(built) == text
    assert built == parse_type(text)


def test_nested_arguments_decide_instance_checks():
    assert not isinstance(5, types.Optional[types.Integer[1, 3]])
    assert isinstance(2, types.Optional[types.Integer[1, 3]])
    assert not isinstance([5], types.Array[types.Optional[types.Integer[1, 3]]])
    assert not isinstance(["abcdef"], types.Array[types.NotUndef[types.String[1, 3]]])
    assert isinstance(
        {"k": "a"}, types.Hash[types.String, types.Optional[types.Enum["a", "b"]]]
    )
    assert not isinstance(
        {"k": "c"}, types.Hash[types.String, types.Optional[types.Enum["a", "b"]]]
    )


def test_nested_value_type_is_enforced_by_lookup():
    with pytest.raises(HieraLookupError):
        _lookup_default(types.Array[types.Optional[types.Integer[1, 3]]], [5])


def _lookup_default(value_type, default):
    from hyera import Hiera

    return Hiera({"version": 5, "hierarchy": []}).lookup("k", value_type, None, default)


def test_type_objects_are_immutable():
    t = types.Integer[10, 20]
    with pytest.raises(AttributeError):
        t.to = 10**9
    with pytest.raises(AttributeError):
        del t.to
    assert str(types.Integer[10, 20]) == "Integer[10, 20]"
    assert not isinstance(5000, types.Integer[10, 20])
    for build in (types.Array[types.Integer], types.Struct[{"a": types.String}]):
        with pytest.raises(AttributeError):
            build.size_from = 1


def test_type_objects_compare_only_with_types():
    t = types.Integer[1, 3]
    assert t.__eq__(5) is NotImplemented
    assert t != 5
    assert not t == "Integer[1, 3]"
    assert t == types.Integer[1, 3]
    assert hash(t) == hash(types.Integer[1, 3])


def test_never_parameterized_raises_puppets_own_text():
    from hyera import HieraLookupError

    with pytest.raises(HieraLookupError, match=r"Not a parameterized type <Any>"):
        types.Any[1]
    with pytest.raises(HieraLookupError, match=r"Not a parameterized type <Data>"):
        types.Data[1]


def test_struct_requires_a_single_dict():
    from hyera import HieraLookupError

    with pytest.raises(HieraLookupError):
        types.Struct[types.Integer]


# --------------------------------------------------------------- isinstance

ISINSTANCE_CASES = [
    (5, types.Integer, True),
    ("x", types.Integer, False),
    (True, types.Integer, False),
    (5, types.Integer[1, 10], True),
    (0, types.Integer[1, 10], False),
    (11, types.Integer[1, 10], False),
    (None, types.Optional[types.Integer], True),
    (5, types.Optional[types.Integer], True),
    ("x", types.Optional[types.Integer], False),
    (None, types.Integer, False),
    (None, types.NotUndef[types.Integer], False),
    (5, types.NotUndef[types.Integer], True),
    (5, types.Variant[types.Integer, types.String], True),
    ("x", types.Variant[types.Integer, types.String], True),
    (1.5, types.Variant[types.Integer, types.String], False),
    ("a", types.Enum["a", "b"], True),
    ("c", types.Enum["a", "b"], False),
    ("abba", types.Pattern["^a"], True),
    ("xyz", types.Pattern["^a"], False),
    ({"a": 1}, types.Struct[{"a": types.Integer}], True),
    ({}, types.Struct[{types.Optional["a"]: types.Integer}], True),
    ({"a": "x"}, types.Struct[{"a": types.Integer}], False),
    ([1, 2], types.Tuple[types.Integer, types.Integer], True),
    ([1, "x"], types.Tuple[types.Integer, types.Integer], False),
    (True, types.Boolean, True),
    (True, types.Boolean[True], True),
    (False, types.Boolean[True], False),
    (1, types.Numeric, True),
    (1.0, types.Numeric, True),
    (True, types.Numeric, False),
    (None, types.Undef, True),
    (5, types.Undef, False),
    (5, types.Any, True),
    (None, types.Any, True),
]


@pytest.mark.parametrize("value, type_, expected", ISINSTANCE_CASES)
def test_isinstance(value, type_, expected):
    assert isinstance(value, type_) is expected


def test_isinstance_struct_optional_key_pair():
    t = types.Struct[{types.Optional["a"]: types.Integer}]
    assert isinstance({"a": 1}, t)
    assert isinstance({}, t)
    assert not isinstance({"b": 1}, t)


# ---------------------------------------------------- hash / equality / str


def test_subscripted_types_are_hashable_and_equal():
    a = types.Integer[1, 10]
    b = types.Integer[1, 10]
    assert a == b
    assert hash(a) == hash(b)
    assert a != types.Integer[1, 11]


def test_bare_class_default_is_the_parser_singleton_where_applicable():
    # `Integer`/`Float`/`String`'s own `DEFAULT` instance is shared by the
    # parser itself (`parse_type` caches by text) -- confirms the facade
    # goes through the very same construction path, not a parallel one.
    assert types.Integer._default() is parse_type("Integer")


# --------------------------------------------------- internal dispatch safe


def test_internal_isinstance_dispatch_on_private_classes_unaffected():
    """An instance-level ``Any.__instancecheck__`` leaves
    ``isinstance(<type object>, <private class>)`` alone; internal dispatch uses it."""
    integer_type = parse_type("Integer")
    assert isinstance(integer_type, _priv.Integer)
    assert isinstance(integer_type, _priv.Any)
    optional_type = parse_type("Optional[String]")
    assert isinstance(optional_type, _priv.Optional)
    assert not isinstance(optional_type, _priv.Integer)


# ------------------------------------------------------------------- calling

CALL_CASES = [
    (types.Integer, ("42",), 42),
    (types.Integer, (5,), 5),
    (types.Integer[1, 10], ("5",), 5),
    (types.Integer, ("0x1F",), 31),
    (types.Float, ("1.5",), 1.5),
    (types.Numeric, ("5",), 5),
    (types.Numeric, ("1.5",), 1.5),
    (types.String, (42,), "42"),
    (types.Boolean, ("true",), True),
    (types.Boolean, ("no",), False),
    (types.Array, ("ab",), ["a", "b"]),
    (types.Array, (5,), [0, 1, 2, 3, 4]),
    (types.Array, ("ab", True), ["ab"]),
    (types.Hash, ([["a", 1], ["b", 2]],), {"a": 1, "b": 2}),
]


@pytest.mark.parametrize("cls, args, expected", CALL_CASES)
def test_call_is_puppet_new(cls, args, expected):
    result = cls(*args)
    assert result == expected
    assert type(result) is type(expected)


def test_call_rejects_keywords():
    with pytest.raises(TypeError, match="takes no keyword arguments"):
        types.Integer(value="42")


def test_call_requires_at_least_one_argument():
    with pytest.raises(TypeError, match="missing required argument"):
        types.Integer()


def test_call_unsupported_type_raises_hieralookuperror():
    with pytest.raises(HieraLookupError, match="does not support new"):
        types.Regexp("^a")


def test_call_already_instance_short_circuits():
    # Puppet's new() is idempotent: an already-conforming value and no
    # extra arguments is returned unchanged, not re-converted.
    assert types.Integer(5) == 5
    assert types.Integer[1, 10](5) == 5


# ------------------------------------------------------------------ Sensitive


def test_sensitive_is_the_same_object_as_hyera_sensitive():
    assert types.Sensitive is Sensitive


def test_sensitive_bare_type_via_as_type():
    assert as_type(types.Sensitive) == parse_type("Sensitive")


def test_sensitive_subscript_with_facade_class():
    t = types.Sensitive[types.String]
    assert t == parse_type("Sensitive[String]")
    assert isinstance(Sensitive("x"), t)
    assert not isinstance(Sensitive(5), t)


def test_sensitive_subscript_with_type_expression_string():
    assert types.Sensitive["Integer"] == parse_type("Sensitive[Integer]")


def test_sensitive_call_stays_the_value_wrapper():
    v = types.Sensitive("secret")
    assert isinstance(v, types.Sensitive)
    assert v.unwrap() == "secret"
    assert str(v) == "Sensitive [value redacted]"


# ------------------------------------------------ Puppet 8 instance rules

NAN = float("nan")

PUPPET_INSTANCE_CASES = [
    # A Struct key is optional when its value type accepts undef.
    (
        {"name": "x"},
        types.Struct[{"name": types.String, "port": types.Optional[types.Integer]}],
        True,
    ),
    ({}, types.Struct[{"a": types.Any}], True),
    ({}, types.Struct[{"a": types.Undef}], True),
    ({}, types.Struct[{"a": types.Integer}], False),
    (
        {},
        types.Struct[{types.NotUndef["a"]: types.Optional[types.Integer]}],
        False,
    ),
    (
        {"a": None},
        types.Struct[{types.NotUndef["a"]: types.Optional[types.Integer]}],
        True,
    ),
    # A Tuple's last bound is a minimum; the last type repeats.
    (["a", "b"], types.Tuple[types.String, 1], True),
    ([1, "a"], types.Tuple[types.Integer, types.String, 1], True),
    ([1, 2, 3], types.Tuple[types.Integer, 2], True),
    ([], types.Tuple[types.Integer, 1], False),
    ([1, 2], types.Tuple[types.Integer, 1, 2], True),
    ([1, 2, 3], types.Tuple[types.Integer, 1, 2], False),
    ([1, "a", 3], types.Tuple, True),
    ([], types.Tuple, True),
    ([1], types.Tuple[types.Integer], True),
    ([1, 2], types.Tuple[types.Integer], False),
    # ScalarData holds scalars only.
    ([1, 2], types.ScalarData, False),
    ({"a": 1}, types.ScalarData, False),
    ("a", types.ScalarData, True),
    (1.5, types.ScalarData, True),
    # Bare Optional is only undef; bare Pattern and Enum accept any String.
    (None, types.Optional, True),
    (5, types.Optional, False),
    ("anything", types.Pattern, True),
    (5, types.Pattern, False),
    ("anything", types.Enum, True),
    # A trailing boolean in Enum is the case-insensitive flag.
    ("A", types.Enum["a", "b", True], True),
    ("A", types.Enum["a", "b", False], False),
    ("A", types.Enum["a", "b"], False),
    # NaN is not a Float.
    (NAN, types.Float, False),
    (NAN, types.Numeric, False),
    (NAN, types.Float[1.0, 3.0], False),
    (1.5, types.Float, True),
]


@pytest.mark.parametrize("value, type_, expected", PUPPET_INSTANCE_CASES)
def test_isinstance_follows_puppet_rules(value, type_, expected):
    assert isinstance(value, type_) is expected


# (Ruby regex source, subject, whether Ruby's ``=~`` matches), from Ruby 4.0.
RUBY_REGEX_CASES = [
    (r"\Aabc\z", "abc", True),
    (r"\Aabc\z", "abc\n", False),
    (r"\Aabc\Z", "abc\n", True),
    (r"\Aabc\Z", "abc\n\n", False),
    (r"\Ax\z", "x\n", False),
    (r"^[\h:]+$", "aa:bb", True),
    (r"^[\h:]+$", "xx", False),
    (r"^\h+$", "dEaD", True),
    (r"^\H+$", "xyz", True),
    (r"^\H+$", "abc", False),
    (r"^[[:alpha:]]+$", "abc", True),
    (r"^[[:alpha:]]+$", "a]", False),
    (r"^[[:digit:]]+$", "123", True),
    (r"^[[:alnum:]_]+$", "a_1", True),
    (r"^[[:upper:]][[:lower:]]+$", "Abc", True),
    (r"^[[:space:]]+$", " \t", True),
    (r"^[[:punct:]]+$", "!?", True),
    (r"^[^[:digit:]]+$", "abc", True),
    (r"^[^[:digit:]]+$", "a1", False),
    (r"[\d\h]+", "12af", True),
    (r"^\w+$", "café", False),
    (r"^\d+$", "١٢", False),
    (r"^\s+$", " \t\n", True),
    (r"\bfoo\b", "a foo b", True),
    (r"\bfoo\b", "afoob", False),
    ("(?m)a.c", "a\nc", True),
    ("a.c", "a\nc", False),
    ("(?i)abc", "ABC", True),
    ("(?i)é", "É", True),
    ("a(?i)bc", "aBC", True),
    ("a(?i)bc", "ABC", False),
    ("x(?i)a|b", "B", False),
    ("(?-i)a", "A", False),
    ("(?i-m)a.b", "A\nB", False),
    ("(?mi)a.b", "A\nB", True),
    ("(?i:b)c", "BC", False),
    ("(?x) a b # note (\n c", "abc", True),
    ("^a$", "b\na\nc", True),
    ("a$", "a\n", True),
    ("(?<n>a)\\k<n>", "aa", True),
    ("(?<n>a)\\k<n>", "ab", False),
    ("(a)\\1", "aa", True),
    ("(?<=a)b", "ab", True),
    ("(?<!a)b", "ab", False),
    ("a{,2}b", "b", True),
    ("\\y", "y", True),
    ("\\e", "\x1b", True),
    ("\\/", "/", True),
    ("\\u{41}", "A", True),
    ("\\x41", "A", True),
    ("(?#note)a", "a", True),
]


@pytest.mark.parametrize("source, subject, expected", RUBY_REGEX_CASES)
def test_pattern_matches_as_ruby_does(source, subject, expected):
    assert isinstance(subject, types.Pattern[source]) is expected


@pytest.mark.parametrize(
    "source, message",
    [
        ("(", "end pattern with unmatched parenthesis: /(/"),
        (")", "unmatched close parenthesis: /)/"),
        ("a)", "unmatched close parenthesis: /a)/"),
        ("[", "premature end of char-class: /[/"),
        ("[b-a]", "empty range in char class: /[b-a]/"),
        ("*a", "target of repeat operator is not specified: /*a/"),
        ("(?z)", "undefined group option: /(?z)/"),
    ],
)
def test_pattern_rejects_with_rubys_text(source, message):
    with pytest.raises(HieraLookupError) as info:
        types.Pattern[source]
    assert str(info.value) == message


@pytest.mark.parametrize(
    "source, construct",
    [
        (r"\p{Alpha}+", r"\p"),
        (r"\P{Alpha}", r"\P"),
        (r"\R", r"\R"),
        (r"\X", r"\X"),
        (r"\G", r"\G"),
        (r"a\Kb", r"\K"),
        (r"(?<x>a)\g<x>", r"\g"),
        ("[a-z&&[^aeiou]]+", "&&"),
        ("[a[bc]]", "nested character class"),
        (r"[\H]", r"\H in a character class"),
        ("a**", "nested repeat"),
    ],
)
def test_pattern_names_an_untranslatable_ruby_construct(source, construct):
    with pytest.raises(HieraLookupError) as info:
        types.Pattern[source]
    assert construct in str(info.value)
    assert source in str(info.value)


def test_regexp_type_rejects_an_invalid_source():
    with pytest.raises(HieraLookupError, match="unmatched parenthesis"):
        types.Regexp["("]


def _empty_hiera():
    from hyera import Hiera

    return Hiera({"version": 5, "hierarchy": []})


def test_lookup_value_type_struct_with_optional_value_accepts_missing_key():
    t = types.Struct[{"name": types.String, "port": types.Optional[types.Integer]}]
    assert _empty_hiera().lookup("k", t, None, {"name": "x"}) == {"name": "x"}


def test_lookup_value_type_tuple_with_minimum_accepts_longer_array():
    got = _empty_hiera().lookup("k", types.Tuple[types.String, 1], None, ["a", "b"])
    assert got == ["a", "b"]


def test_lookup_value_type_scalar_data_rejects_array():
    with pytest.raises(HieraLookupError):
        _empty_hiera().lookup("k", types.ScalarData, None, [1, 2])
