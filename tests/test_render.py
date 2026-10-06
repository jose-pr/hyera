"""Puppet-shaped ``s``/``json``/``yaml`` CLI output rendering.

Byte-exact against real Puppet 8 oracle measurements. Every value is a
plain Python literal (no fixtures): rendering does not touch the
filesystem.
"""

import itertools
import json

import pytest
import yaml

from hyera import Sensitive
from hyera.backends import Backend, YAMLBackend


def _render(fmt, value):
    return Backend.new(fmt, kind="render").dumps(value)


_CASES = {
    "str": ("one", "one", "--- one", '"one"'),
    "bool_t": (True, "true", "--- true", "true"),
    "bool_f": (False, "false", "--- false", "false"),
    "int0": (0, "0", "--- 0", "0"),
    "flt": (1.5, "1.5", "--- 1.5", "1.5"),
    "f": (1.0, "1.0", "--- 1.0", "1.0"),
    "big": (1e20, "1.0e+20", "--- 1.0e+20", "1e+20"),
    "nil": (None, "", "---", "null"),
    "empty_str": ("", "", "--- ''", '""'),
    "h": (
        {"a": 1, "b": ["x", True, None], "c": {"d": "e"}},
        '{"a"=>1, "b"=>["x", true, nil], "c"=>{"d"=>"e"}}',
        "---\na: 1\nb:\n- x\n- true\n-\nc:\n  d: e",
        '{"a":1,"b":["x",true,null],"c":{"d":"e"}}',
    ),
    "arr": (
        [1, "two", {"k": "v"}],
        '[1, "two", {"k"=>"v"}]',
        "---\n- 1\n- two\n- k: v",
        '[1,"two",{"k":"v"}]',
    ),
    "nested": (
        {"b": 2, "a": [1, {"z": 1, "y": 2}]},
        '{"b"=>2, "a"=>[1, {"z"=>1, "y"=>2}]}',
        '---\nb: 2\na:\n- 1\n- z: 1\n  "y": 2',
        '{"b":2,"a":[1,{"z":1,"y":2}]}',
    ),
    "mixedkeys": (
        {1: "one", "b": "two"},
        '{1=>"one", "b"=>"two"}',
        "---\n1: one\nb: two",
        '{"1":"one","b":"two"}',
    ),
    "nullinlist": (
        [1, None, 3],
        "[1, nil, 3]",
        "---\n- 1\n-\n- 3",
        "[1,null,3]",
    ),
    "unicode": ("café ☃", "café ☃", "--- café ☃", '"café ☃"'),
    "multiline": (
        "line1\nline2",
        "line1\nline2",
        "--- |-\n  line1\n  line2",
        '"line1\\nline2"',
    ),
    "quoted": ('say "hi"', 'say "hi"', '--- say "hi"', '"say \\"hi\\""'),
    "ctl": ("a\x01b", "a\x01b", '--- "a\\x01b"', '"a\\u0001b"'),
    "yes_str": ("yes", "yes", "--- 'yes'", '"yes"'),
    "numstr": ("42", "42", "--- '42'", '"42"'),
    "emptyh": ({}, "{}", "--- {}", "{}"),
    "emptya": ([], "[]", "--- []", "[]"),
    "secret": (
        Sensitive("hunter2"),
        "Sensitive [value redacted]",
        "--- Sensitive [value redacted]",
        '"Sensitive [value redacted]"',
    ),
    "secret_hash": (
        Sensitive({"user": "admin", "pass": "hunter2"}),
        "Sensitive [value redacted]",
        "--- Sensitive [value redacted]",
        '"Sensitive [value redacted]"',
    ),
}

_KEYS = list(_CASES)


@pytest.mark.parametrize("key", _KEYS)
def test_render_s(key):
    value, s_text, _yaml_text, _json_text = _CASES[key]
    assert _render("s", value) == s_text


@pytest.mark.parametrize("key", _KEYS)
def test_render_yaml(key):
    value, _s_text, yaml_text, _json_text = _CASES[key]
    assert _render("yaml", value) == yaml_text + "\n"


def test_render_yaml_tuple_renders_as_a_sequence():
    # A tuple never comes from real hiera data (parsed containers are
    # always list/dict), but the YAML dumper still needs a representer for
    # one -- registered and exercised directly.
    assert _render("yaml", (1, 2, 3)) == "---\n- 1\n- 2\n- 3\n"


@pytest.mark.parametrize("key", _KEYS)
def test_render_json(key):
    value, _s_text, _yaml_text, json_text = _CASES[key]
    assert _render("json", value) == json_text


@pytest.mark.parametrize(
    "value,text",
    [
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (float("-inf"), "-Infinity"),
    ],
)
def test_render_s_nonfinite(value, text):
    assert _render("s", value) == text


@pytest.mark.parametrize(
    "value,expected",
    [
        (float("nan"), "--- .nan\n"),
        (float("inf"), "--- .inf\n"),
        (float("-inf"), "--- -.inf\n"),
    ],
)
def test_render_yaml_nonfinite(value, expected):
    assert _render("yaml", value) == expected


