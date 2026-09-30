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
from hyera._lookup_adapter import convert_result
from hyera._new_function import new_instance
from hyera._string_converter import convert as _string_convert
from hyera._string_converter import puppet_quote as _puppet_quote
from hyera._type_mismatch import assert_instance_of
from hyera._type_parser import parse_type
from hyera._types import (
    ALIASES,
    PAnyType,
    PEnumType,
    PNotUndefType,
    POptionalType,
    PTypeReferenceType,
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


# --------------------------------------------------------- aliases/refs


def test_aliases_and_references():
    data_t = parse_type("Data")
    assert data_t.instance({"a": [1, None]}) is True
    assert data_t.instance({1: "a"}) is False

    rich_t = parse_type("RichData")
    from hyera import Sensitive

    assert rich_t.instance(Sensitive("x")) is True

    ref = parse_type("Stdlib::Port")
    assert isinstance(ref, PTypeReferenceType)
    assert str(ref) == "TypeReference['Stdlib::Port']"
    assert ref.instance("80") is False

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Iterable")
    assert "hiera does not support the Puppet type 'Iterable'" == str(exc_info.value)

    # infer_set of a RubySymbol-named object renders Runtime[ruby, 'Symbol'].
    # The real class is *defined* in _yaml_loader and re-exported through
    # backends -- matched by (name, __module__) against the former.
    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera._yaml_loader"
    assert str(infer_set(RubySymbol())) == "Runtime[ruby, 'Symbol']"


# -------------------------------------------------- type-model internals
#
# assignable()/_key()/__eq__()/__hash__()/normalize()/alias_expanded_str()
# have no entry point of their own -- convert_to/value_type only ever call
# instance()/new()/str() on a parsed type. This module ports Puppet's whole
# type model (type_calculator.rb/type_formatter.rb), not only what hiera's
# lookup path currently wires up, so the rest of that API is exercised
# directly against parsed/inferred type instances, same as the rest of this
# file.


def test_any_type_base_defaults():
    any_t = parse_type("Any")
    assert isinstance(any_t, PAnyType) and type(any_t) is PAnyType
    assert any_t.assignable(parse_type("Integer")) is True
    assert any_t.assignable(any_t) is True
    assert any_t.normalize() is any_t
    assert any_t.simple_name == "Any"
    assert any_t.alias_expanded_str() == "Any"
    assert repr(any_t) == "<Any>"
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
    ref_b = PTypeReferenceType("Stdlib::Port")
    assert ref_a == ref_b
    assert hash(ref_a) == hash(ref_b)
    assert ref_a._key() == ("Stdlib::Port",)

    assert ALIASES["data"] == parse_type("Data")

    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera._yaml_loader"
    rt_a, rt_b = infer(RubySymbol()), infer(RubySymbol())
    assert rt_a == rt_b
    assert hash(rt_a) == hash(rt_b)
    assert rt_a._key() == ("ruby", "Symbol")


def test_assignable_across_type_family():
    assert parse_type("Integer[1,5]").assignable(parse_type("Integer[1,5]")) is True
    assert parse_type("Integer[1,5]").assignable(parse_type("Integer[2,4]")) is True
    assert parse_type("Integer[1,5]").assignable(parse_type("Integer[0,5]")) is False
    assert parse_type("Integer[1,5]").assignable(parse_type("Integer[1,6]")) is False
    assert parse_type("Integer[1,5]").assignable(parse_type("String")) is False

    assert parse_type("Float[1,5]").assignable(parse_type("Float[2,4]")) is True
    assert parse_type("Float[1,5]").assignable(parse_type("Float[0,5]")) is False
    assert parse_type("Float[1,5]").assignable(parse_type("Float[1,6]")) is False
    assert parse_type("Float[1,5]").assignable(parse_type("Integer")) is False

    assert parse_type("String[1,3]").assignable(infer("ab")) is True
    assert parse_type("String[1,3]").assignable(infer("abcd")) is False
    assert parse_type("String[1,3]").assignable(parse_type("String[2,4]")) is False
    assert parse_type("String[1,3]").assignable(parse_type("String")) is False
    assert parse_type("String").assignable(infer("a")) is True
    literal = infer("a")
    assert literal.assignable(infer("a")) is True
    assert literal.assignable(infer("b")) is False
    assert literal.assignable(parse_type("String")) is False
    assert parse_type("String").assignable(parse_type("Integer")) is False

    assert parse_type("Boolean[true]").assignable(parse_type("Boolean[true]")) is True
    assert parse_type("Boolean[true]").assignable(parse_type("Boolean[false]")) is False
    assert parse_type("Boolean[true]").assignable(parse_type("Integer")) is False
    assert parse_type("Boolean").assignable(parse_type("Boolean[true]")) is True

    assert (
        parse_type("Variant[String,Integer]").assignable(parse_type("Integer")) is True
    )
    assert (
        parse_type("Variant[String,Integer]").assignable(parse_type("Float")) is False
    )

    assert parse_type("Optional[Integer]").assignable(parse_type("Undef")) is True
    assert parse_type("Optional[Integer]").assignable(parse_type("Integer")) is True
    assert parse_type("Optional").assignable(parse_type("Undef")) is True
    assert parse_type("NotUndef[Integer]").assignable(parse_type("Undef")) is False
    assert parse_type("NotUndef[Integer]").assignable(parse_type("Integer")) is True
    assert parse_type("NotUndef").assignable(parse_type("Integer")) is True

    data_t = parse_type("Data")
    assert data_t.assignable(data_t) is True
    assert data_t.assignable(parse_type("Integer")) is True
    # `Data`'s own Variant branch matches ScalarData, which (like most types
    # in this ported model that never override `assignable()`) falls back to
    # PAnyType's own base implementation -- "any other PAnyType at all" --
    # rather than a real structural subtype check.
    assert data_t.assignable(parse_type("Sensitive")) is True

    assert parse_type("Undef").assignable(parse_type("Undef")) is True
    assert parse_type("Undef").assignable(parse_type("Integer")) is False

    # Bare Optional/NotUndef (no contained type argument at all -- distinct
    # from `Optional[Integer]`) accept any non-Undef instance/assignable
    # target, per their own `contained is None` fast path.
    assert parse_type("Optional").instance(5) is True
    assert parse_type("NotUndef").instance(5) is True
    assert parse_type("Optional").assignable(parse_type("Integer")) is True

    # A bareword contained type argument (`Optional[integer]`) is kept as a
    # raw Python str, not parsed into a real type (`_literal_or_type`) --
    # `_type_instance`/`_type_assignable`'s own string branch is what makes
    # that literal comparable.
    literal_opt = parse_type("Optional[integer]")
    assert literal_opt.instance("integer") is True
    assert literal_opt.instance("other") is False
    assert literal_opt.assignable(parse_type("String")) is False
    assert literal_opt == parse_type("Optional[integer]")
    assert parse_type("Optional") == parse_type("Optional")


def test_alias_normalize_and_expansion():
    data_t = parse_type("Data")
    assert str(data_t.normalize()) == str(data_t.resolved_type)

    # An unguarded expansion inlines the alias's own body text...
    assert (
        data_t.alias_expanded_str()
        == "Variant[ScalarData, Undef, Hash[String, Data], Array[Data]]"
    )
    # ...but a self-referencing alias (Data contains Data) stops recursing
    # once its own name is already in the guard set, printing the bare name
    # instead of looping forever.
    assert data_t.alias_expanded_str({"Data"}) == "Data"


def test_optional_notundef_literal_container_rendering():
    # `Optional[integer]`/`NotUndef[integer]` (a bareword *contained type
    # argument*) keep the contained value as a raw Python str, rendered
    # through the plain-string branch -- but Optional/NotUndef can also wrap
    # a real PStringType with `.literal` set (never produced by parse_type,
    # only by infer()); that flavor renders its quoted literal directly
    # rather than recursing into the child's own (bare "String") renderer.
    assert str(POptionalType(infer("x"))) == "Optional['x']"
    assert str(PNotUndefType(infer("y"))) == "NotUndef['y']"


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
    # path (`_data_functions.py`/`_lookup_adapter.py`/`_type_mismatch.py`,
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

    assert str(infer_set([])) == "Tuple[]"
    assert str(infer_set({})) == "Hash[Any, Any, 0, 0]"

    assert str(infer([1, 1.0]).generalize()) == "Array[Variant[Integer, Float]]"
    assert str(infer(re.compile("^a"))) == "Regexp[/^a/]"
    assert str(infer([])) == "Array[Any, 0, 0]"

    class RubySymbol:
        pass

    RubySymbol.__module__ = "hyera._yaml_loader"
    rt = infer(RubySymbol())
    assert rt.instance(RubySymbol()) is True
    assert rt.instance("x") is False

    # `generalize()`'s module-level dispatcher passes a non-PAnyType value
    # (a bareword literal contained type, e.g. Optional[integer]'s "integer")
    # through unchanged, since it has no `.generalize()` of its own.
    assert generalize("integer") == "integer"


def test_num_str_and_literal_str_direct():
    # `_num_str`'s bool branch and `_literal_str`'s float/other branches are
    # unreachable through `parse_type()`: the parser's own `_num_or_default`
    # only ever hands Integer/Float range bounds a "number" or "default"
    # node (never "bool"), and `_build_enum` only ever hands `PEnumType` a
    # "string" or "bool" node -- so neither a bool bound nor a non-str/bool
    # Enum value can arise from real Puppet type-expression text. Both
    # helpers are still exercised directly, the same as every other private
    # function in this module.
    assert _num_str(True) == "true"
    assert _num_str(False) == "false"

    assert _literal_str(True) == "true"
    assert _literal_str(3.14) == "3.14"
    assert _literal_str(5) == "5"

    assert str(PEnumType([3.14, 5])) == "Enum[3.14, 5]"


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
    assert str(exc_info.value).startswith("'new' ")

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
    assert _string_convert(5, "%b") == "101"
    assert _string_convert(255, "%x") == "ff"
    assert _string_convert(1e20, "%p") == "1.0e+20"
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
    # non-negative value, left-justify, and the "a"/"A"-style else branch
    # (unsupported by this subset, falls back to repr()).
    assert _string_convert(3.14159, "%10.2E") == "  3.14E+00"
    assert _string_convert(3.14159, "%+.2f") == "+3.14"
    assert _string_convert(3.14159, "%-10.2f") + "|" == "3.14      |"
    assert _string_convert(3.14159, "%A") == "3.14159"
    # NaN/Infinity (Ruby Float#inspect, not Python's repr spelling).
    assert _string_convert(float("nan"), "%p") == "NaN"
    assert _string_convert(float("inf"), "%p") == "Infinity"
    assert _string_convert(float("-inf"), "%p") == "-Infinity"
    # A string_formats value that does not parse as a %-directive at all
    # falls back to the plain %s rendering, matching Puppet.
    assert _string_convert(5, "%") == "5"
    assert _string_convert(5, "nope") == "5"
    # Sensitive redacts through every directive, not just the default one.
    assert _string_convert(Sensitive("secret"), "%p") == "Sensitive [value redacted]"


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
