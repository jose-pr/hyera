"""The Puppet type model: aliases, keys, equality, ranges and inference."""

import re

import pytest

from hyera import HieraLookupError, Sensitive
from hyera._types.inference import infer, infer_set
from hyera._types.parser import parse_type
from hyera._types.types import (
    Any,
    Enum,
    NotUndef,
    Optional,
    _eql_key,
    generalize,
)
from hyera._types.literal_format import _literal_str, _num_str
from hyera._types.compound_types import ALIASES, TypeReference

# --------------------------------------------------------- aliases/refs


def test_aliases_and_references():
    data_t = parse_type("Data")
    assert data_t.instance({"a": [1, None]}) is True
    assert data_t.instance({1: "a"}) is False

    rich_t = parse_type("RichData")
    from hyera import Sensitive

    assert rich_t.instance(Sensitive("x")) is True

    ref = parse_type("Stdlib::Port")
    assert isinstance(ref, TypeReference)
    assert str(ref) == "TypeReference['Stdlib::Port']"
    assert ref.instance("80") is False

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Iterable")
    assert "hyera does not support the Puppet type 'Iterable'" == str(exc_info.value)

    # infer_set of a RubySymbol-named object renders Runtime[ruby, 'Symbol'].
    # The real class is *defined* in _psych and re-exported through
    # backends -- matched by (name, __module__) against the former.
    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera.backends._psych"
    assert str(infer_set(RubySymbol())) == "Runtime[ruby, 'Symbol']"


# type-model internals: _key()/__eq__()/__hash__() have no entry point of their own,
# so they are exercised directly on parsed/inferred type instances.


def test_any_type_base_defaults():
    any_t = parse_type("Any")
    assert isinstance(any_t, Any) and type(any_t) is Any
    assert repr(any_t) == "Any"
    assert any_t._key() == ()
    assert hash(any_t) == hash(parse_type("Any"))


def test_type_key_equality_and_hash():
    pairs = [
        ("Integer[1,5]", "Integer[1,5]", "Integer[1,6]"),
        ("Float[1,5]", "Float[1,5]", "Float[1,6]"),
        ("String[1,3]", "String[1,3]", "String[1,4]"),
        ("Boolean[true]", "Boolean[true]", "Boolean[false]"),
        ("Regexp[/^a/]", "Regexp[/^a/]", "Regexp[/^b/]"),
        ("Pattern[/^a/]", "Pattern[/^a/]", "Pattern[/^b/]"),
        ("Enum[a,b]", "Enum[a,b]", "Enum[a,c]"),
        ("Collection[1,3]", "Collection[1,3]", "Collection[1,4]"),
        ("Array[Integer,1,3]", "Array[Integer,1,3]", "Array[String,1,3]"),
        ("Hash[String,Integer]", "Hash[String,Integer]", "Hash[String,String]"),
        ("Tuple[String,Integer]", "Tuple[String,Integer]", "Tuple[String,String]"),
        ("Struct[{a=>String}]", "Struct[{a=>String}]", "Struct[{a=>Integer}]"),
        ("Variant[String,Integer]", "Variant[String,Integer]", "Variant[String,Float]"),
        ("Sensitive[Integer]", "Sensitive[Integer]", "Sensitive[String]"),
        ("NotUndef[Integer]", "NotUndef[Integer]", "NotUndef[String]"),
        ("Optional[Integer]", "Optional[Integer]", "Optional[String]"),
        ("SemVer", "SemVer", "Binary"),
    ]
    for spec, same, different in pairs:
        a, b, c = parse_type(spec), parse_type(same), parse_type(different)
        assert a == b, spec
        assert hash(a) == hash(b), spec
        assert a != c, spec
        assert a != "not a type"

    ref_a = parse_type("Stdlib::Port")
    ref_b = TypeReference("Stdlib::Port")
    assert ref_a == ref_b
    assert hash(ref_a) == hash(ref_b)
    assert ref_a._key() == ("Stdlib::Port",)

    assert ALIASES["data"] == parse_type("Data")

    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera.backends._psych"
    rt_a, rt_b = infer(RubySymbol()), infer(RubySymbol())
    assert rt_a == rt_b
    assert hash(rt_a) == hash(rt_b)
    assert rt_a._key() == ("ruby", "Symbol")


def test_bare_and_literal_optional_instances():
    # Bare NotUndef (no contained type argument at all) accepts any non-Undef
    # instance; bare Optional is only Undef.
    assert parse_type("Optional").instance(5) is False
    assert parse_type("Optional").instance(None) is True
    assert parse_type("NotUndef").instance(5) is True

    # A bareword contained type argument (`Optional[integer]`) is kept as a
    # raw Python str, not parsed into a real type (`_literal_or_type`).
    literal_opt = parse_type("Optional[integer]")
    assert literal_opt.instance("integer") is True
    assert literal_opt.instance("other") is False
    assert literal_opt == parse_type("Optional[integer]")
    assert parse_type("Optional") == parse_type("Optional")


def test_optional_notundef_literal_container_rendering():
    # A bareword contained argument (`Optional[integer]`) keeps a raw str, rendered by
    # the plain-string branch; Optional/NotUndef around a String with `.literal` set
    # (only infer() builds one) renders its quoted literal instead.
    assert str(Optional(infer("x"))) == "Optional['x']"
    assert str(NotUndef(infer("y"))) == "NotUndef['y']"


