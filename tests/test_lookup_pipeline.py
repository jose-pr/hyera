"""The root-key lookup pipeline: ``lookup_options`` fetched always on the
root key, a found ``~`` treated as a genuine value, RichData validated per
value, a dotted key dug once out of the *merged* root value (never per
location or per level), ``convert_to`` applied regardless of an explicit
``merge=``, and a sub-lookup (``%{lookup()}``/``%{hiera()}``/``%{alias()}``)
running the same full pipeline as a top-level lookup -- its own
``lookup_options``, its own ``default_hierarchy`` fallback, its own
``convert_to`` -- with the caller's ``merge`` never carried over.
"""

import pytest

from hyera import Hiera, HieraLookupError, KeyNotFoundError, MergeError


def _levels(make_tree, *level_data, default_hierarchy_data=None):
    """A tree with one hierarchy level per positional YAML string, named
    ``l1`` (highest priority) through ``lN`` (lowest); ``default_hierarchy_
    data`` (if given) adds a one-level ``default_hierarchy``."""
    names = ["l{}".format(i + 1) for i in range(len(level_data))]
    hierarchy = [{"name": n, "path": "{}.yaml".format(n)} for n in names]
    config = {"hierarchy": hierarchy}
    files = {"data/{}.yaml".format(n): data for n, data in zip(names, level_data)}
    if default_hierarchy_data is not None:
        config["default_hierarchy"] = [{"name": "d", "path": "defaults.yaml"}]
        files["data/defaults.yaml"] = default_hierarchy_data
    return make_tree(config, files=files)


def test_dotted_key_digs_after_first_found(make_tree):
    # First-found stops at the first level that has the *root* key; the
    # dig into that level's own value then misses without ever
    # considering a lower level -- there is no falling through to l2's
    # "port"/"b" once l1's "db"/"dot_first" was found at all.
    root = _levels(
        make_tree,
        "db: {host: h1}\ndot_first: {a: 1}\n",
        "db: {host: h2, port: 5432}\ndot_first: {a: 1, b: 2}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        h.get("db.port", throw=True)
    with pytest.raises(KeyNotFoundError):
        h.get("dot_first.b", throw=True)


def test_dotted_key_merges_root_then_digs(make_tree):
    # A merge combines the *root* values across levels first; the dig
    # into the merged result happens exactly once after. Digging each
    # level's value separately and merging the dug results (the old,
    # wrong order) would instead give {"x": 1, "y": 2} for dot_hash.b: l2's
    # own "b" (never dug out on its own) would survive the hash merge.
    root = _levels(
        make_tree,
        "db: {port: 1}\ndot_hash: {b: {x: 1}}\n",
        "db: {port: 2, host: h2}\ndot_hash: {b: {x: 1, y: 2}}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("db.port", merge="hash", throw=True) == 1
    assert h.get("dot_hash.b", merge="hash", throw=True) == {"x": 1}


def test_quoted_segment_key(make_tree):
    root = _levels(make_tree, '"dotted.key": dv\n')
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get('"dotted.key"', throw=True) == "dv"


def test_found_null_is_found(make_tree):
    # A key set to ~ is a genuine value, not a miss: it stops a
    # first-found lookup, beats a default=, and feeds every merge
    # strategy exactly as Puppet's Undef would.
    root = _levels(make_tree, "nil_first: ~\n", "nil_first: x\n")
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("nil_first", default="D") is None
    assert h.get("nil_first", merge="unique", throw=True) == [None, "x"]
    with pytest.raises(MergeError, match="expects a Hash value, got Undef"):
        h.get("nil_first", merge="hash", throw=True)
    # deep_merge treats a nil source as absent, keeping the other side.
    assert h.get("nil_first", merge="deep", throw=True) == "x"

    deep_root = _levels(make_tree, "nil_deep: ~\n", "nil_deep: {a: 1}\n")
    deep_h = Hiera(str(deep_root / "hiera.yaml"))
    assert deep_h.get("nil_deep", merge="deep", throw=True) == {"a": 1}

    only_root = _levels(make_tree, "onlynil: ~\n", "other: 1\n")
    only_h = Hiera(str(only_root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        only_h.get("onlynil.x", throw=True)


def test_lookup_options_key_is_reserved(make_tree):
    root = _levels(
        make_tree,
        "lookup_options: {x: {merge: unique}}\n"
        "x: [a]\n"
        "ref_lo: \"v=%{lookup('lookup_options')}\"\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        h.get("lookup_options", throw=True)
    with pytest.raises(KeyNotFoundError):
        h.get("lookup_options.x", throw=True)
    # A missing interpolated lookup renders as empty, same as any miss.
    assert h.get("ref_lo", throw=True) == "v="


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
    assert h.get("int_plain", merge="first", throw=True) == 42
    with pytest.raises(HieraLookupError) as exc_info:
        h.get("int_plain", merge="unique", throw=True)
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
    assert h.get("ref_k", throw=True) == "5"
    assert h.get("port", throw=True) == 5


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
    assert h.get("al_x", merge="deep", throw=True) == '["a"]'
    assert h.get("al", merge="deep", throw=True) == ["b", "a"]


def test_sub_lookup_spans_default_hierarchy(make_tree):
    root = _levels(
        make_tree,
        "mainval: dval\n",
        default_hierarchy_data=(
            "main_ref: \"%{lookup('mainval')}\"\n"
            "dh_only: mval\n"
            "dh_ref: \"%{lookup('dh_only')}\"\n"
        ),
    )
    h = Hiera(str(root / "hiera.yaml"))
    # default_hierarchy's own values are found only on a main-hierarchy
    # miss; a sub-lookup from inside default_hierarchy reaches the full
    # pipeline too -- both the main hierarchy (main_ref) and
    # default_hierarchy itself (dh_ref) -- never only the level list
    # that happened to be walked to reach it.
    assert h.get("main_ref", throw=True) == "dval"
    assert h.get("dh_ref", throw=True) == "mval"


def test_rich_data_validated_per_value(make_tree):
    root = _levels(
        make_tree,
        "k_boolkey: {true: 1}\nk_nullkey: {null: 1}\nk_intkey: {1: one}\nk_ok: fine\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(HieraLookupError, match="expects Puppet::LookupValue"):
        h.get("k_boolkey", throw=True)
    with pytest.raises(HieraLookupError, match="expects Puppet::LookupValue"):
        h.get("k_nullkey", throw=True)
    # A bad sibling key never breaks a lookup of any other key in the
    # same file -- validation is per found root value, not per file.
    assert h.get("k_intkey", throw=True) == {1: "one"}
    assert h.get("k_ok", throw=True) == "fine"


def test_lookups_inside_lookup_options_see_no_options(make_tree):
    # rk's own lookup_options merge spec is itself an interpolated
    # %{lookup('rk_strategy')}; resolving it is a sub-lookup that runs
    # while this scope's lookup_options gather is still in progress. It
    # must see no options for "rk_strategy" (there are none anyway) and,
    # above all, must not recurse into gathering lookup_options again.
    root = _levels(
        make_tree,
        "lookup_options: {rk: {merge: \"%{lookup('rk_strategy')}\"}}\n"
        "rk: [a]\n"
        "rk_strategy: unique\n",
        "rk: [b]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("rk", throw=True) == ["a", "b"]
