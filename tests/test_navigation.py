"""``hyera._navigation``: Puppet's dotted-key ``split_key``/``sub_lookup``.

Behavioral acceptance for these two functions runs through the
``dotted-navigation`` conformance case; this module unit-tests the parser
and the walker directly, since ``puppet lookup`` output alone cannot show
which internal rule applied (Design Q3).
"""

import re

import pytest

from hyera.exceptions import HieraLookupError
from hyera._navigation import _MISSING, parse_lookup_key, split_key, sub_lookup


def _make_error(problem):
    return HieraLookupError(problem)


@pytest.mark.parametrize(
    "key,expect",
    [
        ("a", ["a"]),
        (" a ", [" a "]),
        ("a.b.c", ["a", "b", "c"]),
        ("a . b . c", ["a", "b", "c"]),
        ("'a.b'.c", ["a.b", "c"]),
        ('"a.b".c', ["a.b", "c"]),
        ('" sp".c', [" sp", "c"]),
        ("lst.1", ["lst", 1]),
        ("lst.+1", ["lst", 1]),
        ("lst.01", ["lst", 1]),
        ("lst.-1", ["lst", -1]),
        ("lst.:1", ["lst", 0]),
        ('lst."1"', ["lst", "1"]),
        ("0.x", [0, "x"]),
        ("a.'b'", ["a", "b"]),
        ("mod::k.x", ["mod::k", "x"]),
    ],
    ids=[
        "bare",
        "bare-untrimmed",
        "nested",
        "spaced-dots",
        "single-quoted-root",
        "double-quoted-root",
        "quoted-space",
        "list-index",
        "list-index-plus",
        "list-index-leading-zero",
        "list-index-negative",
        "list-index-colon",
        "quoted-digit-segment",
        "integer-root",
        "single-quoted-tail",
        "qualified-root",
    ],
)
def test_split_key(key, expect):
    assert split_key(key, _make_error) == expect


@pytest.mark.parametrize(
    "key",
    ["a..b", "a.", ".a", "a.'b", "'a", 'a.""'],
    ids=[
        "double-dot",
        "trailing-dot",
        "leading-dot",
        "unbalanced-single",
        "lone-single-quote",
        "empty-double-quoted",
    ],
)
def test_split_key_syntax_error(key):
    with pytest.raises(HieraLookupError, match="Syntax error"):
        split_key(key, _make_error)


@pytest.mark.parametrize(
    "segments,value,expect",
    [
        (["x"], {"x": 1}, 1),
        ([1], ["a", "b", "c"], "b"),
        ([-1], ["a", "b", "c"], _MISSING),
        ([9], ["a", "b", "c"], _MISSING),
        (["x"], None, _MISSING),
        ([0], {0: "zero"}, "zero"),
        ([0], {"0": "s"}, _MISSING),
        (["0"], {0: "zero"}, _MISSING),
        ([1], {True: "t"}, _MISSING),
        ([1], {1.0: "f"}, _MISSING),
        (["b", "c"], {"b": {"c": "n"}}, "n"),
        (["b", "x"], {"b": None}, _MISSING),
    ],
    ids=[
        "hash-hit",
        "array-index",
        "array-negative-index",
        "array-out-of-range",
        "nil-value",
        "int-key-hit",
        "int-key-vs-string-segment",
        "string-key-vs-int-segment",
        "bool-key-vs-int-segment",
        "float-key-vs-int-segment",
        "nested-hit",
        "nested-miss-on-nil",
    ],
)
def test_sub_lookup(segments, value, expect):
    got = sub_lookup("k", segments, value)
    if expect is _MISSING:
        assert got is _MISSING
    else:
        assert got == expect


@pytest.mark.parametrize(
    "value,segment,ruby_class",
    [
        ("hello", "x", "String"),
        (["a"], "x", "Array"),
        ("hello", 0, "String"),
        (5, "x", "Integer"),
        (1.5, "x", "Float"),
        (True, "x", "TrueClass"),
        (False, "x", "FalseClass"),
    ],
    ids=["string", "array", "string-int-segment", "integer", "float", "true", "false"],
)
def test_sub_lookup_mismatch(value, segment, ruby_class):
    with pytest.raises(HieraLookupError) as exc_info:
        sub_lookup("k", [segment], value)
    message = str(exc_info.value)
    assert ruby_class in message
    if value == "hello" and segment == "x":
        assert message == (
            "Data Provider type mismatch: Got String when a hash-like "
            "object was expected to access value using 'x' from key 'k'"
        )


@pytest.mark.parametrize(
    "key,expect",
    [("a", ("a", ())), ("a.b.0", ("a", ("b", 0)))],
    ids=["bare", "nested"],
)
def test_parse_lookup_key(key, expect):
    assert parse_lookup_key(key) == expect


@pytest.mark.parametrize("key", ["a..b", "0.x"], ids=["syntax-error", "integer-root"])
def test_parse_lookup_key_syntax_error(key):
    expect = "Syntax error in key: '{}'".format(key)
    with pytest.raises(HieraLookupError, match=re.escape(expect)):
        parse_lookup_key(key)
