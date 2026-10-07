"""Unit tests for the Puppet MergeStrategy port (``hyera._lookup.merge_strategy``):
pure Python, no Puppet oracle, no I/O.
"""

import collections
import re
import types

import pytest

from hyera import Hiera, HieraError, MergeError
from hyera._lookup import merge_strategy
from hyera._lookup.interpolation import unshare
from hyera._lookup.merge_strategy import (
    _MISSING,
    DeepMergeStrategy,
    DefaultMergeStrategy,
    FirstFoundStrategy,
    HashMergeStrategy,
    MergeStrategy,
    UniqueMergeStrategy,
)
from hyera._lookup.deep_merge import (
    _eql_key,
    _ruby_delete,
    _ruby_delete_if,
    _ruby_or,
    deep_merge,
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

    # A legacy Python type (list/set/dict) is not a merge= spelling; it misses like any
    # other non-string name.
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

    # An object with no Ruby equivalent: keyed by identity, matching
    # Ruby's default Object#hash/#eql?.
    marker = object()
    assert _eql_key(marker) == ("id", id(marker))
    assert _eql_key(marker) != _eql_key(object())


def test_is_data_puppet_type_name_and_ruby_inspect_fallbacks():
    from hyera._lookup.merge_strategy import _is_data, _puppet_type_name
    from hyera._lookup.interpolation import _ruby_inspect

    marker = object()
    assert _is_data(marker) is False
    assert _puppet_type_name(marker) == "object"
    assert _ruby_inspect({"a": 1, "b": [1, 2]}) == '{"a"=>1, "b"=>[1, 2]}'


def test_ruby_class_name_eq_cmp_direct():
    from hyera._lookup.deep_merge import _ruby_class_name, _ruby_cmp, _ruby_eq

    assert _ruby_class_name([1, 2]) == "Array"
    marker = object()
    assert _ruby_class_name(marker) == "object"

    assert _ruby_eq([1, 2], [1, 2]) is True
    assert _ruby_eq([1, 2], [1, 3]) is False
    assert _ruby_eq([1, 2], [1, 2, 3]) is False

    # A per-element comparison failing partway through stops immediately.
    assert _ruby_cmp([1, "a"], [1, 2]) is None
    # Equal-so-far elements fall through to a length tiebreak.
    assert _ruby_cmp([1, 2, 3], [1, 2]) == 1


def test_clear_or_nil_direct():
    from hyera._lookup.deep_merge import _clear_or_nil

    assert _clear_or_nil("x") == ""
    assert _clear_or_nil(object()) is None


def test_unpack_arrays_with_a_non_array_dest():
    # unpack_arrays (array_split_char) joins/re-splits dest too, but only
    # when dest is itself an Array -- a scalar dest is left alone before
    # the rest of the merge decides what to do with it.
    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "unpack_arrays": ","}
    )
    assert strategy.merge({"l": ["a,b"]}, {"l": "x"}) == {"l": ["a", "b"]}


def test_unpack_arrays_renders_a_hash_element_the_way_ruby_inspects_it():
    # unpack_arrays joins the array with to_s, so a Hash element becomes its
    # Ruby inspect text: non-ASCII characters stay as they are.
    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "unpack_arrays": ","}
    )
    merged = strategy.merge({"l": [{"k": "é"}]}, {"l": ["c"]})
    assert merged == {"l": ["c", '{"k"=>"é"}']}


def test_subclass_without_key_is_not_registered():
    # __init_subclass__ registers (and instantiates INSTANCE for) only a subclass that
    # sets its own KEY; an abstract intermediate subclass is the only way to reach the
    # "skip" side.
    from hyera._lookup.merge_strategy import _STRATEGIES

    class _NoKeyStrategy(MergeStrategy):
        pass

    assert _NoKeyStrategy.INSTANCE is None
    assert "_NoKeyStrategy" not in _STRATEGIES
    assert all(cls is not _NoKeyStrategy for cls in _STRATEGIES.values())