@pytest.mark.parametrize(
    "value,message",
    [
        (float("nan"), "NaN not allowed in JSON"),
        (float("inf"), "Infinity not allowed in JSON"),
        (float("-inf"), "-Infinity not allowed in JSON"),
    ],
)
def test_render_json_nonfinite(value, message):
    with pytest.raises(ValueError, match=message):
        _render("json", value)


def test_render_yaml_psych_quoting():
    value = {"y": 1, "n": 2, ":sym": 3, "k": ":x"}
    assert _render("yaml", value) == '---\n"y": 1\n"n": 2\n":sym": 3\nk: ":x"\n'


_ROUND_TRIP_STRINGS = [
    "plain",
    "yes",
    "no",
    "y",
    "Y",
    "n",
    "N",
    ":sym",
    ":x",
    "~",
    "- a",
    "#c",
    " lead",
    "=",
    "@x",
    "<<",
    "x'y",
    "42",
    "1.5",
    "true",
    "false",
    "null",
    "",
    "line1\nline2",
    "café ☃",
    "a\x01b",
    'say "hi"',
    "a: b",
    "[a]",
    "{a}",
    "a,b",
]


def test_render_yaml_round_trips():
    for s in _ROUND_TRIP_STRINGS:
        rendered = _render("yaml", {s: s})
        assert yaml.safe_load(rendered) == {s: s}


_YAML_EDGE_STRINGS = [
    "1,000",
    "1,000.5",
    "1,2,3",
    "12,345,678",
    "+1,000",
    "2001-1-1",
    "2001-01-01",
    "2001-01-01 10:00:00 +0100",
    "2001-01-01T10:00:00Z",
    ".Nan",
    ".NaN",
    ".Inf",
    "-.Inf",
    "+.INF",
    "+.5",
    ".5",
    "0.",
    "1.",
    "08",
    "0888",
    "0o17",
    "0b101",
    "0x1F",
    "1_000",
    "1e3",
    "1.5e3",
    "1:30",
    "190:20:30",
    "1:30.5",
    "a\x85b",
    "\x85",
    "a b",
    "a b",
    "line\nbreak\x85",
    "a\rb",
    "a\tb",
    " ",
    "  lead",
    "trail  ",
    "a  b",
    "a #b",
    "a: b",
    "? q",
    "- x",
    "! tag",
    "& anchor",
    "* alias",
    "| pipe",
    "> fold",
    "%percent",
    "`tick",
    "﻿a",
    "a￾b",
    "~",
    "null",
    "Null",
    "NULL",
    "true",
    "True",
    "off",
    "On",
    "y",
    "N",
    "-",
    "?",
    ":",
    "::",
    ":x",
    ":'x'",
    "=",
    "<<",
]


def _yaml_corpus():
    """About 2,000 strings: the edge cases above and every string of up to
    three characters over an alphabet of the characters Psych's scalar
    scanner treats specially."""
    corpus = list(_YAML_EDGE_STRINGS) + _ROUND_TRIP_STRINGS
    alphabet = "01.,_:-+eExyn~# '\"\n\x85"
    corpus.extend(
        "".join(chars)
        for size in (1, 2, 3)
        for chars in itertools.product(alphabet, repeat=size)
    )
    return corpus


def test_render_yaml_every_string_reads_back_through_the_psych_reader():
    reader = YAMLBackend()
    unread = []
    for text in _yaml_corpus():
        for value in (text, [text], {"k": text}, {text: 1}):
            rendered = _render("yaml", value)
            if reader.loads(rendered) != value:
                unread.append((value, rendered))
    assert unread == []


def test_render_yaml_nested_values_read_back():
    value = {
        "s": ["1,000", "2001-1-1", ".Nan", "", "a b"],
        "n": [None, True, False, 0, -3, 2.5],
        "h": {"x": {"y": [[], {}, {"z": "yes"}]}},
        "": "empty key",
    }
    assert YAMLBackend().loads(_render("yaml", value)) == value


def test_render_yaml_shared_values_have_no_aliases():
    shared = [1, 2]
    text = _render("yaml", {"a": shared, "b": shared, "c": shared})
    assert "&" not in text and "*" not in text
    assert YAMLBackend().loads(text) == {"a": [1, 2], "b": [1, 2], "c": [1, 2]}


def test_render_yaml_empty_string_key_is_a_simple_key():
    assert _render("yaml", {"": 1}) == "---\n'': 1\n"


@pytest.mark.parametrize("fmt", ["s", "json", "yaml"])
@pytest.mark.parametrize("digits", [4300, 4301, 5000, 20000])
def test_render_prints_an_integer_of_any_length(fmt, digits):
    number = 10 ** (digits - 1) + 7
    text = "1" + "0" * (digits - 2) + "7"
    for value, expected in ((number, text), (-number, "-" + text)):
        rendered = _render(fmt, {"k": value})
        assert expected in rendered
    assert _render(fmt, {number: 1}).count(text) == 1


def test_render_json_rejects_non_data():
    with pytest.raises(TypeError):
        _render("json", {1, 2, 3})


def test_render_names():
    assert Backend.names("render") == ["s", "json", "yaml"]
