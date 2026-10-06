"""The root-key lookup pipeline: ``lookup_options``, ``~`` as a value, RichData
validation, a dotted key dug once from the merged root, ``convert_to`` and
sub-lookups running the full pipeline.
"""

import pytest

from hyera import Hiera, HieraLookupError, KeyNotFoundError, MergeError


def _levels(make_tree, *level_data):
    """A tree with one hierarchy level per positional YAML string, named
    ``l1`` (highest priority) through ``lN`` (lowest)."""
    names = ["l{}".format(i + 1) for i in range(len(level_data))]
    hierarchy = [{"name": n, "path": "{}.yaml".format(n)} for n in names]
    config = {"hierarchy": hierarchy}
    files = {"data/{}.yaml".format(n): data for n, data in zip(names, level_data)}
    return make_tree(config, files=files)


def test_dotted_key_digs_after_first_found(make_tree):
    # First-found stops at the first level that has the root key; the dig into its value
    # then misses without considering a lower level (no falling through to l2's
    # "port"/"b").
    root = _levels(
        make_tree,
        "db: {host: h1}\ndot_first: {a: 1}\n",
        "db: {host: h2, port: 5432}\ndot_first: {a: 1, b: 2}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        h.lookup("db.port")
    with pytest.raises(KeyNotFoundError):
        h.lookup("dot_first.b")


def test_dotted_key_merges_root_then_digs(make_tree):
    # A merge combines the root values across levels first and digs once after: digging
    # each level and merging the results would give {"x": 1, "y": 2} for dot_hash.b, as
    # l2's own "b" would survive the hash merge.
    root = _levels(
        make_tree,
        "db: {port: 1}\ndot_hash: {b: {x: 1}}\n",
        "db: {port: 2, host: h2}\ndot_hash: {b: {x: 1, y: 2}}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("db.port", merge="hash") == 1
    assert h.lookup("dot_hash.b", merge="hash") == {"x": 1}


def test_quoted_segment_key(make_tree):
    root = _levels(make_tree, '"dotted.key": dv\n')
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup('"dotted.key"') == "dv"


def test_found_null_is_found(make_tree):
    # A key set to ~ is a genuine value, not a miss: it stops a
    # first-found lookup, beats a default=, and feeds every merge
    # strategy exactly as Puppet's Undef would.
    root = _levels(make_tree, "nil_first: ~\n", "nil_first: x\n")
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("nil_first", default_value="D") is None
    assert h.lookup("nil_first", merge="unique") == [None, "x"]
    with pytest.raises(MergeError, match="expects a Hash value, got Undef"):
        h.lookup("nil_first", merge="hash")
    # deep_merge treats a nil source as absent, keeping the other side.
    assert h.lookup("nil_first", merge="deep") == "x"

    deep_root = _levels(make_tree, "nil_deep: ~\n", "nil_deep: {a: 1}\n")
    deep_h = Hiera(str(deep_root / "hiera.yaml"))
    assert deep_h.lookup("nil_deep", merge="deep") == {"a": 1}

    only_root = _levels(make_tree, "onlynil: ~\n", "other: 1\n")
    only_h = Hiera(str(only_root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        only_h.lookup("onlynil.x")


def test_lookup_options_key_is_reserved(make_tree):
    root = _levels(
        make_tree,
        "lookup_options: {x: {merge: unique}}\n"
        "x: [a]\n"
        "ref_lo: \"v=%{lookup('lookup_options')}\"\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        h.lookup("lookup_options")
    with pytest.raises(KeyNotFoundError):
        h.lookup("lookup_options.x")
    # A missing interpolated lookup renders as empty, same as any miss.
    assert h.lookup("ref_lo") == "v="


def test_explicit_merge_keeps_convert_to(make_tree):
    root = _levels(
        make_tree,
        'lookup_options: {int_plain: {convert_to: Integer}}\nint_plain: "42"\n',
        'int_plain: "7"\n',
    )
    h = Hiera(str(root / "hiera.yaml"))
    # An explicit merge= replaces only the *merge* lookup_options would
    # have picked (here, none -- both use first-match); convert_to still
    # runs on the result.
    assert h.lookup("int_plain", merge="first") == 42
    with pytest.raises(HieraLookupError) as exc_info:
        h.lookup("int_plain", merge="unique")
    assert str(exc_info.value).startswith(
        "The convert_to lookup_option for key 'int_plain' raised error:"
    )


def test_sub_lookup_uses_target_lookup_options(make_tree):
    root = _levels(
        make_tree,
        "lookup_options: {port: {convert_to: Integer}}\n"
        'port: "5"\n'
        "ref_k: \"%{lookup('port')}\"\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    # A sub-lookup runs the target key's own lookup_options: ref_k gets
    # port already converted to an Integer, never the raw "5" string.
    assert h.lookup("ref_k") == "5"
    assert h.lookup("port") == 5


def test_sub_lookup_never_shares_merge(make_tree):
    root = _levels(
        make_tree,
        "al: [a]\nal_x: \"%{lookup('al')}\"\n",
        "al: [b]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    # al_x's own sub-lookup of "al" always runs with merge=None
    # (first-match): it sees only l1's ["a"], never al_x's own caller
    # merge="deep", which would otherwise have picked up l2's "b" too.
    assert h.lookup("al_x", merge="deep") == '["a"]'
    assert h.lookup("al", merge="deep") == ["b", "a"]


def test_rich_data_validated_per_value(make_tree):
    root = _levels(
        make_tree,
        "k_boolkey: {true: 1}\nk_nullkey: {null: 1}\nk_intkey: {1: one}\nk_ok: fine\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(HieraLookupError, match="expects Puppet::LookupValue"):
        h.lookup("k_boolkey")
    with pytest.raises(HieraLookupError, match="expects Puppet::LookupValue"):
        h.lookup("k_nullkey")
    # A bad sibling key never breaks a lookup of any other key in the
    # same file -- validation is per found root value, not per file.
    assert h.lookup("k_intkey") == {1: "one"}
    assert h.lookup("k_ok") == "fine"


def test_lookups_inside_lookup_options_see_no_options(make_tree):
    # rk's lookup_options merge spec is an interpolated %{lookup('rk_strategy')}: that
    # sub-lookup runs while the lookup_options gather is in progress, so it must see no
    # options for "rk_strategy" and must not gather lookup_options again.
    root = _levels(
        make_tree,
        "lookup_options: {rk: {merge: \"%{lookup('rk_strategy')}\"}}\n"
        "rk: [a]\n"
        "rk_strategy: unique\n",
        "rk: [b]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("rk") == ["a", "b"]
