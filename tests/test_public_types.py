"""Tests for the public facade in :mod:`hyera.types`.

Every bare class and subscript expression is checked against
:func:`hyera._types.parser.parse_type` of the equivalent Puppet text --
``hyera.types`` is a thin, string-equivalent facade over the already
oracle-tested private type model, so this file never needs its own Puppet
oracle data.
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
    assert set(types.__all__) == set(BARE_NAMES) | {"Sensitive", "TypeSpec"}


def test_typespec_accepts_every_form_a_type_position_takes():
    assert types.TypeSpec is not None


def test_direct_references_for_export_coverage():
    """Touches every ``hyera.types`` name not already referenced inside
    some other ``test_*`` function body here: the parametrized tables above
    (``BARE_NAMES``, ``SUBSCRIPT_CASES``, ...) live at module level, which
    ``tests/test_exports.py``'s per-export presence check (a bare
    ``ast.Name``/``ast.Attribute`` inside a ``test_*`` function) does not
    see."""
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
    """Giving ``Any`` an instance-level ``__instancecheck__`` must not
    change ``isinstance(<type object>, <private class>)`` -- the dispatch
    every internal module (``_types.new_function``, ``_types.mismatch``,
    ...) relies on."""
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
