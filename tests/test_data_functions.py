"""``Hiera.dig``: Puppet's ``dig()`` function (``functions/dig.rb``), ported
onto an already-looked-up value."""

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
