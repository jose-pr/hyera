"""``Hash.new``: an Array as a key and the ``tree`` / ``hash_tree`` option."""

import json

import pytest

from hyera import HieraLookupError
from hyera._types.new_function import new_instance
from hyera._types.parser import parse_type
from hyera._output.render import JSONRender, StringRender

_HASH = parse_type("Hash")


def _new(value, *args):
    return new_instance(_HASH, value, *args)


def test_an_array_key_is_a_hashable_key_that_renders_as_a_ruby_array():
    result = _new([[["a", 1], "x"]])
    ((key, value),) = result.items()
    assert list(key) == ["a", 1] and value == "x"
    assert json.loads(JSONRender().dumps(result)) == {'["a", 1]': "x"}
    assert StringRender().dumps(result) == '{["a", 1]=>"x"}'


def test_a_nested_array_key_renders_all_its_levels():
    result = _new([[[["a"], "b"], "x"]])
    assert json.loads(JSONRender().dumps(result)) == {'[["a"], "b"]': "x"}


def test_a_hash_key_is_refused():
    with pytest.raises(HieraLookupError, match="unusable Hash key"):
        _new([[{"a": 1}, "x"]])


@pytest.mark.parametrize(
    "entries, build, expected",
    [
        ([[["a", "b"], "x"]], "tree", {"a": {"b": "x"}}),
        ([[["a", "b"], ["p", "q"]]], "tree", {"a": {"b": ["p", "q"]}}),
        ([[["a", "b"], ["p", "q"]]], "hash_tree", {"a": {"b": {0: "p", 1: "q"}}}),
        ([[["a"], {"x": [1, 2]}]], "hash_tree", {"a": {"x": [1, 2]}}),
        ([[[], ["p", "q"]], [["a"], 2]], "tree", {0: "p", 1: "q", "a": 2}),
        ([[[], {"r": 1}], [["a"], 2]], "tree", {"r": 1, "a": 2}),
        (
            [[["a"], [1, 2, 3]], [["a", 5], "Q"]],
            "tree",
            {"a": [1, 2, 3, None, None, "Q"]},
        ),
        ([[["a"], [1, 2, 3]], [["a", -1], "Q"]], "tree", {"a": [1, 2, "Q"]}),
        ([[["a"], "xyz"], [["a", 0], "Q"]], "tree", {"a": "Qyz"}),
        ([[["a"], "xyz"], [["a", "yz"], "Q"]], "tree", {"a": "xQ"}),
    ],
)
def test_build_option_nests_entries_by_path(entries, build, expected):
    assert _new(entries, build) == expected


@pytest.mark.parametrize(
    "value, args",
    [
        ([[["a"], "x"]], ("bogus",)),
        ([[["a"], "x"]], ("TREE",)),
        ([[["a"], "x"]], (None,)),
        ([[["a"], "x"]], (5,)),
        ([[["a"], "x"]], ("tree", "extra")),
        ([["a", "x"]], ("tree",)),
        ([], ("tree",)),
        ({"a": 1}, ("tree",)),
        ("abc", ("tree",)),
        ([[["a"], 1, 2]], ("tree",)),
        ([[[], "x"]], ("tree",)),
        ([[["a"], 1], [["a", "b"], 2]], ("tree",)),
        ([[["a"], None], [["a", "b"], "Q"]], ("tree",)),
        ([[["a"], [1, 2, 3]], [["a", -9], "Q"]], ("tree",)),
        ([[["a"], [1, 2, 3]], [["a", "k"], "Q"]], ("tree",)),
        ([[["a"], "xyz"], [["a", "k"], "Q"]], ("tree",)),
    ],
)
def test_build_option_refuses_what_puppet_refuses(value, args):
    with pytest.raises(HieraLookupError):
        _new(value, *args)
