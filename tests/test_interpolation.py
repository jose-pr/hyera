"""Interpolation edge cases: literal backslashes, regex-special values, format()."""

import logging

import pytest

from hyera import ConfigError, Hiera, InterpolationError, Scope, Sensitive
from hyera._interpolation import (
    _float_to_s,
    _ruby_inspect,
    _to_puppet_str,
    interpolate,
    unshare,
)
from hyera._invocation import Invocation
from hyera._navigation import _MISSING
from hyera.cli import main as _cli_main


def _error_records(caplog):
    return [
        r for r in caplog.records if r.name == "hyera" and r.levelno == logging.ERROR
    ]


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
    stored = h.lookup("winpath")
    assert stored == "C:\\data\\sub"  # sanity: one backslash each
    assert h.lookup("ref") == stored  # interpolation preserves it verbatim


def test_scope_value_with_group_ref_is_literal(make_tree):
    # A scope value containing "\g<0>" must not be treated as a group ref.
    h = _hiera(make_tree, 'msg: "got %{token}"\n', token=r"\g<0>")
    assert h.lookup("msg") == r"got \g<0>"


def test_missing_scope_interpolates_empty(make_tree):
    h = _hiera(make_tree, 'msg: "[%{absent}]"\n')
    assert h.lookup("msg") == "[]"


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


def test_method_syntax_not_allowed():
    # allow_methods=False (used for hierarchy locations) still allows a
    # plain %{var} reference; only an explicit method call raises -- as a
    # ConfigError, since this is a hiera.yaml problem, not a lookup-time one.
    inv = Invocation(Scope(), lambda k, i: _MISSING)
    with pytest.raises(ConfigError, match="method syntax is not allowed"):
        interpolate("%{lookup('x')}", inv, allow_methods=False)


def test_undefined_variable_same_for_both_forms(make_tree, caplog):
    h = _hiera(make_tree, 'a: "[%{nosuch}]"\nb: "[%{scope(\'nosuch\')}]"\n')

    with caplog.at_level(logging.WARNING):
        assert h.lookup("a") == "[]"
        assert h.lookup("b") == "[]"
    assert any("Undefined variable 'nosuch'" in r.getMessage() for r in caplog.records)

    strict_h = h.scoped(strict="error")
    with pytest.raises(InterpolationError, match="Undefined variable 'nosuch'"):
        strict_h.lookup("a")
    with pytest.raises(InterpolationError, match="Undefined variable 'nosuch'"):
        strict_h.lookup("b")


def test_lenient_invocation_warns(caplog):
    inv = Invocation(Scope(strict="error"), lambda k, i: _MISSING, lenient=True)
    with caplog.at_level(logging.WARNING):
        assert interpolate("[%{nosuch}]", inv) == "[]"
    assert any(
        "Interpolation failed with 'nosuch', but compilation continuing"
        in r.getMessage()
        for r in caplog.records
    )


def test_cli_recursion_exits_2(make_tree, caplog):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "rec: \"%{lookup('rec')}\"\n"},
    )
    rc = _cli_main(["rec", "-c", str(root / "hiera.yaml")])
    assert rc == 2
    assert (
        "Recursive lookup detected in [rec]" in _error_records(caplog)[-1].getMessage()
    )


def test_anchor_interpolated_once_and_shared(make_tree):
    h = _hiera(
        make_tree,
        'lst: &l ["a", "%{k}"]\nb: [*l, *l]\n',
        k="v",
    )
    v = h.lookup("b")
    assert v == [["a", "v"], ["a", "v"]]
    assert v[0] is v[1]

    v[0].append("z")
    v2 = h.lookup("b")
    assert v2 == [["a", "v"], ["a", "v"]]


def test_unshare_copies_every_position():
    x = [1]
    u = unshare({"p": x, "q": x})
    assert u == {"p": [1], "q": [1]}
    assert u["p"] is not u["q"]
    assert u["p"] is not x


def _format_hiera(make_tree):
    return _hiera(
        make_tree,
        "k: v\narr: [x, y]\n",
        num=42,
        flag=False,
        n=0,
        l=["a", "b"],
    )


def test_format_missing_variable_follows_strict(make_tree):
    h = _format_hiera(make_tree)
    assert h.format("[%{absent}]") == "[]"
    with pytest.raises(InterpolationError, match="Undefined variable 'absent'"):
        h.scoped(strict="error").format("[%{absent}]")


def test_format_leaves_literal_braces(make_tree):
    h = _format_hiera(make_tree)
    assert h.format('json {"a": 1} %{num}') == 'json {"a": 1} 42'


def test_format_methods(make_tree):
    h = _format_hiera(make_tree)
    assert (
        h.format("%{lookup('k')}|%{hiera('k')}|%{literal('%')}|%{ num }") == "v|v|%|42"
    )


def test_format_renders_like_puppet(make_tree):
    h = _format_hiera(make_tree)
    assert h.format("%{flag} %{n} %{l}") == 'false 0 ["a", "b"]'


def test_format_whole_alias_returns_value(make_tree):
    h = _format_hiera(make_tree)
    assert h.format("%{alias('arr')}") == ["x", "y"]


def test_format_rejects_non_str(make_tree):
    h = _format_hiera(make_tree)
    with pytest.raises(TypeError):
        h.format(5)
