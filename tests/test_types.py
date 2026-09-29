"""Golden-driven tests for the Puppet type model and parser.

Reads ``tests/conformance/cases/convert-type-parse`` and
``.../convert-value-type`` directly (Puppet-recorded ground truth), never
through the lookup API -- ``value_type``/``--type`` wiring is a later
plan's job (parent plan Design Q11).
"""

import json
import re
from pathlib import Path

import pytest
import yaml

from pyera import HieraLookupError
from pyera._type_mismatch import assert_instance_of
from pyera._type_parser import parse_type
from pyera._types import ALIASES, PTypeReferenceType, infer, infer_set

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
    from pyera import Sensitive

    assert rich_t.instance(Sensitive("x")) is True

    ref = parse_type("Stdlib::Port")
    assert isinstance(ref, PTypeReferenceType)
    assert str(ref) == "TypeReference['Stdlib::Port']"
    assert ref.instance("80") is False

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("Iterable")
    assert "hiera does not support the Puppet type 'Iterable'" == str(exc_info.value)

    # infer_set of a RubySymbol-named object renders Runtime[ruby, 'Symbol'].
    class RubySymbol:
        pass

    RubySymbol.__module__ = "pyera.backends"
    assert str(infer_set(RubySymbol())) == "Runtime[ruby, 'Symbol']"


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
#: instance mismatch -- Phase 1's, already proved by test_parse_matches_golden.
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
