"""Interpolation edge cases: literal backslashes, regex-special values, format()."""

import logging

import pytest

from hyera import ConfigError, Hiera, InterpolationError, Scope, Sensitive
from hyera._lookup.interpolation import (
    _float_to_s,
    _ruby_inspect,
    _to_puppet_str,
    interpolate,
    unshare,
)
from hyera._lookup.invocation import Invocation
from hyera._lookup.navigation import _MISSING
from hyera.cli import main as _cli_main


def _error_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "hyera.cli" and r.levelno == logging.ERROR
    ]


def _hiera(make_tree, common, **variables):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": common},
    )
    scope = Scope(variables=variables) if variables else None
    return Hiera(str(root / "hiera.yaml"), scope=scope)


@pytest.mark.parametrize(
    "text",
    ["a%{}b", "a%{::}b", 'a%{""}b', "a%{''}b", 'a%{"::"}b', "a%{'::'}b"],
)
def test_empty_interpolation_variants_resolve_to_empty_string(text):
    # An *empty* quoted/unquoted name (not sub-key syntax) always resolves
    # to "" without a scope lookup at all -- every spelling Puppet accepts.
    inv = Invocation(Scope(), lambda k, i: None)
    assert interpolate(text, inv) == "ab"


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
    # Both functions' own otherwise-unmodeled-type fallback (a RubySymbol,
    # or any other object neither renders specially): plain str().
    from hyera.backends._psych import RubySymbol

    assert _to_puppet_str(RubySymbol("x")) == ":x"
    assert _ruby_inspect(RubySymbol("x")) == ":x"


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


def test_recursive_lookup_raises_interpolation_error(make_tree):
    h = _hiera(make_tree, "rec: \"%{lookup('rec')}\"\n")
    with pytest.raises(
        InterpolationError, match=r"Recursive lookup detected in \[rec\]"
    ):
        h.lookup("rec")


def _chain(length, method="lookup"):
    """k0 -> k1 -> ... -> k<length>, each an interpolation of the next."""
    lines = ["k%d: \"%%{%s('k%d')}\"" % (i, method, i + 1) for i in range(length)]
    lines.append("k%d: end" % length)
    return "\n".join(lines) + "\n"


_ENTRY_POINTS = {
    "lookup": lambda h: h.lookup("k0"),
    "call": lambda h: h("k0"),
    "getitem": lambda h: h["k0"],
    "contains": lambda h: "k0" in h,
    "dig": lambda h: h.dig("k0"),
    "get": lambda h: h.get("k0"),
    "format": lambda h: h.format("%{lookup('k0')}"),
}


@pytest.mark.parametrize("method", ["lookup", "alias"])
@pytest.mark.parametrize("entry", sorted(_ENTRY_POINTS))
def test_a_chain_beyond_the_stack_raises_interpolation_error(make_tree, method, entry):
    h = _hiera(make_tree, _chain(300, method))
    with pytest.raises(InterpolationError, match="k0"):
        _ENTRY_POINTS[entry](h)


def test_explain_of_a_chain_beyond_the_stack_never_raises_recursion_error(make_tree):
    h = _hiera(make_tree, _chain(300))
    try:
        result = h.explain("k0")
    except InterpolationError:
        return
    assert isinstance(result.error, InterpolationError)


def test_a_value_type_nested_beyond_the_stack_raises_a_hiera_error(make_tree):
    h = _hiera(make_tree, "k: x\n")
    nested = "Array[" * 3000 + "String" + "]" * 3000
    with pytest.raises(InterpolationError):
        h.lookup("k", nested)


def test_cli_recursion_exits_2(make_tree, caplog):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "rec: \"%{lookup('rec')}\"\n"},
        facts={"role": "web"},
    )
    rc = _cli_main(
        [
            "rec",
            "--hiera_config",
            str(root / "hiera.yaml"),
            "--facts",
            str(root / "facts.yaml"),
        ]
    )
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


def test_shared_dict_anchor_interpolated_once():
    # _interpolate's own dict-shaped memo cache hit -- distinct from the
    # list-shaped one test_anchor_interpolated_once_and_shared above
    # already exercises.
    shared = {"x": "%{k}"}
    inv = Invocation(Scope(variables={"k": "v"}), lambda name, i: None)
    result = interpolate({"p": shared, "q": shared}, inv)
    assert result == {"p": {"x": "v"}, "q": {"x": "v"}}
    assert result["p"] is result["q"]


def test_interpolated_hash_key_unhashable_raises():
    # A hash key that interpolates (through alias(), which preserves the
    # looked-up value's own type instead of stringifying it) to something
    # Python can't hash -- Ruby has no such restriction, so this is this
    # port's own defensive check, not a Puppet-fidelity one.
    def sub_lookup(name, invocation):
        return [1, 2] if name == "arr" else None

    inv = Invocation(Scope(), sub_lookup)
    with pytest.raises(InterpolationError, match="not hashable"):
        interpolate({"%{alias('arr')}": "x"}, inv)


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


def test_format_embedded_alias_rejected(make_tree):
    # alias() is only permitted when it is the entire string, matching
    # Puppet's own restriction -- distinct from the already-tested
    # whole-string case just above.
    h = _format_hiera(make_tree)
    with pytest.raises(
        InterpolationError,
        match="'alias' interpolation is only permitted if the expression "
        "is equal to the entire string",
    ):
        h.format("x%{alias('arr')}y")


def test_scope_ref_numeric_root_rejected():
    # A purely-digit root (%{0.x}) parses as an Integer segment, not a
    # variable name -- Scope variable names are always strings.
    inv = Invocation(Scope(), lambda k, i: None)
    with pytest.raises(
        InterpolationError, match="Scope variable name 0 is a Integer, not a string"
    ):
        interpolate("%{0.x}", inv)


def test_format_rejects_non_str(make_tree):
    h = _format_hiera(make_tree)
    with pytest.raises(TypeError):
        h.format(5)
