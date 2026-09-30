"""``Hiera.dig``/``.get``/``.getvar``: Puppet's ``dig()``/``get()``/
``getvar()`` functions (``functions/dig.rb``, ``get.rb``, ``getvar.rb``),
ported onto an already-looked-up value or the bound scope."""

import pytest

from hyera import Hiera, HieraLookupError, Scope


@pytest.fixture
def fn(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/a.yaml": "u: [1]\nh: {x: {p: 1}}\n",
            "data/common.yaml": (
                "lookup_options:\n"
                "  u: {merge: unique}\n"
                "  h: {merge: deep}\n"
                "  cv: {convert_to: Integer}\n"
                "u: [2]\n"
                "h: {x: {q: 2}, y: 3}\n"
                's: "str"\n'
                "n: 5\n"
                "big: 11\n"
                'cv: "7"\n'
                "nilk: ~\n"
                'k: "kv"\n'
                "mixed: [a, 1]\n"
                "hsi: {a: 1, b: 2}\n"
                'hdot: {"a.b": 1}\n'
            ),
        },
    )
    return Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"role": "web"}))


def test_dig_walks_keys_as_given(fn):
    h = fn
    assert h.dig("h", "x", "p") == 1  # deep merge, from lookup_options
    assert h.dig("h", "z", "p") is None  # missing middle key
    assert h.dig("nope") is None  # missing root
    assert h.dig("mixed", -1) == 1
    assert h.dig("hdot", "a.b") == 1  # a key containing "." is one key


def test_dig_errors(fn):
    h = fn
    with pytest.raises(
        HieraLookupError,
        match=r"The given data requires an Integer index at \[\"a\"\], got 'String'",
    ):
        h.dig("mixed", "a")
    with pytest.raises(
        HieraLookupError,
        match=(
            r"'dig' parameter 'data' expects a value of type Undef or "
            r"Collection, got String"
        ),
    ):
        h.dig("s", "a")
    with pytest.raises(TypeError):
        h.dig()


def test_dig_takes_lookup_options(fn):
    h = fn
    assert h.dig("h", "x", merge="hash") == {"p": 1}
    with pytest.raises(
        HieraLookupError,
        match=r"Found value has wrong type, expects a String value, got Integer",
    ):
        h.dig("h", "x", "p", value_type="String")


def test_get_navigates_like_puppet(fn):
    h = fn
    assert h.get("h.x.q", "D") == 2
    with pytest.raises(
        HieraLookupError,
        match=(
            r"The given data does not contain a Collection at \[\"y\"\], "
            r"got 'Integer\[3, 3\]'"
        ),
    ):
        h.get("h.y.z", "D")
    caught = h.get("h.y.z", "D", block=lambda e: "E:" + str(e))
    assert caught.startswith("E:The given data does not contain a Collection")
    assert h.get("mixed.5", "D") == "D"
    with pytest.raises(
        HieraLookupError, match="Syntax error in dotted-navigation string"
    ):
        h.get("h.a..b", "D")
    assert h.get("nope.x", "D") == "D"  # missing root
    assert h.get("0.a", "D") == "D"  # an int root can never match
    # A bare root key (no dots at all): get_segments's own "no segments
    # left, the root value itself is the found value" path.
    assert h.get("h", "D") == {"x": {"p": 1, "q": 2}, "y": 3}
    with pytest.raises(TypeError, match="get\\(\\) dotted key must be a str"):
        h.get(5)
    with pytest.raises(
        HieraLookupError, match="Syntax error in dotted-navigation string"
    ):
        h.get("")
    assert h.get("h.x.p", value_type="Integer") == 1
    with pytest.raises(
        HieraLookupError,
        match=r"Found value has wrong type, expects a String value, got Integer",
    ):
        h.get("h.x.p", value_type="String")


def test_data_functions_get_empty_navigation_direct():
    # hyera._data_functions.get()'s own "empty navigation returns the
    # value untouched" branch (core.Hiera.get() never calls it with an
    # empty string -- "" parses to zero segments the same way a bare root
    # key does, reaching get_segments directly instead).
    from hyera._data_functions import get

    assert get({"a": 1}, "") == {"a": 1}


def test_getvar_reads_scope(fn):
    h = fn
    assert h.getvar("facts.role") == "web"
    assert h.getvar("nosuch.x", "GD") == "GD"

    strict_h = Hiera(
        str(h._base_path / "hiera.yaml"),
        scope=Scope(facts={"role": "web"}, strict="error"),
    )
    # An undefined variable returns the default regardless of strict.
    assert strict_h.getvar("nosuch.x", "GD") == "GD"

    with pytest.raises(
        HieraLookupError,
        match="'getvar' The given string does not start with a valid variable name",
    ):
        h.getvar("1abc")
    with pytest.raises(
        HieraLookupError,
        match="'getvar' The given string does not start with a valid variable name",
    ):
        h.getvar("facts-x")
