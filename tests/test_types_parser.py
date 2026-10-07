"""The Puppet type-expression parser: goldens, grammar and syntax errors."""

import re

import pytest
import yaml

from hyera import HieraLookupError
from hyera._types.inference import infer, infer_set
from hyera._types.parser import parse_type
from hyera._types.type_syntax import _Parser
from hyera._types.compound_types import TypeReference
from types_support import (  # noqa: F401
    _CASES,
    _data,
    _golden,
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


# parser grammar/errors beyond the golden-driven test: array/hash literals, quoted
# string escapes, unary minus, `default` size bounds and every builder's argument
# count/shape error.


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
        == "hyera does not support the Puppet type 'Iterable[Integer]'"
    )

    with pytest.raises(HieraLookupError) as exc_info:
        parse_type("SemVer[1,2]")
    assert str(exc_info.value) == "hyera does not support the Puppet type 'SemVer[1,2]'"

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
    # Callers only advance past a non-"eof" token, so advance() on an eof token (a
    # backstop against an out-of-range `self.pos`) is unreachable through
    # `parse_type()`; exercised directly.
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

    from hyera._types import type_syntax as syntax_module

    a_text, b_text = "Aaaaaaaaa1[1]", "Bbbbbbbbbbbbbbbb2[2]"
    a_built, b_done = threading.Event(), threading.Event()
    original = syntax_module._Parser.parse_primary
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

    monkeypatch.setattr(syntax_module._Parser, "parse_primary", parse_primary)
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


def test_type_nesting_past_the_bound_is_a_lookup_error():
    text = "Array[" * 400 + "Integer" + "]" * 400
    with pytest.raises(HieraLookupError, match="nested more than 200 levels"):
        parse_type(text)
    assert str(parse_type("Array[" * 150 + "Integer" + "]" * 150)).count("Array") == 150


@pytest.mark.parametrize(
    "text, value, expected",
    [
        ("Integer[1.0, 50]", 1, True),
        ("Integer[1.0, 50]", 0, False),
        ("Integer[1.0, 50]", 51, False),
        ("Integer[1.0, 50]", 1.0, False),
        ("Integer[40.5, 50]", 40, False),
        ("Integer[40.5, 50]", 41, True),
        ("Integer[1e1, 50]", 9, False),
        ("Integer[1e1, 50]", 10, True),
        ("Integer[1, 50.5]", 50, True),
        ("Integer[1, 50.5]", 51, False),
        ("Integer[default, 1.5]", 1, True),
        ("Integer[default, 1.5]", 2, False),
        ("Integer[undef, 2]", -5, True),
        ("Integer[undef, 2]", 3, False),
        ("Hash[0, 0]", {}, True),
        ("Hash[0, 0]", {"a": 1}, False),
        ("Hash[1, 2]", {"a": 1}, False),
        ("Hash[-1, 2]", {}, True),
        ("Hash[String, Integer, Integer[2]]", {"a": 1, "b": 2}, True),
        ("Hash[String, Integer, Integer[2]]", {"a": 1}, False),
        ("Array[1, 2]", [], False),
        ("Array[1, 2]", ["x"], False),
        ("Array[String, Integer[2, 2]]", ["x", "y"], True),
        ("Array[String, Integer[2, 2]]", ["x"], False),
        ("String[Integer[1, 5]]", "abc", True),
        ("String[Integer[1, 5]]", "", False),
        ("Collection[Integer[1, 2]]", [1], True),
        ("Collection[Integer[1, 2]]", [], False),
    ],
)
def test_range_parameters_read_as_puppets_type_parser_reads_them(text, value, expected):
    assert parse_type(text).instance(value) is expected


@pytest.mark.parametrize(
    "text",
    [
        "Integer[1.5]",
        "Integer[1e1]",
        "Hash[1.5, 2]",
        "Hash[Integer, 2]",
        "Hash[String, Integer, 1.5]",
        "Array[String, undef]",
        "Array[1, Integer[1, 2]]",
        "String[1.0, 5]",
        "Collection[1.0, 2]",
    ],
)
def test_range_parameters_puppet_refuses_stay_refused(text):
    with pytest.raises(HieraLookupError, match="not a valid type specification"):
        parse_type(text)


def test_a_float_bound_keeps_its_float_text():
    assert str(parse_type("Integer[1e1, 50]")) == "Integer[10.0, 50]"
    assert str(parse_type("Integer[1.0, 2.5]")) == "Integer[1.0, 2.5]"