def test_merge_strategy_base_is_abstract_and_first_found_never_rejects():
    # MergeStrategy's checked_merge()/_value_problem() are abstract (every concrete
    # strategy overrides both), so they are called by direct construction.
    # FirstFoundStrategy overrides only _value_problem (it never merges).
    base = MergeStrategy({})
    with pytest.raises(NotImplementedError):
        base.checked_merge(1, 2)
    with pytest.raises(NotImplementedError):
        base._value_problem(1)

    assert FirstFoundStrategy.INSTANCE._value_problem(1) is None


def test_unique_value_problem_nested_array_item():
    from hyera._lookup.merge_strategy import UniqueMergeStrategy
    from hyera._lookup.deep_merge import _ruby_class_name

    assert UniqueMergeStrategy.INSTANCE._value_problem([1, object()]) == (
        "expects a value of type Scalar or Array, got Array[object]"
    )
    assert _ruby_class_name(None) == "NilClass"


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


def test_deep_merge_union_lower_first():
    result = deep_merge({"a": {"x": 1, "l": [1, 2]}}, {"a": {"y": 2, "l": [2, 3]}}, {})
    assert result == {"a": {"x": 1, "y": 2, "l": [2, 3, 1]}}

    assert deep_merge(["a", "b"], ["c", "a"], {}) == ["c", "a", "b"]
    assert deep_merge({"l": ["a", "a"]}, {"l": ["b"]}, {}) == {"l": ["b", "a"]}
    assert deep_merge({"l": ["b"]}, {"l": ["a", "a"]}, {}) == {"l": ["a", "b"]}
    assert deep_merge({"l": [1]}, {"l": [True, 1.0]}, {}) == {"l": [True, 1.0, 1]}


def test_deep_merge_type_table():
    assert deep_merge({"k": [1]}, {"k": {"a": 1}}, {}) == {"k": [1]}
    assert deep_merge({"k": [1]}, {"k": 5}, {}) == {"k": [1]}
    assert deep_merge({"k": [1]}, {"k": None}, {}) == {"k": [1]}
    assert deep_merge({"k": ["a"]}, {"k": False}, {}) == {"k": ["a"]}
    assert deep_merge({"k": None}, {"k": 1}, {}) == {"k": 1}

    # Whole-value type mismatches (not under a key): a non-empty Hash source overwrites;
    # an empty Hash source leaves dest untouched (Ruby's `source.each` never enters its
    # "overwrite dest" branch, deep_merge_core.rb:111-138).
    assert deep_merge({"a": 1, "b": 2}, ["x"], {}) == {"a": 1, "b": 2}
    assert deep_merge({}, "s", {}) == "s"


def test_knockout_semantics():
    options = {"knockout_prefix": "--"}

    # Hash-KEY level: "--drop" is just a literal key, never a knockout
    # marker -- knockout only ever acts on scalar/Array VALUES.
    result = deep_merge({"--drop": "x", "keep": 1}, {"drop": 2, "other": 3}, options)
    assert result == {"drop": 2, "other": 3, "--drop": "x", "keep": 1}

    # A scalar string source starting with the prefix blanks the value,
    # regardless of dest's own type.
    assert deep_merge({"k": "--"}, {"k": [1]}, options) == {"k": ""}
    assert deep_merge({"k": "--foo"}, {"k": [1]}, options) == {"k": ""}
    assert deep_merge({"k": "--"}, {"k": {"a": 1}}, options) == {"k": ""}
    assert deep_merge("--", [1], options) == ""

    # An Array source vs a non-Array dest: the pruned source wins outright.
    assert deep_merge({"k": ["--a", "b"]}, {"k": 5}, options) == {"k": ["b"]}

    # A naked prefix element (an array member EQUAL to the bare prefix)
    # clears dest entirely.
    assert deep_merge({"l": ["--"]}, {"l": ["a"]}, options) == {"l": []}

    # Dest's own "--a" is plain data here -- only SOURCE elements are
    # knockout instructions.
    result = deep_merge({"l": ["a"]}, {"l": ["--a", "c"]}, options)
    assert result == {"l": ["--a", "c", "a"]}

    # Three levels, folded highest to lowest the way the base reduce does.
    memo = deep_merge(["--a"], unshare(["b"]), options)
    memo = deep_merge(memo, unshare(["a", "c"]), options)
    assert memo == ["a", "c", "b"]

    # A key only in the higher level: the dup of the containing dict shares the same
    # nested list with source (a shallow dup), which makes the 2-element case knock
    # itself out entirely; see _ruby_delete_if.
    result = deep_merge({"k": {"l": ["--a", "b"]}}, {"other": 1}, options)
    assert result == {"other": 1, "k": {"l": []}}

    result = deep_merge({"k": {"l": ["--a", "b", "c"]}}, {"other": 1}, options)
    assert result == {"other": 1, "k": {"l": ["c"]}}

    # Same shape, one level shallower: the dup here is of the LIST itself,
    # a genuinely separate object -- no aliasing, so "b" survives.
    result = deep_merge({"l": ["--a", "b"]}, {"other": 1}, options)
    assert result == {"other": 1, "l": ["b"]}

    assert deep_merge({"k": 1}, {"k": 2}, {}) == {"k": 1}