def test_collection_pattern_regexp_enum_instance_and_render():
    assert parse_type("Regexp").instance(re.compile("^a")) is True
    assert parse_type("Regexp[/^a/]").instance(re.compile("^a")) is True
    assert parse_type("Regexp[/^a/]").instance(re.compile("^b")) is False
    assert parse_type("Regexp").instance("not a regex") is False

    assert parse_type("Pattern[/^a/]").instance("abc") is True
    assert parse_type("Pattern[/^a/]").instance("bbc") is False
    assert parse_type("Pattern[/^a/]").instance(5) is False

    assert parse_type("Enum[a,b]").instance("a") is True
    assert parse_type("Enum[a,b]").instance("c") is False
    assert parse_type("Enum[a,b]").instance(5) is False

    assert parse_type("Collection").instance([1, 2]) is True
    assert parse_type("Collection").instance("x") is False
    assert parse_type("Collection[1,2]").instance([1]) is True
    assert parse_type("Collection[1,2]").instance([]) is False
    assert parse_type("Collection[1,2]").instance([1, 2, 3]) is False
    assert str(parse_type("Collection[1,3]").generalize()) == "Collection"
    assert str(parse_type("Collection")) == "Collection"
    assert str(parse_type("Collection[1,3]")) == "Collection[1, 3]"


def test_numeric_and_string_range_bounds():
    assert parse_type("Float[1.0,5.0]").instance(6.0) is False
    assert parse_type("Float[1.0,5.0]").instance(3.0) is True

    literal = infer("a")
    assert literal.instance("a") is True
    assert literal.instance("b") is False

    assert parse_type("String[1,3]").instance("ab") is True

    assert (
        parse_type("Hash[String,Integer,1,2]").instance({"a": 1, "b": 2, "c": 3})
        is False
    )
    assert str(parse_type("Hash[String,Integer]").generalize()) == (
        "Hash[String, Integer]"
    )

    assert parse_type("Tuple[String,Integer,1,3]").instance(["a", 1, 2]) is True
    assert parse_type("Tuple[String]").instance("not a list") is False
    assert str(parse_type("Tuple[Integer[1,1]]").generalize()) == "Tuple[Integer]"

    assert parse_type("Struct[{a=>String}]").instance([1, 2]) is False

    # A range bound renders through `_num_str`, not the value's instance check: infer()
    # on a NaN/Infinity/large-exponent float gives `Float[<v>, <v>]`, which the mismatch
    # messages in `_types/mismatch.py` render.
    assert str(infer(float("nan"))) == "Float[nan, nan]"
    assert str(infer(float("inf"))) == "Float[inf, inf]"
    assert str(infer(float("-inf"))) == "Float[-inf, -inf]"
    # `repr(1e21)` has no "." in its mantissa ("1e+21"); `_num_str` inserts
    # one so the rendered bound still reads as a Puppet Float literal.
    assert str(infer(1e21)) == "Float[1.0e+21, 1.0e+21]"


def test_infer_edge_cases():
    with pytest.raises(TypeError, match="no Puppet type for"):
        infer(object())

    assert str(infer_set([])) == "Array[0, 0]"
    assert str(infer_set({})) == "Hash[0, 0]"

    assert str(infer([1, 1.0]).generalize()) == "Array[Variant[Integer, Float]]"
    assert str(infer(re.compile("^a"))) == "Regexp[/^a/]"
    assert str(infer([])) == "Array[0, 0]"

    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera.backends._psych"
    rt = infer(RubySymbol())
    assert rt.instance(RubySymbol()) is True
    assert rt.instance("x") is False

    # `generalize()`'s module-level dispatcher passes a non-Any value
    # (a bareword literal contained type, e.g. Optional[integer]'s "integer")
    # through unchanged, since it has no `.generalize()` of its own.
    assert generalize("integer") == "integer"


def test_num_str_and_literal_str_direct():
    # `_num_str`'s bool branch and `_literal_str`'s float/other branches cannot be
    # reached through parse_type(): the parser hands range bounds only number/default
    # nodes and Enum only string/bool nodes. Both are exercised directly.
    assert _num_str(True) == "true"
    assert _num_str(False) == "false"

    assert _literal_str(True) == "true"
    assert _literal_str(3.14) == "3.14"
    assert _literal_str(5) == "5"

    assert str(Enum([3.14, 5])) == "Enum[3.14, 5]"


def test_eql_key_undef_sensitive_and_identity_fallback():
    assert _eql_key(None) == ("undef", None)
    assert _eql_key(Sensitive(5)) == ("sensitive", ("int", 5))

    obj = object()
    assert _eql_key(obj) == ("id", id(obj))
    assert _eql_key(obj) != _eql_key(object())


def test_parse_type_is_cached():
    assert parse_type("Integer") is parse_type("Integer")


def test_alias_registry_matches_module():
    # Sanity: every alias this module exposes parses back to itself.
    for name in (
        "Data",
        "RichDataKey",
        "RichData",
        "Puppet::LookupKey",
        "Puppet::LookupValue",
    ):
        assert parse_type(name) is ALIASES[name.lower()]


def test_pattern_accepts_a_pattern_or_regexp_type_argument():
    assert str(parse_type("Pattern[Pattern[/a/]]")) == "Pattern[/a/]"
    assert str(parse_type("Pattern[Regexp[/a/], /b/]")) == "Pattern[/a/, /b/]"
