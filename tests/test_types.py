"""Golden-driven tests for the Puppet type model and parser.

Reads ``tests/conformance/cases/convert-type-parse`` and
``.../convert-value-type`` directly (Puppet-recorded ground truth), never
through the lookup API -- ``value_type``/``--type`` wiring is not tested
here.
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from hyera import HieraLookupError, Sensitive
from hyera._lookup.lookup_adapter import convert_result
from hyera._types.new_function import new_instance
from hyera._types.string_converter import convert as _string_convert
from hyera._types.string_converter import puppet_quote as _puppet_quote
from hyera._types.mismatch import (
    _a_an,
    _size_text,
    assert_instance_of,
)
from hyera._types.parser import _Parser, parse_type
from hyera._types.types import (
    ALIASES,
    Any,
    Enum,
    NotUndef,
    Optional,
    TypeReference,
    _eql_key,
    _literal_str,
    _num_str,
    generalize,
    infer,
    infer_set,
)

_CASES = Path(__file__).parent / "conformance" / "cases"


def _golden(case):
    return json.loads((_CASES / case / "golden.json").read_text(encoding="utf-8"))


def _data(case):
    return yaml.safe_load(
        (_CASES / case / "data" / "common.yaml").read_text(encoding="utf-8")
    )


# ------------------------------------------------------------- type-parse


def _type_parse_params():
    golden = _golden("convert-type-parse")["results"]
    data = _data("convert-type-parse")
    specs = data["lookup_options"]
    for key in sorted(golden, key=lambda s: int(s[1:])):
        yield pytest.param(key, specs[key]["convert_to"], golden[key], id=key)


_CONVERTED_FROM_RE = re.compile(r"Converted value from (.*)\.new\(\)")
_NOT_SUPPORTED_RE = re.compile(r"instance of type '(.*)' is not supported")


@pytest.mark.parametrize("key,spec,result", list(_type_parse_params()))
def test_parse_matches_golden(key, spec, result):
    message = result.get("message", "")
    if message.startswith(
        "Invalid data type in lookup_options for key '{}' could not parse".format(key)
    ):
        inner = message.split("error: '", 1)[1]
        with pytest.raises(HieraLookupError) as exc_info:
            parse_type(spec)
        assert str(exc_info.value) == inner
        return

    t = parse_type(spec)
    m = _CONVERTED_FROM_RE.search(message) or _NOT_SUPPORTED_RE.search(message)
    if m:
        assert str(t) == m.group(1)


# ------------------------------------------------------------ value-type


def _value_type_params():
    case = yaml.safe_load(
        (_CASES / "convert-value-type" / "case.yaml").read_text(encoding="utf-8")
    )
    golden = _golden("convert-value-type")["results"]
    data = _data("convert-value-type")
    for q in case["queries"]:
        qid = q["id"]
        yield pytest.param(
            qid, q["key"], q["type"], data[q["key"]], golden[qid], id=qid
        )


@pytest.mark.parametrize("qid,key,type_text,value,result", list(_value_type_params()))
def test_instance_matches_golden(qid, key, type_text, value, result):
    if result["status"] == "found":
        assert parse_type(type_text).instance(value) is True
        return
    message = result["message"]
    if message.strip().startswith("Found value has wrong type"):
        assert parse_type(type_text).instance(value) is False
        return
    with pytest.raises(HieraLookupError) as exc_info:
        parse_type(type_text)
    assert str(exc_info.value) == message


# ------------------------------------------------------------- rendering


def test_render_table():
    cases = [
        ("Float[1, 2]", "Float[1.0, 2.0]"),
        ("Enum[a,b]", "Enum['a', 'b']"),
        ("Sensitive[Integer[0,5]]", "Sensitive[Integer]"),
        ("Optional[integer]", "Optional['integer']"),
        ("Struct[{Optional[c] => String}]", "Struct[{Optional['c'] => String}]"),
        ("Integer[default,10]", "Integer[default, 10]"),
        ("Pattern['^a']", "Pattern[/^a/]"),
        (" Integer", "Integer"),
        ("::Integer", "Integer"),
        ("Array[String, 1, default]", "Array[String, 1]"),
        ("Variant[String[1,2], Integer]", "Variant[String[1, 2], Integer]"),
    ]
    for spec, expected in cases:
        assert str(parse_type(spec)) == expected, spec

    assert str(infer_set(11)) == "Integer[11, 11]"
    assert str(infer([1]).generalize()) == "Array[Integer]"
    assert str(infer([True]).generalize()) == "Array[Boolean]"


# --------------------------------------------------- parser grammar/errors
#
# The golden-driven `test_parse_matches_golden` above only exercises specs
# Puppet itself was asked to parse; these cover the parser's own remaining
# grammar shapes and error branches directly (array/hash literals, quoted
# string escapes, unary minus, `default` size bounds, and every builder's
# own argument-count/shape error).


def test_parser_syntax_errors():
    error_cases = [
        ("@bad", "Syntax error at '@' (line: 1, column: 1)"),
        ("Array[", "Syntax error at end of input"),
        ("Hash[]", "Syntax error at ']' (line: 1, column: 6)"),
        ("Collection[", "Syntax error at end of input"),
        ("Tuple[]", "Syntax error at ']' (line: 1, column: 7)"),
        ("Variant[]", "Syntax error at ']' (line: 1, column: 9)"),
        ("Enum[]", "Syntax error at ']' (line: 1, column: 6)"),
        ("Pattern[]", "Syntax error at ']' (line: 1, column: 9)"),
        ("Optional[]", "Syntax error at ']' (line: 1, column: 10)"),
        ("NotUndef[]", "Syntax error at ']' (line: 1, column: 10)"),
        ("Float[]", "Syntax error at ']' (line: 1, column: 7)"),
        ("String[]", "Syntax error at ']' (line: 1, column: 8)"),
        ("Boolean[]", "Syntax error at ']' (line: 1, column: 9)"),
        ("Sensitive[]", "Syntax error at ']' (line: 1, column: 11)"),
        ("-x", "Syntax error at 'x' (line: 1, column: 2)"),
        ("Integer,", "Syntax error at end of input"),
        ("Integer]", "Syntax error at ']' (line: 1, column: 8)"),
        ("Pattern['^a'b']", "Syntax error at ''' (line: 1, column: 14)"),
    ]
    for spec, message in error_cases:
        with pytest.raises(HieraLookupError) as exc_info:
            parse_type(spec)
        assert str(exc_info.value) == message, spec


def test_parser_not_a_type_spec_errors():
    for spec in (
        "-5",
        "Integer [1]",
        "Struct[String]",
        # "undef" is a valid primary expression (a literal value), just
        # never a valid *type* expression.
        "Optional[undef]",
        # A non-type first argument with no second one.
        "Array[0]",
        "Array[default]",
        # Array's own three-size-argument form (no element type given at
        # all, just size args directly) is still capped at two.
        "Array[1,2,3]",
        # An "access" key whose base name IS "optional" but whose own
        # argument isn't a bareword/quoted string.
        "Struct[{Optional[Integer]=>String}]",
    ):
        with pytest.raises(HieraLookupError) as exc_info:
            parse_type(spec)
        assert str(exc_info.value) == (
            "The expression <{}> is not a valid type specification.".format(spec)
        )


def test_parser_builder_arg_count_errors():
    cases = [
        (
            "Array[Integer,1,2,3]",
            "Invalid number of type parameters specified: Array requires 1 to 3, 4 provided",
        ),
        (
            "Hash[String=>Integer]",
            "Invalid number of type parameters specified: Hash requires 2 to 4, 1 provided",
        ),
        (
            "Sensitive[Integer,String]",
            "Invalid number of type parameters specified: Sensitive requires 0 to 1, 2 provided",
        ),
        (
            "Optional[Integer,String]",
            "Invalid number of type parameters specified: Optional requires 1, 2 provided",
        ),
        (
            "Boolean[true,false]",
            "Invalid number of type parameters specified: Boolean requires 1, 2 provided",
        ),
        (
            "Float[1,2,3]",
            "Invalid number of type parameters specified: Float requires 1 or 2, 3 provided",
        ),
        (
            "String[1,2,3]",
            "Invalid number of type parameters specified: String requires 1 to 2, 3 provided",
        ),
        (
            "NotUndef[Integer,String]",
            "Invalid number of type parameters specified: NotUndef requires 0 to 1, 2 provided",
        ),
    ]
    for spec, message in cases:
        with pytest.raises(HieraLookupError) as exc_info:
            parse_type(spec)
        assert str(exc_info.value) == message, spec

    # Float bounds stay floats, and the "from > to" message prints them so.
    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Float[5.0,1.0]")
    assert str(exc_info.value) == "'from' must be less or equal to 'to'. Got (5.0, 1.0"


def test_parser_never_parameterized_and_unsupported_with_args():
    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Data[Integer]")
    assert str(exc_info.value) == "Not a parameterized type <Data>"

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Any[Integer]")
    assert str(exc_info.value) == "Not a parameterized type <Any>"

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Iterable[Integer]")
    assert (
        str(exc_info.value)
        == "hiera does not support the Puppet type 'Iterable[Integer]'"
    )

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("SemVer[1,2]")
    assert str(exc_info.value) == "hiera does not support the Puppet type 'SemVer[1,2]'"

    ref = parse_type("Stdlib::Port[80]")
    assert isinstance(ref, TypeReference)
    assert str(ref) == "TypeReference['Stdlib::Port[80]']"


def test_parser_literals_and_collections():
    # Array/hash literal argument shapes: empty, one element, a trailing
    # comma, and a bare "k => v" collapsed into one pair-argument.
    assert str(parse_type("Array[Integer]")) == "Array[Integer]"
    assert str(parse_type("Array[String,]")) == "Array[String]"
    assert str(parse_type("Hash[String,Integer,]")) == "Hash[String, Integer]"
    assert str(parse_type("Struct[{}]")) == "Struct"
    assert str(parse_type("Struct[{Optional[c] => String, d => Integer}]")) == (
        "Struct[{Optional['c'] => String, 'd' => Integer}]"
    )
    # A trailing comma in a hash literal's own body (distinct from the
    # access-argument-list trailing comma covered above).
    assert str(parse_type("Struct[{a=>String,}]")) == "Struct[{'a' => String}]"

    # `default` as an explicit lower size bound is 0.
    assert str(parse_type("Array[Integer,default,3]")) == "Array[Integer, 0, 3]"
    assert str(parse_type("Hash[String,Integer,default,3]")) == (
        "Hash[String, Integer, 0, 3]"
    )
    assert str(parse_type("Collection[default,3]")) == "Collection[0, 3]"
    assert str(parse_type("Collection[1,default]")) == "Collection[1]"

    # Unary minus.
    assert parse_type("Integer[-5,5]").instance(-5) is True

    # Quoted-string escapes (double- and single-quoted), inside an Enum
    # argument list.
    enum_t = parse_type('Enum["a\\nb\\t\\r\\"\\\\c"]')
    assert enum_t.values == ('a\nb\t\r"\\c',)
    assert parse_type("Pattern['^a\\'b']").instance("a'bx") is True

    # Collection's single-bound form (a lower bound only, no upper).
    assert str(parse_type("Collection[5]")) == "Collection[5]"


def test_parser_advance_past_eof_is_a_no_op():
    # Every call site in this module only ever advances past a token whose
    # kind it just confirmed is not "eof" -- so calling `advance()` a
    # second time once the parser is already sitting on the eof token (a
    # defensive backstop against an out-of-range `self.pos`) never happens
    # through `parse_type()` itself. Exercised directly on the class.
    p = _Parser("x")
    first = p.advance()
    assert first.kind == "name"
    pos_at_eof = p.pos
    eof1 = p.advance()
    assert eof1.kind == "eof"
    assert p.pos == pos_at_eof  # unchanged: the "if tok.kind != eof" guard
    eof2 = p.advance()
    assert eof2 is eof1
    assert p.pos == pos_at_eof  # still unchanged on a second call


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
    assert "hiera does not support the Puppet type 'Iterable'" == str(exc_info.value)

    # infer_set of a RubySymbol-named object renders Runtime[ruby, 'Symbol'].
    # The real class is *defined* in _psych and re-exported through
    # backends -- matched by (name, __module__) against the former.
    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera.backends._psych"
    assert str(infer_set(RubySymbol())) == "Runtime[ruby, 'Symbol']"


# -------------------------------------------------- type-model internals
#
# _key()/__eq__()/__hash__()
# have no entry point of their own -- convert_to/value_type only ever call
# instance()/new()/str() on a parsed type. This module ports Puppet's whole
# type model (type_calculator.rb/type_formatter.rb), not only what hiera's
# lookup path currently wires up, so the rest of that API is exercised
# directly against parsed/inferred type instances, same as the rest of this
# file.


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
    # `Optional[integer]`/`NotUndef[integer]` (a bareword *contained type
    # argument*) keep the contained value as a raw Python str, rendered
    # through the plain-string branch -- but Optional/NotUndef can also wrap
    # a real String with `.literal` set (never produced by parse_type,
    # only by infer()); that flavor renders its quoted literal directly
    # rather than recursing into the child's own (bare "String") renderer.
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

    # A range-bound rendered through `_num_str`, not the value's own
    # instance-check: `infer()` on a NaN/Infinity/large-exponent float turns
    # it into `Float[<that value>, <that value>]`, and the mismatch-message
    # path (`_lookup/data_functions.py`/`_lookup/lookup_adapter.py`/
    # `_types/mismatch.py`,
    # all calling `infer(value)`) renders it right there.
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
    # `_num_str`'s bool branch and `_literal_str`'s float/other branches are
    # unreachable through `parse_type()`: the parser's own `_num_or_default`
    # only ever hands Integer/Float range bounds a "number" or "default"
    # node (never "bool"), and `_build_enum` only ever hands `Enum` a
    # "string" or "bool" node -- so neither a bool bound nor a non-str/bool
    # Enum value can arise from real Puppet type-expression text. Both
    # helpers are still exercised directly, the same as every other private
    # function in this module.
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


# ------------------------------------------------------- mismatch describer

#: convert-value-type queries whose golden status is a parse error, not an
#: instance mismatch -- already proved by test_parse_matches_golden.
_PARSE_ERROR_TNN = {"t62", "t63", "t69"}


def _assert_matches_params():
    case = yaml.safe_load(
        (_CASES / "convert-value-type" / "case.yaml").read_text(encoding="utf-8")
    )
    golden = _golden("convert-value-type")["results"]
    data = _data("convert-value-type")
    for q in case["queries"]:
        qid = q["id"]
        if qid in _PARSE_ERROR_TNN:
            continue
        yield pytest.param(qid, q["type"], data[q["key"]], golden[qid], id=qid)


@pytest.mark.parametrize("qid,type_text,value,result", list(_assert_matches_params()))
def test_assert_matches_golden(qid, type_text, value, result):
    t = parse_type(type_text)
    if result["status"] == "found":
        assert assert_instance_of("Found value", t, value) is value
        return
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", t, value)
    assert str(exc_info.value).splitlines()[0].strip() == result["message"].strip()


def test_describe_evidence():
    # lookup('hint', Data) with hint: {1: a} -> "Found value has wrong type,
    # expects a Data value, got Hash".
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", parse_type("Data"), {1: "a"})
    assert (
        str(exc_info.value)
        == "Found value has wrong type, expects a Data value, got Hash"
    )

    # lookup('hsi', Variant[Hash[String, String], Array]) -> first line
    # " Found value has wrong type, variant 0 entry 'a' expects a String
    # value, got Integer".
    hsi = {"a": 1, "b": 2}
    t = parse_type("Variant[Hash[String, String], Array]")
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", t, hsi)
    assert str(exc_info.value).splitlines()[0] == (
        "Found value has wrong type, variant 0 entry 'a' expects a String value, got Integer"
    )

    # lookup('big', Variant[Integer[0,10], Integer[20,30]]).
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Variant[Integer[0,10], Integer[20,30]]"), 11
        )
    assert str(exc_info.value) == (
        "Found value has wrong type, expects a value of type Integer[0, 10] or "
        "Integer[20, 30], got Integer[11, 11]"
    )

    # lookup('s', Variant[Enum['a'], Pattern[/^x/]]).
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Variant[Enum['a'], Pattern[/^x/]]"), "str"
        )
    assert str(exc_info.value) == (
        "Found value has wrong type, expects a match for "
        "Variant[Enum['a'], Pattern[/^x/]], got 'str'"
    )

    # Optional[Integer[0]] on -3.
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", parse_type("Optional[Integer[0]]"), -3)
    assert str(exc_info.value) == (
        "Found value has wrong type, expects a value of type Undef or "
        "Integer[0], got Integer[-3, -3]"
    )

    # Hash[String, Integer, 1, 2] on {a: "x"}.
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Hash[String, Integer, 1, 2]"), {"a": "x"}
        )
    assert str(exc_info.value) == (
        "Found value has wrong type, entry 'a' expects an Integer value, got String"
    )

    # Tuple[String, String, String] on [1, "x"] -> size mismatch.
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Tuple[String, String, String]"), [1, "x"]
        )
    assert (
        str(exc_info.value) == "Found value has wrong type, expects size to be 3, got 2"
    )

    # Hash[String, String] on {a: 1, b: 2} -> two lines, joined with "\n ".
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Hash[String, String]"), {"a": 1, "b": 2}
        )
    lines = str(exc_info.value).splitlines()
    assert (
        lines[0]
        == "Found value has wrong type, entry 'a' expects a String value, got Integer"
    )
    assert (
        lines[1]
        == " Found value has wrong type, entry 'b' expects a String value, got Integer"
    )


def test_describe_evidence_more_branches():
    def err(t, v):
        with pytest.raises(HieraLookupError) as exc_info:
            assert_instance_of("Found value", t, v)
        return str(exc_info.value)

    # A Variant branch that is itself an Optional[<bareword literal>]:
    # short_name's wrapper-type case, with a raw-str `.contained` (never a
    # real Any -- `_bare_name`'s own str branch).
    assert err(parse_type("Variant[Optional[integer], Boolean]"), 5) == (
        "Found value has wrong type, expects a value of type Optional[String] "
        "or Boolean, got Integer"
    )

    # NotUndef on Undef, both bare and with a contained type.
    assert err(parse_type("NotUndef[Integer]"), None) == (
        "Found value has wrong type, expects a NotUndef[Integer] value, got Undef"
    )
    assert err(parse_type("NotUndef"), None) == (
        "Found value has wrong type, expects a NotUndef value, got Undef"
    )
    # A bare NotUndef (no contained type) never rejects a non-Undef value,
    # whatever it is; a bare Optional accepts only undef.
    marker = object()
    assert assert_instance_of("Found value", parse_type("NotUndef"), marker) is marker
    assert "expects an Optional value, got Integer" in err(parse_type("Optional"), 5)

    # A type alias whose own `.instance()` fails collapses to one mismatch
    # on the alias itself (never the branches' own structural detail).
    assert err(parse_type("RichDataKey"), [1, 2]) == (
        "Found value has wrong type, expects a RichDataKey value, got Tuple"
    )

    # Array/Collection/Tuple/Struct all reject a non-list/non-dict value the
    # same way, before any element/size check.
    assert err(parse_type("Array[Integer]"), "not a list") == (
        "Found value has wrong type, expects an Array value, got String"
    )
    assert err(parse_type("Collection"), "not a list") == (
        "Found value has wrong type, expects a Collection value, got String"
    )
    assert err(parse_type("Tuple[Integer]"), "not a list") == (
        "Found value has wrong type, expects a Tuple value, got String"
    )
    assert err(parse_type("Struct[{a=>String}]"), [1, 2]) == (
        "Found value has wrong type, expects a Struct value, got Tuple"
    )

    # `_size_text`'s four shapes: unlimited (no bound at all -- covered by
    # the Collection case above, which has no size constraint), "at least"
    # (a lower bound only), "at most" (an upper bound only), and "between"
    # (both, covered by the Tuple/Hash cases elsewhere in this file).
    assert err(parse_type("Array[Integer,3]"), [1, 2]) == (
        "Found value has wrong type, expects size to be at least 3, got 2"
    )
    assert err(parse_type("Array[Integer,default,3]"), [1, 2, 3, 4]) == (
        "Found value has wrong type, expects size to be at most 3, got 4"
    )

    # A Variant with a mix of immediate (whole-value) and deeper (nested
    # path) failures reports the first deeper one, prefixed with its own
    # "variant N" path element.
    assert err(parse_type("Variant[Hash[String,String],Array]"), {"a": 1, "b": 2}) == (
        "Found value has wrong type, variant 0 entry 'a' expects a String "
        "value, got Integer"
    )

    # `short_name`'s own top-level "expected is a raw literal string"
    # branch (distinct from the wrapper-type case above): NotUndef[integer]
    # unwraps to its raw-str contained type directly (not re-wrapped).
    assert err(parse_type("NotUndef[integer]"), 5) == (
        "Found value has wrong type, expects a String value, got Integer"
    )

    # A wrapper type whose contained type is literally `Any` renders bare
    # (no "[Any]" suffix) -- `short_name`'s own "or not _is_any(contained)"
    # guard.
    assert err(parse_type("Sensitive[Any]"), 5) == (
        "Found value has wrong type, expects a Sensitive value, got Integer"
    )

    # An Optional wrapping a Variant: the "Undef" prefix (m.optional) on a
    # list-shaped `e_render`, `_join_or`'s 3-way "a, b, or c" join.
    assert err(parse_type("Optional[Variant[Integer,Boolean]]"), "x") == (
        "Found value has wrong type, expects a value of type Undef, "
        "Integer, or Boolean, got String"
    )

    # Two Variant branches that render to the *same* short name dedupe down
    # to `_join_or`'s single-item return (no "or" at all).
    assert err(parse_type("Variant[Optional[integer], Optional[foo]]"), 5) == (
        "Found value has wrong type, expects a value of type "
        "Optional[String], got Integer"
    )

    # `_actual_literal`'s own fallback (a non-string actual value against a
    # pattern-shaped expected type renders the actual's short name, not a
    # quoted literal).
    assert err(parse_type("Enum[a,b]"), 5) == (
        "Found value has wrong type, expects a match for Enum['a', 'b'], " "got Integer"
    )

    # A type alias whose own `.instance()` succeeds: `_describe` returns no
    # mismatches for it, same as any other matching type.
    assert (
        assert_instance_of("Found value", parse_type("RichDataKey"), "a string")
        == "a string"
    )

    # A literal-string contained type (NotUndef[integer]) that DOES match.
    assert (
        assert_instance_of("Found value", parse_type("NotUndef[integer]"), "integer")
        == "integer"
    )

    # A Variant where the FIRST branch fails immediately (shallow) and a
    # LATER one fails deeper: the loop must skip the immediate one to find
    # the deep one it actually reports.
    assert err(
        parse_type("Variant[Boolean, Hash[String,String]]"), {"a": 1, "b": 2}
    ) == (
        "Found value has wrong type, variant 1 entry 'a' expects a String "
        "value, got Integer"
    )


def test_size_text_direct():
    # `_size_text`'s own "unlimited" branch is unreachable through
    # `_describe_array`/`_describe_hash`/`_describe_tuple` (each defaults or
    # normalizes its bounds before ever calling `_size_mismatch`, so a
    # size-mismatch report is never built from a truly unconstrained size);
    # exercised directly, the same as this file's other private helpers.
    assert _size_text(None, None) == "unlimited"
    assert _size_text(3, None) == "at least 3"
    assert _size_text(None, 3) == "at most 3"
    assert _size_text(1, 3) == "between 1 and 3"


def test_a_an_direct():
    # _a_an's own leading-quote skip (Puppet's a_an handles a quoted label,
    # even though nothing this subset renders through it is ever
    # quote-prefixed): a quote char is skipped, a real letter stops the
    # loop, and a label that is quote characters all the way through (or
    # empty) never finds one at all.
    assert _a_an("'x'") == "a"
    assert _a_an("'Anvil'") == "an"
    assert _a_an("''") == "a"
    assert _a_an("") == "a"


def test_assert_nil_ok_and_reference():
    assert (
        assert_instance_of("Found value", parse_type("String"), None, nil_ok=True)
        is None
    )
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", parse_type("Stdlib::Port"), "80")
    assert str(exc_info.value) == (
        "Found value has wrong type, references an unresolved type 'Stdlib::Port'"
    )


# ----------------------------------------------------------- new() / format


def _run_new(spec, value):
    """Build new()'s args from a lookup_options convert_to spec exactly as
    the lookup engine does: a list as is, else [spec]; a str first element goes
    through parse_type, anything else (a malformed convert_to shape) is
    passed to new_instance raw."""
    args = list(spec) if isinstance(spec, list) else [spec]
    type_arg = args[0] if args else None
    rest = args[1:]
    type_ = parse_type(type_arg) if isinstance(type_arg, str) else type_arg
    return new_instance(type_, value, *rest)


def _new_params():
    for case_name in ("convert-new", "convert-cv"):
        case = yaml.safe_load(
            (_CASES / case_name / "case.yaml").read_text(encoding="utf-8")
        )
        golden = _golden(case_name)["results"]
        data = _data(case_name)
        opts = data["lookup_options"]
        seen = set()
        for q in case["queries"]:
            if q.get("merge") is not None:
                continue
            key = q["key"]
            qid = q.get("id") or key
            if qid in seen:
                continue
            seen.add(qid)
            spec = opts.get(key, {}).get("convert_to")
            if spec is None:
                continue
            yield pytest.param(
                qid, spec, data[key], golden[qid], id="{}::{}".format(case_name, qid)
            )


#: hiera has no new() for these (Puppet does) -- ours is a
#: deliberate deviation, never the golden's recorded (Puppet-real) outcome.
_Q8_UNSUPPORTED_QIDS = {"semver", "tspan", "re_t"}

#: convert-cv/tuple_t: a real, oracle-confirmed discrepancy between what
#: ``Tuple.new()`` reports through a direct call (both index 0 and index 1
#: mismatch, verified with `puppet apply`) and what the SAME conversion
#: reports through `lookup_options`/`convert_to` (only index 1) -- the
#: lookup_adapter path's own error surfaces only the last element checked.
#: Not reproduced here (this subset collects every mismatch, matching the
#: direct-call oracle behaviour); asserted loosely instead of exactly.
_KNOWN_QUIRK_QIDS = {"tuple_t"}


@pytest.mark.parametrize("qid,spec,value,result", list(_new_params()))
def test_new_matches_golden(qid, spec, value, result):
    from hyera import Sensitive

    if qid in _Q8_UNSUPPORTED_QIDS:
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, value)
        assert "hiera does not support new() for the Puppet type" in str(exc_info.value)
        return

    message = result.get("message", "")
    if "could not parse" in message and message.startswith(
        "Invalid data type in lookup_options for key"
    ):
        inner = message.split("error: '", 1)[1]
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, value)
        assert str(exc_info.value) == inner
        return
    if result["status"] == "error":
        inner = (
            message.split("raised error: ", 1)[1]
            if "raised error: " in message
            else message
        )
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, value)
        if qid in _KNOWN_QUIRK_QIDS:
            assert inner.splitlines()[-1].strip() in str(exc_info.value)
        else:
            assert str(exc_info.value) == inner
        return
    got = _run_new(spec, value)
    if isinstance(got, Sensitive):
        got = "Sensitive [value redacted]"
    assert json.dumps(got, sort_keys=True) == json.dumps(
        result["value"], sort_keys=True
    )


def test_new_unrecorded():
    with pytest.raises(HieraLookupError) as exc_info:
        _run_new([parse_type("Integer"), "default", True], "-5")
    assert str(exc_info.value) == (
        "Illegal radix: default, expected 2, 8, 10, 16, or default"
    )

    for spec in (
        [parse_type("String"), {"Integer": "%x"}],
        [parse_type("String"), 5],
    ):
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, "x" if not isinstance(spec[1], int) else 5)
        assert "'new_string' parameter 'string_formats'" in str(exc_info.value)

    with pytest.raises(HieraLookupError) as exc_info:
        _run_new(parse_type("Integer"), {"a": 1})
    assert "Integer.new has wrong type" in str(
        exc_info.value
    ) or "unrecognized key 'a'" in str(exc_info.value)


def test_new_dispatch_optional_notundef_bare_and_literal_contained():
    # _dispatch directly: new_instance()'s own fast path ("not args and
    # type_.instance(value): return value") would otherwise skip _dispatch
    # entirely here, since any non-None value trivially satisfies a bare
    # Optional/NotUndef's own instance() check.
    from hyera._types.new_function import _dispatch

    # A bare Optional/NotUndef (no contained type argument at all) is not
    # actually new()-able on its own -- Puppet has no meaningful
    # "construct an Optional" operation without a contained type.
    with pytest.raises(HieraLookupError, match="is not supported"):
        _dispatch(parse_type("Optional"), "x", ())
    with pytest.raises(HieraLookupError, match="is not supported"):
        _dispatch(parse_type("NotUndef"), "x", ())
    # A bareword literal contained type (Optional[integer]): dispatches
    # through a plain String new(), since the literal is kept as a raw str
    # rather than a real type.
    assert _dispatch(parse_type("Optional[integer]"), "integer", ()) == "integer"


def test_new_integer_binary_literal():
    from hyera._types.new_function import new_instance

    assert new_instance(parse_type("Integer"), "0b101") == 5


def test_new_dispatch_integer_int_passthrough_and_empty_dict():
    # _dispatch directly (same fast-path reason as the Optional/NotUndef
    # case above -- an int already satisfies Integer's own instance()).
    from hyera._types.new_function import _dispatch

    assert _dispatch(parse_type("Integer"), 5, ()) == 5
    # An empty dict is a named-arguments hash with no entry at all.
    with pytest.raises(HieraLookupError) as exc_info:
        _dispatch(parse_type("Integer"), {}, ())
    assert str(exc_info.value) == (
        "Integer.new has wrong type, expects size to be between 1 and 3, got 0"
    )


def test_new_dispatch_float_and_numeric_from_int_and_unsupported_type():
    # _dispatch directly, for the same reason as above: an int already
    # satisfies Numeric's own instance() check (unlike Float's, so that
    # one call goes through new_instance() normally).
    from hyera._types.new_function import _dispatch, new_instance

    assert new_instance(parse_type("Float"), 5) == 5.0
    assert _dispatch(parse_type("Numeric"), 5, ()) == 5
    with pytest.raises(HieraLookupError, match="cannot be converted to Float"):
        _dispatch(parse_type("Float"), [1, 2], ())
    with pytest.raises(HieraLookupError, match="cannot be converted to Numeric"):
        _dispatch(parse_type("Numeric"), [1, 2], ())


# ------------------------------------------------------ convert_result


def test_convert_result_messages():
    with pytest.raises(HieraLookupError) as exc_info:
        convert_result("k", "NoSuch[", "x")
    assert str(exc_info.value) == (
        "Invalid data type in lookup_options for key 'k' could not parse "
        "'NoSuch[', error: 'Syntax error at end of input"
    )
    assert exc_info.value.__cause__ is not None

    with pytest.raises(HieraLookupError) as exc_info:
        convert_result("k", "Boolean", "maybe")
    assert str(exc_info.value) == (
        "The convert_to lookup_option for key 'k' raised error: "
        "'new_boolean' The string 'maybe' cannot be converted to Boolean"
    )
    assert exc_info.value.__cause__ is not None

    assert convert_result("k", None, "5") == "5"

    with pytest.raises(HieraLookupError) as exc_info:
        convert_result("k", "SemVer", "1.2.3")
    assert str(exc_info.value) == (
        "The convert_to lookup_option for key 'k' raised error: hiera does "
        "not support new() for the Puppet type 'SemVer'"
    )
    assert exc_info.value.__cause__ is not None


# ------------------------------------------------------------ Sensitive


def test_sensitive_puppet_semantics():
    assert str(Sensitive("a")) == "Sensitive [value redacted]"
    assert repr(Sensitive("a")) == "Sensitive [value redacted]"

    assert Sensitive("a") == Sensitive("a")
    assert Sensitive(1) != Sensitive(1.0)
    assert Sensitive(1) != Sensitive(True)
    assert Sensitive([1, {"a": 2}]) == Sensitive([1, {"a": 2}])
    assert len({Sensitive([1]), Sensitive([1])}) == 1
    assert Sensitive("a") != "a"
    assert Sensitive("a").unwrap() == "a"


def test_ruby_format_table():
    assert _string_convert(8, "%#o") == "010"
    # Plain "%o" (no "#" alternate-form flag) -- distinct from the "%#o"
    # case above, which always takes the "0" prefix branch.
    assert _string_convert(8, "%o") == "10"
    assert _string_convert(5, "%b") == "101"
    assert _string_convert(255, "%x") == "ff"
    assert _string_convert(1e20, "%p") == "1.0e+20"
    # A mantissa that already has a decimal point in exponent form (unlike
    # 1e20 above, whose repr() mantissa is bare "1" and needs ".0" added).
    assert _string_convert(1.5e20, "%p") == "1.5e+20"
    assert _string_convert(3.0, "%f") == "3.000000"
    assert _string_convert(2.5, "%s") == "2.5"
    # "#" alternate-form prefix on hex/binary (octal's own "#" is covered
    # above already).
    assert _string_convert(255, "%#x") == "0xff"
    assert _string_convert(5, "%#B") == "0B101"
    # An integer value with a float-style directive redirects through the
    # float body, not the integer one.
    assert _string_convert(3, "%f") == "3.000000"
    # Float body: uppercase exponent + width/precision, "+" on a
    # non-negative value, left-justify, and the hex-float form.
    assert _string_convert(3.14159, "%10.2E") == "  3.14E+00"
    assert _string_convert(3.14159, "%+.2f") == "+3.14"
    assert _string_convert(3.14159, "%-10.2f") + "|" == "3.14      |"
    assert _string_convert(3.14159, "%A") == "0X1.921F9F01B866EP+1"
    # NaN/Infinity (Ruby Float#inspect, not Python's repr spelling).
    assert _string_convert(float("nan"), "%p") == "NaN"
    assert _string_convert(float("inf"), "%p") == "Infinity"
    assert _string_convert(float("-inf"), "%p") == "-Infinity"
    # A string_formats value that is not one directive is refused.
    for bad in ("%", "nope", "%d items"):
        with pytest.raises(HieraLookupError, match="is not a valid format"):
            _string_convert(5, bad)
    # Sensitive redacts through every directive, as Ruby's inspect does.
    assert _string_convert(Sensitive("secret"), "%p") == "#<Sensitive [value redacted]>"
    assert _string_convert(Sensitive("secret"), "%s") == "Sensitive [value redacted]"
    # "%c": the integer's own character (Kernel#format's char directive).
    assert _string_convert(65, "%c") == "A"
    # Integer body width/padding: zero-padded, negative zero-padded (sign
    # then digits), left-justified, and plain space-padded.
    assert _string_convert(5, "%05d") == "00005"
    assert _string_convert(-5, "%05d") == "-0005"
    assert _string_convert(5, "%-5d") + "|" == "5    |"
    assert _string_convert(5, "%5x") == "    5"
    # "%g": Ruby's general float format (the exponent form's own sign is
    # already normalized by Python's repr, so _ruby_float_inspect's own
    # "prepend a sign" branch is effectively unreachable -- not a
    # discrepancy Puppet fidelity depends on).
    assert _string_convert(3.14159, "%g") == "3.14159"
    assert _string_convert(1e10, "%g") == "1e+10"
    # The final, otherwise-unmodeled-type fallback: a RubySymbol (or any
    # other object with no Ruby equivalent this subset renders specially)
    # falls back to plain str().
    from hyera.backends import RubySymbol

    assert _string_convert(RubySymbol("x")) == ":x"


def test_puppet_quote():
    # A control character not in the named-escape table renders as a
    # \u{XX} escape, and forces the double-quoted form.
    assert _puppet_quote("a\x01b") == '"a\\u{1}b"'
    assert _puppet_quote("x", enforce_double_quotes=True) == '"x"'
    # Single-quoted form: an embedded "'" and a literal "\" both escape.
    assert _puppet_quote("it's") == "'it\\'s'"
    assert _puppet_quote("a\\b") == "'a\\b'"
    # A trailing, unpaired backslash still closes the quote correctly.
    assert _puppet_quote("trail\\") == "'trail\\'"


# Puppet 8.10's own rendering of each parsed text (recorded from
# ``puppet lookup``'s type formatter).
PUPPET_PARSE_TEXT = [
    ("Enum", "Enum"),
    ("Pattern", "Pattern"),
    ("Tuple", "Tuple"),
    ("Struct", "Struct"),
    ("Struct[{}]", "Struct"),
    ("Variant", "Variant"),
    ("Integer[0x10, 0x20]", "Integer[16, 32]"),
    ("Integer[010]", "Integer[8]"),
    ("Integer[--1]", "Integer[1]"),
    ("String[default,10]", "String[0, 10]"),
    ("String[-1]", "String[0]"),
    ("Array[String, default, 3]", "Array[String, 0, 3]"),
    ("Hash[String, Integer, default, 3]", "Hash[String, Integer, 0, 3]"),
    ("Collection[default, 2]", "Collection[0, 2]"),
    ("Array[Any]", "Array"),
    ("Hash[Any,Any]", "Hash"),
    ("Enum['a', 'b', false]", "Enum['a', 'b']"),
    ("Enum['a', 'b', true]", "Enum['a', 'b', true]"),
    ("Enum[a,a]", "Enum['a']"),
    ("Tuple[String, Integer, 1, default]", "Tuple[String, Integer, 1]"),
    ("Tuple[String, default]", "Tuple[String, 0]"),
    ("Tuple[String, default, 3]", "Tuple[String, 0, 3]"),
    ("Tuple[1,2]", "Tuple"),
    ("Tuple[1]", "Tuple"),
    ("Struct[{NotUndef[a]=>Integer}]", "Struct[{'a' => Integer}]"),
    (
        "Struct[{Optional[a]=>Optional[Integer]}]",
        "Struct[{'a' => Optional[Integer]}]",
    ),
    ("Struct[{a=>Integer, a=>String}]", "Struct[{'a' => String}]"),
    ("Variant[String]", "String"),
    ("Variant[String, String]", "String"),
    ("Variant[Variant[String,Integer], Float]", "Variant[String, Integer, Float]"),
]


@pytest.mark.parametrize("text, expected", PUPPET_PARSE_TEXT)
def test_parse_renders_as_puppet_does(text, expected):
    assert str(parse_type(text)) == expected


@pytest.mark.parametrize(
    "text, message",
    [
        ("Enum[1]", "Enum parameters must be identifiers or strings"),
        ("Enum[true]", "Enum requires 1 or more, 0 provided"),
        ("Enum[A]", "Enum parameters must be identifiers or strings"),
        ("Enum[a, b, true, c]", "Enum parameters must be identifiers or strings"),
        ("Enum[true, a]", "Enum parameters must be identifiers or strings"),
        ("Enum[undef]", "Enum parameters must be identifiers or strings"),
        ("Enum[[1,2]]", "Enum parameters must be identifiers or strings"),
        ("Enum[[]]", "Enum parameters must be identifiers or strings"),
        ("Enum[[1,2,]]", "Enum parameters must be identifiers or strings"),
        ("Boolean[true,false]", "Boolean requires 1, 2 provided"),
        ("Collection[1,2,3]", "Collection requires 1 to 2, 3 provided"),
        ("Array[String,1,2,3]", "Array requires 1 to 3, 4 provided"),
        (
            "Pattern[1]",
            "Only String, Regexp, Pattern-Type, and Regexp-Type are allowed",
        ),
        ("String[1.5]", "not a valid type specification"),
        ("Struct[{'' => Integer}]", "Struct element key cannot be an empty String"),
        ("Struct[{1=>Integer}]", "Illegal Struct member key type"),
        ("Struct[{a=>Integer},{b=>String}]", "Struct requires 1, 2 provided"),
        ("ScalarData[1]", "Not a parameterized type <ScalarData>"),
        ("RichData[1]", "Not a parameterized type <RichData>"),
        ("Puppet::LookupKey[1]", "Not a parameterized type <Puppet::LookupKey>"),
        ("Float[2.0,1.0]", "Got (2.0, 1.0"),
        ("Float[0.1, 1.0e-5]", "Got (0.1, 1.0e-05"),
        ("Float[1e400]", "NUMBER token does not contain a valid number, 1e400"),
    ],
)
def test_parse_rejects_with_puppets_text(text, message):
    with pytest.raises(HieraLookupError) as info:
        parse_type(text)
    assert message in str(info.value)


def test_concurrent_parses_never_share_source_text(monkeypatch):
    """Thread A is parsed up to the point its tree is built, then thread B
    parses a different text to completion; A must still report its own text."""
    import threading

    from hyera._types import parser as parser_module

    a_text, b_text = "Aaaaaaaaa1[1]", "Bbbbbbbbbbbbbbbb2[2]"
    a_built, b_done = threading.Event(), threading.Event()
    original = parser_module._Parser.parse_primary
    depth = threading.local()

    def parse_primary(self):
        depth.n = getattr(depth, "n", 0) + 1
        try:
            return original(self)
        finally:
            depth.n -= 1
            if depth.n == 0 and self.text == a_text:
                a_built.set()
                b_done.wait(5)

    monkeypatch.setattr(parser_module._Parser, "parse_primary", parse_primary)
    results = {}

    def parse_a():
        results["a"] = str(parse_type(a_text))

    def parse_b():
        a_built.wait(5)
        results["b"] = str(parse_type(b_text))
        b_done.set()

    threads = [threading.Thread(target=parse_a), threading.Thread(target=parse_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == {
        "a": "TypeReference['{}']".format(a_text),
        "b": "TypeReference['{}']".format(b_text),
    }


# (value, format, Puppet 8.10's ``String.new`` output).
STRING_FORMAT_ROWS = [
    (0, "%+d", "+0"),
    (1, "%5d", "    1"),
    (1, "% d", " 1"),
    (1, "%10s", "         1"),
    (1, "%#s", '"1"'),
    (1, "%.3d", "001"),
    (-1, "%05d", "-0001"),
    (-1, "%+d", "-1"),
    (-1, "%x", "..f"),
    (-1, "%o", "..7"),
    (-1, "%b", "..1"),
    (-1, "%#b", "0b..1"),
    (255, "%x", "ff"),
    (255, "%#x", "0xff"),
    (255, "%o", "377"),
    (255, "%#o", "0377"),
    (255, "%b", "11111111"),
    (255, "%.3d", "255"),
    (255, "%.10x", "00000000ff"),
    (-255, "%x", "..f01"),
    (-255, "%X", "..F01"),
    (-255, "%#x", "0x..f01"),
    (-255, "%+x", "-ff"),
    (65, "%c", "A"),
    (65, "%5c", "    A"),
    (1.0, "%A", "0X1P+0"),
    (-1.5, "%d", "-1"),
    (-1.5, "%e", "-1.500000e+00"),
    (-1.5, "%E", "-1.500000E+00"),
    (-1.5, "%f", "-1.500000"),
    (-1.5, "%10.3f", "    -1.500"),
    (-1.5, "%+.1f", "-1.5"),
    (-1.5, "%010.2f", "-000001.50"),
    (3.14159, "%.2f", "3.14"),
    (1e20, "%e", "1.000000e+20"),
    (1e20, "%g", "1e+20"),
    (1e20, "%s", "1.0e+20"),
    (1e-05, "%G", "1E-05"),
    (1e16, "%s", "1.0e+16"),
    (1000000000000000.0, "%s", "1.0e+15"),
    (1000000000000000.0, "%p", "1.0e+15"),
    (100.0, "%g", "100"),
    (100.0, "%#g", "100.000"),
    ("abc", "%10s", "       abc"),
    ("abc", "%.2s", "ab"),
    ("abc", "%u", "ABC"),
    ("abc", "%C", "Abc"),
    ("a'b", "%p", "'a\\'b'"),
    ("a\nb", "%p", '"a\\nb"'),
    ("Abc", "%d", "abc"),
    (True, "%d", "1"),
    (True, "%T", "True"),
    (True, "%y", "yes"),
    (True, "%#y", "y"),
    (False, "%t", "false"),
    (False, "%Y", "No"),
    (None, "%d", "NaN"),
    (None, "%s", ""),
    (None, "%p", "undef"),
    ([1, "a", None, True, 2.5], "%p", "[1, 'a', undef, true, 2.5]"),
    ({"a": 1}, "%a", "[['a', 1]]"),
    ({"a": 1}, "%p", "{'a' => 1}"),
    ({"a": [1, {"b": None}]}, "%s", "{'a' => [1, {'b' => undef}]}"),
    (5, "%d\n", "5\n"),
]


@pytest.mark.parametrize("value, fmt, expected", STRING_FORMAT_ROWS)
def test_string_format_matches_puppet(value, fmt, expected):
    assert _string_convert(value, fmt) == expected


@pytest.mark.parametrize(
    "value, fmt, message",
    [
        (5, "%z", "Illegal format 'z' specified for value of Integer type"),
        (1.5, "%z", "Illegal format 'z' specified for value of Float type"),
        ("a", "%z", "Illegal format 'z' specified for value of String type"),
        ([1], "%d", "Illegal format 'd' specified for value of Array type"),
        ({}, "%d", "Illegal format 'd' specified for value of Hash type"),
        (5, "%d items", "The format '%d items' is not a valid format on the form"),
        (5, "abc", "The format 'abc' is not a valid format on the form"),
        (5, "%dd", "is not a valid format"),
        (5, "%-5d|", "is not a valid format"),
        (5, "%--5d", "The same flag can only be used once, got '%--5d'"),
        (65, "%.3c", None),
        (-1, "%c", "pack(U): value out of range"),
    ],
)
def test_string_format_refuses_what_puppet_refuses(value, fmt, message):
    if message is None:
        assert _string_convert(value, fmt) == "A"
        return
    with pytest.raises(HieraLookupError) as info:
        _string_convert(value, fmt)
    assert message in str(info.value)


def test_string_format_outside_the_subset_is_refused_not_ignored():
    with pytest.raises(HieraLookupError, match="indenting"):
        _string_convert([1], "%#a")
    with pytest.raises(HieraLookupError, match="precision"):
        _string_convert(1.5, "%.2a")
    with pytest.raises(HieraLookupError, match="parameter 'string_formats'"):
        _string_convert(5, {"Integer": "%x"})


def test_string_of_a_hash_uses_puppets_own_separator():
    assert _string_convert({"a": 1, "b": "c"}) == "{'a' => 1, 'b' => 'c'}"


def test_string_new_arity_and_argument_types():
    with pytest.raises(
        HieraLookupError, match="expects between 1 and 2 arguments, got 3"
    ):
        new_instance(parse_type("String"), "x", "%s", "%s")
    with pytest.raises(HieraLookupError, match="parameter 'string_formats' expects"):
        new_instance(parse_type("String"), 5, None)


@pytest.mark.parametrize(
    "type_, value, args, expected",
    [
        ("Integer", "-11", (16, True), 17),
        ("Integer", "-5", (10, True), 5),
        ("Integer", -5, (10, True), 5),
        ("Integer", {"from": "-5", "abs": True}, (), 5),
        ("Integer", {"from": "10", "radix": 16}, (), 16),
        ("Integer", "0x1F", (), 31),
        ("Integer", "- 11", (), -11),
        ("Float", "-5.5", (True,), 5.5),
        ("Float", {"from": "-5.5", "abs": True}, (), 5.5),
        ("Float", "0x1F", (), 31.0),
        ("Float", "- 1.5", (), -1.5),
        ("Numeric", -5, (True,), 5),
        ("Numeric", "- 1.5", (), -1.5),
        ("Numeric", "010", (), 8),
        ("Numeric", "0x10", (), 16),
    ],
)
def test_new_numbers_follow_puppet(type_, value, args, expected):
    got = new_instance(parse_type(type_), value, *args)
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize(
    "type_, value",
    [
        ("Integer", "12\n"),
        ("Integer", "١٢"),
        ("Integer", "1_000"),
        ("Float", "inf"),
        ("Float", "nan"),
        ("Float", "1_000.5"),
        ("Float", "1.5 "),
        ("Float", ".5"),
        ("Float", "1."),
        ("Float", "1.5e+3"),
        ("Numeric", "1 "),
        ("Numeric", "1_000"),
    ],
)
def test_new_numbers_reject_strings_puppet_rejects(type_, value):
    with pytest.raises(HieraLookupError, match="cannot be converted to"):
        new_instance(parse_type(type_), value)


@pytest.mark.parametrize(
    "type_, args, message",
    [
        ("Integer", (10, True, 1), "'new' expects between 1 and 3 arguments, got 4"),
        ("Float", (True, True), "'new_float' expects between 1 and 2 arguments, got 3"),
        (
            "Numeric",
            (True, True),
            "'new_numeric' expects between 1 and 2 arguments, got 3",
        ),
        ("Boolean", (True,), "'new_boolean' expects 1 argument, got 2"),
        ("Sensitive", (1,), "'new_sensitive' expects 1 argument, got 2"),
        ("Array", (True, True), "'new_array' expects between 1 and 2 arguments, got 3"),
        (
            "Array",
            ("x",),
            "'new_array' parameter 'wrap' expects a Boolean value, got String",
        ),
        ("Integer", (3,), "Illegal radix: 3, expected 2, 8, 10, 16, or default"),
    ],
)
def test_new_checks_arguments_as_puppet_does(type_, args, message):
    with pytest.raises(HieraLookupError) as info:
        new_instance(parse_type(type_), "5", *args)
    assert str(info.value) == message


def test_new_numeric_hash_that_is_not_named_arguments_is_just_a_bad_value():
    with pytest.raises(HieraLookupError, match="cannot be converted to Float"):
        new_instance(parse_type("Float"), {"from": 5, "x": 1})


@pytest.mark.parametrize(
    "type_, data",
    [
        ("Variant", 5),
        ("Struct[{a=>Optional[Integer]}]", {}),
        ("Struct", {}),
    ],
)
def test_assert_reports_bare_forms_without_crashing(type_, data):
    try:
        assert_instance_of("Found value", parse_type(type_), data)
    except HieraLookupError:
        pass


def test_struct_of_non_string_keys_reports_a_hash_type_mismatch():
    t = parse_type("Struct[{a=>Integer, Optional[b]=>String}]")
    with pytest.raises(HieraLookupError) as info:
        assert_instance_of("Found value", t, {1: "a"})
    assert str(info.value) == (
        "Found value has wrong type, expects a Struct[{'a' => Integer, "
        "Optional['b'] => String}] value, got Hash[Integer[1, 1], String]"
    )
    with pytest.raises(HieraLookupError, match="expects size to be between 1 and 2"):
        assert_instance_of("Found value", t, {})


def test_type_nesting_past_the_bound_is_a_lookup_error():
    text = "Array[" * 400 + "Integer" + "]" * 400
    with pytest.raises(HieraLookupError, match="nested more than 200 levels"):
        parse_type(text)
    assert str(parse_type("Array[" * 150 + "Integer" + "]" * 150)).count("Array") == 150


def test_convert_result_wraps_every_error_of_a_conversion():
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Numeric", "08")
    assert str(info.value).startswith(
        "The convert_to lookup_option for key 'k' raised error: "
    )
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Integer", float("inf"))
    assert "raised error" in str(info.value)
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Hash", [[[1, 2], 3]])
    assert "raised error: unusable Hash key" in str(info.value)


def test_convert_result_wraps_an_error_that_is_not_a_hiera_error(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr("hyera._lookup.lookup_adapter.new_instance", broken)
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Integer", "5")
    assert str(info.value) == (
        "The convert_to lookup_option for key 'k' raised error: boom"
    )
    assert isinstance(info.value.__cause__, RuntimeError)


def test_a_bad_pattern_in_value_type_is_a_lookup_error():
    with pytest.raises(HieraLookupError, match="unmatched parenthesis"):
        parse_type("Pattern[/(/]")


def test_quotes_are_rendered_by_one_function():
    assert str(parse_type("Enum['a\\b']")) == "Enum['a\\b']"
    assert str(parse_type('Enum["a\\tb"]')) == 'Enum["a\\tb"]'
    assert str(parse_type("Struct[{'a\\b' => Integer}]")) == (
        "Struct[{'a\\b' => Integer}]"
    )


def test_ruby_regex_rejects_a_trailing_backslash():
    from hyera._types.types import _ruby_regex

    with pytest.raises(HieraLookupError) as info:
        _ruby_regex("a\\")
    assert str(info.value) == "too short escape sequence: /a\\/"


def test_pattern_accepts_a_pattern_or_regexp_type_argument():
    assert str(parse_type("Pattern[Pattern[/a/]]")) == "Pattern[/a/]"
    assert str(parse_type("Pattern[Regexp[/a/], /b/]")) == "Pattern[/a/, /b/]"