def test_knockout_regex_prefixes():
    # A prefix is spliced unescaped into the pattern -- it can BE a regex.
    result = deep_merge(
        {"l": [".a", "xb"]}, {"l": ["a", "b", "xb", "c"]}, {"knockout_prefix": "."}
    )
    assert result == {"l": ["c"]}

    result = deep_merge(
        {"l": ["x+a"]}, {"l": ["a", "x+a", "xxa"]}, {"knockout_prefix": "x+"}
    )
    assert result == {"l": ["a", "xxa"]}


def test_ruby_delete_if_live_length():
    """``_ruby_delete_if`` in isolation, with the exact knockout callback
    shape, against a SHARED array (source is dest, same object) -- the
    scenario ``test_knockout_semantics`` exercises through the full
    ``deep_merge`` pipeline. Confirms the read/write/truncate walk, not
    just its end-to-end effect.
    """
    shared = ["--a", "b", "c"]
    pattern = re.compile("^--")

    def knockout(ko_item):
        item = pattern.sub("", ko_item) if isinstance(ko_item, str) else ko_item
        if item != ko_item:
            _ruby_delete(shared, item)
            _ruby_delete(shared, ko_item)
            return True
        return False

    _ruby_delete_if(shared, knockout)
    assert shared == ["c"]


def test_merge_hash_arrays_any_length():
    result = deep_merge([{"a": 1}, {"b": 2}], [{"c": 3}], {"merge_hash_arrays": True})
    assert result == [{"c": 3, "a": 1}, {"b": 2}]

    result = deep_merge([{"a": 1}], [{"c": 3}, {"d": 4}], {"merge_hash_arrays": True})
    assert result == [{"c": 3, "a": 1}, {"d": 4}]

    # A mixed list (not every element is a Hash) falls back to the union.
    result = deep_merge([1, {"a": 1}], [{"c": 3}], {"merge_hash_arrays": True})
    assert result == [{"c": 3}, 1, {"a": 1}]


def test_sort_merged_arrays_and_errors():
    # An array only in the higher level of a merged hash is still sorted.
    result = deep_merge(
        {"a": {"l": [2, 1]}}, {"other": 1}, {"sort_merged_arrays": True}
    )
    assert result == {"other": 1, "a": {"l": [1, 2]}}

    result = deep_merge([[2, 1]], [[1, 2]], {"sort_merged_arrays": True})
    assert result == [[1, 2], [2, 1]]

    def sort_error(source, dest):
        with pytest.raises(MergeError) as excinfo:
            deep_merge(source, dest, {"sort_merged_arrays": True})
        return str(excinfo.value)

    assert sort_error({"l": [2, "a"]}, {"l": [1]}) == (
        "comparison of Integer with String failed"
    )
    assert sort_error({"l": [{"x": 1}]}, {"l": [{"y": 2}]}) == (
        "comparison of Hash with Hash failed"
    )
    assert sort_error([True], [False]) == "comparison of FalseClass with true failed"
    assert sort_error([None], [1]) == "comparison of Integer with nil failed"
    assert sort_error([1.5], ["a"]) == "comparison of String with 1.5 failed"
    assert sort_error([1.0, "b"], [1]) == "comparison of Float with String failed"
    # Named in original-index order (not whatever order cmp_to_key happens
    # to probe an incomparable pair in) -- the reverse ordering from the
    # cases above.
    assert sort_error({"l": [1]}, {"l": ["a", 2]}) == (
        "comparison of String with 2 failed"
    )


