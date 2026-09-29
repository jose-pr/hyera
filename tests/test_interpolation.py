"""Interpolation edge cases: literal backslashes, regex-special values, format()."""

import pytest

from hyera import Hiera, Scope, Sensitive
from hyera._interpolation import _float_to_s, _ruby_inspect, _to_puppet_str


def _hiera(make_tree, common, **variables):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": common},
    )
    scope = Scope(variables=variables) if variables else None
    return Hiera(str(root / "hiera.yaml"), scope=scope)


def test_value_with_backslash_is_literal(make_tree):
    # An interpolated value is inserted literally, never treated as a re.sub
    # replacement template, so a backslash (e.g. a Windows path) is preserved
    # as-is. Use a YAML double-quoted scalar so the stored value has exactly
    # ONE backslash per separator: C:\data\sub
    h = _hiera(
        make_tree,
        'winpath: "C:\\\\data\\\\sub"\n' "ref: \"%{hiera('winpath')}\"\n",
    )
    stored = h.get("winpath")
    assert stored == "C:\\data\\sub"  # sanity: one backslash each
    assert h.get("ref") == stored  # interpolation preserves it verbatim


def test_scope_value_with_group_ref_is_literal(make_tree):
    # A scope value containing "\g<0>" must not be treated as a group ref.
    h = _hiera(make_tree, 'msg: "got %{token}"\n', token=r"\g<0>")
    assert h.get("msg") == r"got \g<0>"


def test_missing_scope_interpolates_empty(make_tree):
    h = _hiera(make_tree, 'msg: "[%{absent}]"\n')
    assert h.get("msg") == "[]"


def test_format_uses_bound_scope(make_tree):
    # Hiera.format resolves against the instance's bound scope.
    # "name" is now a reserved, always-"main" top-scope variable, so this
    # uses an ordinary variable name instead.
    h = _hiera(make_tree, "x: 1\n", who="bob")
    assert h.format("hi %{who}") == "hi bob"


# Ruby Float#to_s: fixed notation for a scientific exponent of -4..14 (and 15
# only when the shortest round-trip digits run past the point), scientific
# otherwise. Measured against Ruby 4.0.7 over 8,291 floats.
@pytest.mark.parametrize(
    "value,want",
    [
        (1e20, "1.0e+20"),
        (1e-05, "1.0e-05"),
        (1e15, "1.0e+15"),
        (-1e15, "-1.0e+15"),
        (1.5e15, "1.5e+15"),
        (999999999999999.0, "999999999999999.0"),
        (1234567890123456.8, "1234567890123456.8"),
        (1e14, "100000000000000.0"),
        (0.0001, "0.0001"),
        (0.00012, "0.00012"),
        (1.25e-05, "1.25e-05"),
        (100.0, "100.0"),
        (1.5, "1.5"),
        (-0.0, "-0.0"),
        (5e-324, "5.0e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (0.1 + 0.2, "0.30000000000000004"),
        (2.0**53, "9.007199254740992e+15"),
        (float("inf"), "Infinity"),
    ],
)
def test_float_to_s(value, want):
    assert _float_to_s(value) == want


def test_float_to_s_nan():
    assert _float_to_s(float("nan")) == "NaN"


# Ruby String#inspect: a double-quoted, escaped rendering. Named control
# escapes get their short mnemonic; "#" before "{"/"$"/"@" is escaped since
# Ruby would otherwise read it as interpolation syntax; the rest of C0, DEL,
# C1 and U+2028/9 become \uXXXX; everything else -- including non-ASCII text
# outside those ranges -- is left raw.
@pytest.mark.parametrize(
    "value,want",
    [
        ("a\\b", '"a\\\\b"'),
        ('q"b', '"q\\"b"'),
        ("\t\n\r\f\v\b\a\x1b", '"\\t\\n\\r\\f\\v\\b\\a\\e"'),
        ("x#{y} x#$y x#@z # #x", '"x\\#{y} x\\#$y x\\#@z # #x"'),
        ("\x00\x7f\x85", '"\\u0000\\u007F\\u0085"'),
        ("\xa0​﻿\xe9\U0001f600", '"\xa0​﻿\xe9\U0001f600"'),
        (" ", '"\\u2028"'),
        ("", '""'),
    ],
)
def test_ruby_inspect_string(value, want):
    assert _ruby_inspect(value) == want


def test_render_values():
    assert (
        _ruby_inspect([1, "a", None, True, 1e20, [], {}])
        == '[1, "a", nil, true, 1.0e+20, [], {}]'
    )
    assert (
        _ruby_inspect({1: "a", "b": [None], "c": {}}) == '{1=>"a", "b"=>[nil], "c"=>{}}'
    )
    assert _to_puppet_str(None) == ""
    assert _to_puppet_str(Sensitive("x")) == "Sensitive [value redacted]"
    assert _to_puppet_str([Sensitive("x")]) == "[#<Sensitive [value redacted]>]"
