"""Unit tests for the Puppet MergeStrategy port (``hyera._merge_strategy``).

Pure Python-level tests: no Puppet oracle, no I/O. Every rule cites the
Ruby source line it mirrors in ``_merge_strategy.py`` itself.
"""

import pytest

from hyera import HieraError, MergeError
from hyera._merge_strategy import (
    _MISSING,
    DefaultMergeStrategy,
    FirstFoundStrategy,
    HashMergeStrategy,
    MergeStrategy,
    UniqueMergeStrategy,
    _eql_key,
    _ruby_or,
)


def test_strategy_factory_forms():
    assert MergeStrategy.strategy(None) is DefaultMergeStrategy.INSTANCE
    assert MergeStrategy.strategy(False) is DefaultMergeStrategy.INSTANCE
    assert MergeStrategy.strategy("first") is FirstFoundStrategy.INSTANCE
    assert MergeStrategy.strategy("default") is DefaultMergeStrategy.INSTANCE
    assert MergeStrategy.strategy("hash") is HashMergeStrategy.INSTANCE
    assert MergeStrategy.strategy("unique") is UniqueMergeStrategy.INSTANCE

    instance = HashMergeStrategy.INSTANCE
    assert MergeStrategy.strategy(instance) is instance

    assert MergeStrategy.strategy({"strategy": "hash"}) is HashMergeStrategy.INSTANCE


def test_strategy_factory_errors():
    cases = [
        (5, "Unknown merge strategy: '5'"),
        (["deep"], "Unknown merge strategy: '[\"deep\"]'"),
        ({"strategy": ["deep"]}, "Unknown merge strategy: '[\"deep\"]'"),
        (
            {"strategy": None, "knockout_prefix": "--"},
            "The hash given as 'merge' must contain the name of a strategy "
            "in string form for the key 'strategy'",
        ),
        (
            {"strategy": "first", "knockout_prefix": "--"},
            "The merge options has wrong type, unrecognized key 'knockout_prefix'",
        ),
        (
            # No "merge" alias for "strategy": a dict with no "strategy"
            # key reads as an empty strategy name, same as {strategy: ~}.
            {"merge": "unique"},
            "The hash given as 'merge' must contain the name of a strategy "
            "in string form for the key 'strategy'",
        ),
        (
            # sort_merged_arrays is not a first/hash/unique option.
            {"strategy": "unique", "sort_merged_arrays": True},
            "The merge options has wrong type, unrecognized key 'sort_merged_arrays'",
        ),
    ]
    for merge, message in cases:
        with pytest.raises(MergeError) as excinfo:
            MergeStrategy.strategy(merge)
        assert isinstance(excinfo.value, HieraError)
        assert str(excinfo.value) == message

    # A legacy Python type (list/set/dict) is no longer an accepted merge=
    # spelling; it misses like any other non-string name.
    with pytest.raises(MergeError) as excinfo:
        MergeStrategy.strategy(list)
    assert isinstance(excinfo.value, HieraError)


def test_lookup_reduce_shapes():
    """Variants ARE the values; ``fn`` is the identity, ``_MISSING`` a miss."""
    unique = UniqueMergeStrategy.INSTANCE

    # A single variant: merge_single (uniq) applies, never convert_value/flatten.
    assert unique.lookup([[["a"], ["a", "b"]]], lambda v: v) == [["a"], ["a", "b"]]

    # Multiple variants, only one found: convert_value (flatten) applies,
    # merge_single (uniq) does not -- a genuine Puppet quirk.
    assert unique.lookup([["a", "a", "b"], _MISSING], lambda v: v) == [
        "a",
        "a",
        "b",
    ]

    # Multiple found variants: convert_value on the first, then merge()
    # (which itself dedupes via checked_merge's _ruby_or) on each later one.
    assert unique.lookup([["a", "a"], ["b"]], lambda v: v) == ["a", "b"]

    assert unique.lookup([], lambda v: v) is _MISSING
    assert unique.lookup([_MISSING, _MISSING], lambda v: v) is _MISSING

    # fn is called at most once per variant.
    calls = []

    def counting_fn(variant):
        calls.append(variant)
        return variant

    assert unique.lookup(["a", "b"], counting_fn) == ["a", "b"]
    assert calls == ["a", "b"]

    # "first" returns the first found and never calls fn again after.
    first = FirstFoundStrategy.INSTANCE
    calls = []
    assert first.lookup([_MISSING, "x", "y"], counting_fn) == "x"


def test_ruby_equality():
    assert _ruby_or([1, True], [True, 2]) == [1, True, 2]
    assert _ruby_or([1, 2], [1.0, 3]) == [1, 2, 1.0, 3]
    assert _ruby_or([0], [False]) == [0, False]

    assert _eql_key({"a": 1}) != _eql_key({"a": 1.0})
    assert _eql_key({"a": 1, "b": 2}) == _eql_key({"b": 2, "a": 1})


def test_unique_flattens_recursively():
    strategy = UniqueMergeStrategy.INSTANCE
    result = strategy.merge(["a", ["b", ["c"]]], [["d"], "a"])
    assert result == ["a", "b", "c", "d"]


def test_hash_merge_lower_keys_first():
    strategy = HashMergeStrategy.INSTANCE
    result = strategy.merge({"a": 1, "b": {"x": 1}}, {"b": {"y": 2}, "c": 3})
    assert list(result) == ["b", "c", "a"]
    assert result["b"] == {"x": 1}


def test_value_type_messages():
    hash_strategy = HashMergeStrategy.INSTANCE
    unique_strategy = UniqueMergeStrategy.INSTANCE

    def message(strategy, e1, e2):
        with pytest.raises(MergeError) as excinfo:
            strategy.merge(e1, e2)
        return str(excinfo.value)

    assert message(hash_strategy, "s", {}) == (
        "The first element of the merge has wrong type, expects a Hash value, got String"
    )
    assert message(hash_strategy, {}, "s") == (
        "The second element of the merge has wrong type, expects a Hash value, got String"
    )
    assert message(hash_strategy, None, {}) == (
        "The first element of the merge has wrong type, expects a Hash value, got Undef"
    )
    assert message(hash_strategy, ["a"], {}) == (
        "The first element of the merge has wrong type, expects a Hash value, got Tuple"
    )
    assert message(hash_strategy, [], {}) == (
        "The first element of the merge has wrong type, expects a Hash value, got Array"
    )
    assert message(hash_strategy, 1.5, {}) == (
        "The first element of the merge has wrong type, expects a Hash value, got Float"
    )
    assert message(hash_strategy, True, {}) == (
        "The first element of the merge has wrong type, expects a Hash value, got Boolean"
    )
    assert message(hash_strategy, {1: "x"}, {}) == (
        "The first element of the merge has wrong type, "
        "expects a Hash[String, Data] value, got Hash"
    )
    assert message(hash_strategy, {"a": {1: "x"}}, {}) == (
        "The first element of the merge has wrong type, "
        "entry 'a' expects a Data value, got Hash"
    )

    assert message(unique_strategy, "x", {"k": 1}) == (
        "The second element of the merge has wrong type, "
        "expects a value of type Scalar or Array, got Struct"
    )
    assert message(unique_strategy, "x", {}) == (
        "The second element of the merge has wrong type, "
        "expects a value of type Scalar or Array, got Hash"
    )
    assert message(unique_strategy, "x", None) == (
        "The second element of the merge has wrong type, "
        "expects a value of type Scalar or Array, got Undef"
    )