def test_deep_options_validation():
    with pytest.raises(MergeError) as excinfo:
        DeepMergeStrategy({"strategy": "deep", "bogus": 1})
    assert str(excinfo.value) == (
        "The merge options has wrong type, unrecognized key 'bogus'"
    )

    with pytest.raises(MergeError) as excinfo:
        DeepMergeStrategy({"strategy": "deep", "sort_merged_arrays": "yes"})
    assert str(excinfo.value) == (
        "The merge options has wrong type, entry 'sort_merged_arrays' "
        "expects a value of type Undef or Boolean, got String"
    )

    with pytest.raises(MergeError) as excinfo:
        DeepMergeStrategy({"strategy": "deep", "knockout_prefix": 5})
    assert str(excinfo.value) == (
        "The merge options has wrong type, entry 'knockout_prefix' "
        "expects a value of type Undef or String, got Integer"
    )

    # knockout_prefix: ~ (None) is accepted -- only a non-None non-str value
    # is a type problem.
    DeepMergeStrategy({"strategy": "deep", "knockout_prefix": None})


def test_empty_prefix_raises_only_when_merging():
    strategy = DeepMergeStrategy({"strategy": "deep", "knockout_prefix": ""})

    # A single found value never reaches deep_merge at all (merge_single is
    # the base class's identity), so the empty prefix never gets checked.
    assert strategy.lookup([["a"]], lambda v: v) == ["a"]

    with pytest.raises(MergeError) as excinfo:
        strategy.merge(["a"], ["b"])
    assert str(excinfo.value) == (
        "knockout_prefix cannot be an empty string in deep_merge!"
    )


def test_knockout_prefix_not_python_regex():
    with pytest.raises(MergeError) as excinfo:
        deep_merge({"l": ["a"]}, {"l": ["b"]}, {"knockout_prefix": "**"})
    assert "not a valid regular expression" in str(excinfo.value)


def test_deep_merge_mutates_only_owned_values():
    e1 = {"a": 1}
    e2 = {"b": {"c": [1, 2]}}
    original_e2 = {"b": {"c": [1, 2]}}
    DeepMergeStrategy.INSTANCE.merge(e1, e2)
    assert e2 == original_e2


# --- wired through the engine ---------------------------------------


def test_merged_lookup_leaves_cache_untouched(make_tree):
    # A merged result is freshly built per call, never a reference into
    # ``h._store._file_cache``: mutating one result must not change a later one, or
    # the cached parsed data itself.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "high", "path": "high.yaml"},
                {"name": "low", "path": "low.yaml"},
            ]
        },
        files={
            "data/high.yaml": "conf: {items: ['--a', b]}\n",
            "data/low.yaml": "conf: {items: [c]}\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    merge = {"strategy": "deep", "knockout_prefix": "--"}

    first = h.lookup("conf", merge=merge)
    second = h.lookup("conf", merge=merge)
    assert first == second

    first["items"].append("mutated")
    third = h.lookup("conf", merge=merge)
    assert third == second
    assert "mutated" not in third["items"]

    for entry in h._store._file_cache.values():
        conf = entry.data.get("conf")
        if conf:
            assert "mutated" not in conf.get("items", [])


def test_invalid_merge_raises_merge_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": (
                "k: v\nlookup_options: {k: {merge: {merge: unique}}}\n"
            )
        },
    )
    h = Hiera(str(root / "hiera.yaml"))

    with pytest.raises(ValueError) as excinfo:
        h.lookup("k", merge="bogus")  # the caller's own argument
    assert type(excinfo.value) is ValueError
    # lookup_options' `{merge: unique}` has no `strategy` key.
    with pytest.raises(MergeError):
        h.lookup("k")
    # A legacy Python type is not merge='s call shape at all (a str or a
    # dict) -- lookup()'s own call-shape validation now catches it as a
    # TypeError before it ever reaches MergeStrategy.strategy().
    legacy_type_spelling = list
    with pytest.raises(TypeError):
        h.lookup("k", merge=legacy_type_spelling)


