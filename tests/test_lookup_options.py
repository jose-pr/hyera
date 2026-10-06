"""``lookup_options`` matching: Ruby regex search semantics, entry shapes,
invalid patterns, and merged pattern order (ports ``lookup_adapter.rb``
:249-330 via :mod:`hyera._lookup.lookup_adapter`)."""

import pytest

from hyera import Hiera, HieraLookupError


def _two_level(make_tree, high, low):
    return make_tree(
        {
            "hierarchy": [
                {"name": "high", "path": "high.yaml"},
                {"name": "low", "path": "low.yaml"},
            ]
        },
        files={"data/high.yaml": high, "data/low.yaml": low},
    )


def test_prefix_pattern_matches_by_search(make_tree):
    # `^app::` (no trailing `.*`) never *fullmatch*es `app::ports`, but
    # Puppet's own `=~` searches from the start, so it applies.
    root = _two_level(
        make_tree,
        "app::ports: [80]\nlookup_options: {'^app::': {merge: unique}}\n",
        "app::ports: [443]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("app::ports") == [80, 443]


def test_ruby_only_syntax_is_translated(make_tree):
    root = _two_level(
        make_tree,
        "rbkey: [a]\n"
        "hxbeef: [a]\n"
        "lbkey: [a]\n"
        "lookup_options:\n"
        r"  '^rb(?<n>\w+)$': {merge: unique}" + "\n"
        r"  '^hx\h+\z': {merge: unique}" + "\n"
        r"  '^(?<!q)lb': {merge: unique}" + "\n",
        "rbkey: [b]\nhxbeef: [b]\nlbkey: [b]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("rbkey") == ["a", "b"]
    assert h.lookup("hxbeef") == ["a", "b"]
    assert h.lookup("lbkey") == ["a", "b"]


def test_posix_class_and_inline_flag_patterns_apply(make_tree):
    root = _two_level(
        make_tree,
        "app::settings: [a]\n"
        "Other: [a]\n"
        "lookup_options:\n"
        "  '^[[:alpha:]]+::[[:alpha:]]+$': {merge: unique}\n"
        "  '^(?i)other$': {merge: unique}\n",
        "app::settings: [b]\nOther: [b]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("app::settings") == ["a", "b"]
    assert h.lookup("Other") == ["a", "b"]


def test_untranslatable_pattern_raises_a_lookup_error_naming_it(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": "bar: [1]\n"
            "lookup_options: {'^\\p{Alpha}+$': {merge: unique}}\n"
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(HieraLookupError, match=r"\\p"):
        h.lookup("bar")


def test_invalid_pattern_raises(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": "bar: [1]\nlookup_options: {'^foo(': {merge: unique}}\n"
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(HieraLookupError) as ei:
        h.lookup("bar")
    assert str(ei.value).endswith(": /^foo(/")


def test_non_hash_lookup_options_raises(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: [1]\nlookup_options: unique\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        HieraLookupError, match="value of lookup_options must be a hash"
    ):
        h.lookup("k")


def test_non_hash_entry_error_names_the_key(make_tree):
    root = _two_level(
        make_tree,
        "kint: [1]\nlookup_options:\n  kint: 5\n",
        "kint: [2]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        HieraLookupError,
        match="The lookup_options entry for key 'kint' is not a hash",
    ):
        h.lookup("kint")


def test_entry_shapes(make_tree):
    root = _two_level(
        make_tree,
        "kk: [1]\n"
        "karr: [1]\n"
        "kint: [1]\n"
        "kbool: [1]\n"
        "pa1: [1]\n"
        "nilk: [1]\n"
        "lookup_options:\n"
        "  kk: unique\n"
        "  '^kk': {merge: unique}\n"
        "  karr: [x]\n"
        "  kint: 5\n"
        "  kbool: true\n"
        "  '^pa': [x]\n"
        "  nilk: ~\n"
        "  '^nilk': {merge: unique}\n",
        "kk: [2]\nkarr: [2]\nkint: [2]\nkbool: [2]\npa1: [2]\nnilk: [2]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    # A string exact entry gives no options and blocks the matching
    # pattern -> first-found wins.
    assert h.lookup("kk") == [1]
    # A non-hash, non-string exact or pattern entry raises.
    with pytest.raises(HieraLookupError):
        h.lookup("karr")
    with pytest.raises(HieraLookupError):
        h.lookup("kint")
    with pytest.raises(HieraLookupError):
        h.lookup("kbool")
    with pytest.raises(HieraLookupError):
        h.lookup("pa1")
    # An exact `~` entry falls through to a matching pattern.
    assert h.lookup("nilk") == [1, 2]


def test_patterns_follow_merged_order(make_tree):
    # The lower-priority level's pattern is merged in first, so it is
    # tried before the higher-priority level's more specific pattern.
    root = _two_level(
        make_tree,
        "a::b::list: [1]\nlookup_options: {'^a::b.*': {merge: first}}\n",
        "a::b::list: [2]\nlookup_options: {'^a::.*': {merge: unique}}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("a::b::list") == [1, 2]


def test_validate_lookup_options_module_prefix_direct():
    from hyera._lookup.lookup_adapter import validate_lookup_options

    # A "^"-prefixed key whose own prefix DOES match the module name: the
    # loop keeps checking the rest of the keys instead of raising.
    assert validate_lookup_options({"^m::x": {}, "m::y": {}}, "m") == {
        "^m::x": {},
        "m::y": {},
    }


def test_validate_data_value_no_location_direct():
    from hyera._lookup.lookup_adapter import validate_data_value

    # The location-less message clause (a data_hash function called with no path/uri),
    # unlike the "when using location" clause every other validate_data_value test
    # reaches through a path-based entry.
    with pytest.raises(HieraLookupError) as exc_info:
        validate_data_value({True: "a"}, "test_data_hash", None, "k")
    assert str(exc_info.value) == (
        "Value for key 'k', in hash returned from data_hash function "
        "'test_data_hash', has wrong type, expects Puppet::LookupValue, "
        "got Hash[Boolean, String, 1, 1]"
    )
    assert "when using location" not in str(exc_info.value)


def test_convert_result_list_spec_and_non_string_type_arg_direct():
    from hyera._lookup.lookup_adapter import convert_result

    # convert_to as a list (type plus new()'s extra arguments), and a
    # non-string first element (already a type-shaped value, skipping
    # parse_type entirely).
    assert convert_result("k", ["Integer"], "5") == 5
    with pytest.raises(HieraLookupError, match="expects a Type value, got Integer"):
        convert_result("k", [5], "x")