# --- Hiera 3 deep strategies: reverse_deep, unconstrained_deep ---------


def test_hidden_strategy_keys():
    # merge_strategy.rb:49-51 -- both are reachable but never advertised.
    assert "unconstrained_deep" not in MergeStrategy.strategy_keys()
    assert "reverse_deep" not in MergeStrategy.strategy_keys()
    assert MergeStrategy.strategy("unconstrained_deep") is not None
    assert MergeStrategy.strategy("reverse_deep") is not None


def test_ruby_join_split():
    from hyera._lookup.deep_merge import _ruby_join, _ruby_split

    assert _ruby_join(["a", "b"], ",") == "a,b"
    assert _ruby_join([["a", "b"], "c"], ",") == "a,b,c"  # nested, recursive
    assert _ruby_join([None, 1, True], ",") == ",1,true"

    assert _ruby_split("a,b,c", ",") == ["a", "b", "c"]
    assert _ruby_split("a,b,", ",") == ["a", "b"]  # trailing empties dropped
    assert _ruby_split("", ",") == []
    assert _ruby_split("  a   b  ", " ") == ["a", "b"]  # whitespace-run rule


def test_unconstrained_deep_options():
    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "merge_nil_values": True}
    )
    assert strategy.merge({"k": None, "l": [1]}, {"k": 1, "l": [2]}) == {
        "k": None,
        "l": [2, 1],
    }

    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "overwrite_arrays": True}
    )
    assert strategy.merge({"l": [1]}, {"l": [2]}) == {"l": [1]}

    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "unpack_arrays": ","}
    )
    assert strategy.merge({"l": ["a,b"]}, {"l": ["c,d"]}) == {"l": ["c", "d", "a", "b"]}

    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "extend_existing_arrays": True}
    )
    assert strategy.merge({"l": [1], "s": "x"}, {"l": [2], "s": ["y"]}) == {
        "l": [2, 1],
        "s": ["y", "x"],
    }

    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "preserve_unmergeables": True}
    )
    assert strategy.merge({"l": [1], "s": "x"}, {"l": [2], "s": ["y"]}) == {
        "l": [2, 1],
        "s": ["y"],
    }

    with pytest.raises(MergeError) as excinfo:
        MergeStrategy.strategy(
            {
                "strategy": "unconstrained_deep",
                "preserve_unmergeables": True,
                "knockout_prefix": "--",
            }
        ).merge({"l": [1]}, {"l": [2]})
    assert "overwrite_unmergeable must be true" in str(excinfo.value)

    # An unrecognized key is accepted and simply ignored (deep_merge never
    # reads it).
    strategy = MergeStrategy.strategy({"strategy": "unconstrained_deep", "bogus": 1})
    assert strategy.merge({"l": [1]}, {"l": [2]}) == {"l": [2, 1]}

    with pytest.raises(MergeError):
        MergeStrategy.strategy({"strategy": "unconstrained_deep", "": 1})

    # No options at all behaves exactly like plain `deep` (source dedupes
    # its own duplicates through the union too).
    plain = MergeStrategy.strategy("unconstrained_deep")
    assert plain.merge({"l": [1, 1]}, {"l": [2]}) == {"l": [2, 1]}


def test_keep_array_duplicates():
    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "keep_array_duplicates": True}
    )
    assert strategy.merge({"l": ["a", "a"]}, {"l": ["b", "a"]}) == {
        "l": ["b", "a", "a", "a"]
    }

    # dest is missing the key entirely: src_value merges with its own
    # empty dup (additive) instead of with itself, so the source's own
    # duplicates survive rather than deduping against an identical copy.
    assert strategy.merge({"l": [1, 1, 2]}, {}) == {"l": [1, 1, 2]}


def test_overwrite_unmergeable_scalar_source_wins_over_falsy_dest():
    strategy = MergeStrategy.strategy(
        {
            "strategy": "unconstrained_deep",
            "overwrite_unmergeable": True,
            "merge_nil_values": True,
        }
    )
    # A Ruby-falsy dest (nil; an empty Hash/Array is truthy in Ruby, unlike Python) is
    # replaced by a scalar source, never merged into. Nested in a dict a falsy dest key
    # recurses with a dup of the source, so this fires only at deep_merge's top level.
    assert strategy.merge(5, None) == 5


def test_extend_existing_arrays_with_a_hash_source():
    strategy = MergeStrategy.strategy(
        {"strategy": "unconstrained_deep", "extend_existing_arrays": True}
    )
    # source is a Hash and dest is a (non-Hash) Array: the whole source
    # gets pushed onto dest, rather than merged key by key.
    assert strategy.merge({"k": {"a": 1}}, {"k": [1]}) == {"k": [1, {"a": 1}]}


def test_reverse_deep_lower_wins():
    # The lower-priority value is deep_merge's source (wins ties); the
    # higher-priority one is cloned into dest.
    strategy = MergeStrategy.strategy("reverse_deep")
    assert strategy.merge({"a": {"x": 1}, "s": "hi"}, {"a": {"y": 2}, "s": "lo"}) == {
        "a": {"x": 1, "y": 2},
        "s": "lo",
    }

    strategy = MergeStrategy.strategy(
        {"strategy": "reverse_deep", "knockout_prefix": "--"}
    )
    assert strategy.merge({"l": ["--z", "a"]}, {"l": ["z", "b"]}) == {
        "l": ["--z", "a", "z", "b"]
    }


# --- a Hash source over a non-Hash destination -----------------------


def test_hash_over_non_hash_merges_every_key_after_the_first():
    # The first key replaces the destination with the source Hash; each
    # later key is then merged with itself, which dedupes its arrays.
    source = {"a": ["x", "x"], "b": ["y", "y", "z"], "c": {"p": [1, 1]}}
    assert deep_merge(source, "legacy", {}) == {
        "a": ["x", "x"],
        "b": ["y", "z"],
        "c": {"p": [1]},
    }


def test_hash_over_non_hash_applies_knockout_after_the_first_key():
    source = {"a": ["--x", "x", "y"], "b": ["--x", "x", "y"]}
    options = {"knockout_prefix": "--"}
    assert deep_merge(source, "legacy", options) == {
        "a": ["--x", "x", "y"],
        "b": [],
    }


def test_knockout_prefix_overwrite_matches_any_line_of_a_string_item():
    options = {"knockout_prefix": "--"}
    merged = deep_merge({"l": ["keep\n--drop", "c"]}, {"l": "legacy"}, options)
    assert merged == {"l": ["c"]}


# --- merge= accepts any mapping --------------------------------------


@pytest.mark.parametrize("wrap", [types.MappingProxyType, collections.ChainMap])
def test_merge_option_accepts_any_mapping(make_tree, wrap):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "b", "path": "b.yaml"},
            ]
        },
        files={"data/a.yaml": "k: {x: 1}\n", "data/b.yaml": "k: {y: 2}\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    spec = wrap({"strategy": "deep"})
    assert h.lookup("k", merge=spec) == {"x": 1, "y": 2}
    assert MergeStrategy.strategy(spec) is DeepMergeStrategy.INSTANCE


# --- costs ------------------------------------------------------------


def test_deep_lookup_merge_does_not_clone_values_it_already_owns(
    make_tree, monkeypatch
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "b", "path": "b.yaml"},
            ]
        },
        files={"data/a.yaml": "k: {x: [1]}\n", "data/b.yaml": "k: {x: [2]}\n"},
    )
    clones = []
    original = DeepMergeStrategy.checked_merge

    def spy(self, e1, e2, *, clone=True):
        clones.append(clone)
        return original(self, e1, e2, clone=clone)

    monkeypatch.setattr(DeepMergeStrategy, "checked_merge", spy)
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k", merge="deep") == {"x": [2, 1]}
    assert clones and not any(clones)


def test_sort_merged_arrays_orders_ints_and_strings_and_rejects_mixes():
    options = {"sort_merged_arrays": True}
    assert deep_merge({"l": [3, 1]}, {"l": [2, 1]}, options) == {"l": [1, 2, 3]}
    assert deep_merge({"l": ["b", "a"]}, {"l": ["c"]}, options) == {
        "l": ["a", "b", "c"]
    }
    with pytest.raises(MergeError):
        deep_merge({"l": [1, "a"]}, {"l": [2]}, options)
