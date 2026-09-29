"""Hiera 5 spec-compliance features: version, merges, lookup_options,
mapped_paths, default_hierarchy, convert_to."""

import pytest

from pyera import ConfigError, Hiera, Sensitive, make_merge

# --- version ---------------------------------------------------------


def test_version_5_accepted(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    assert Hiera(str(root / "hiera.yaml")).get("k") == "v"


def test_version_4_rejected(make_tree):
    root = make_tree(
        """\
        version: 4
        defaults: {data_hash: yaml_data, datadir: data}
        hierarchy: [{name: c, path: common.yaml}]
        """,
        {"data/common.yaml": "k: v\n"},
    )
    with pytest.raises(ConfigError, match="version"):
        Hiera(str(root / "hiera.yaml"))


# --- merges ----------------------------------------------------------


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


def test_unique_merge_flattens_and_dedupes(make_tree):
    root = _two_level(
        make_tree, "vals: [a, b]\nscalar: x\n", "vals: [b, c]\nscalar: x\n"
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("vals", merge="unique") == ["a", "b", "c"]
    # A scalar present at multiple levels flattens+dedupes to a single entry.
    assert h.get("scalar", merge="unique") == ["x"]


def test_deep_merge_via_string_strategy(make_tree):
    root = _two_level(
        make_tree,
        "conf: {a: 1, nested: {x: 1}}\n",
        "conf: {b: 2, nested: {y: 2}}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("conf", merge="deep") == {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}


def test_deep_merge_knockout_prefix(make_tree):
    root = _two_level(
        make_tree,
        "conf: {keep: 1, '--drop': true}\n",
        "conf: {drop: 99, other: 2}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    merged = h.get("conf", merge={"strategy": "deep", "knockout_prefix": "--"})
    assert merged == {"keep": 1, "other": 2}
    assert "drop" not in merged


def test_unique_sort_merged_arrays(make_tree):
    # Renamed: this exercises the *unique* strategy, not deep. The deep case
    # it was named for is covered below and was previously unimplemented.
    root = _two_level(make_tree, "items: [c, a]\n", "items: [b]\n")
    h = Hiera(str(root / "hiera.yaml"))
    merged = h.get("items", merge={"strategy": "unique", "sort_merged_arrays": True})
    assert merged == ["a", "b", "c"]


def test_deep_merge_sort_merged_arrays(make_tree):
    # sort_merged_arrays is a deep-merge option in Puppet; it used to be
    # swallowed by Merge.__init__ and never applied on the deep path.
    root = _two_level(
        make_tree,
        "conf: {items: [c, a], nested: {more: [z, x]}}\n",
        "conf: {items: [b], nested: {more: [y]}}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    merged = h.get("conf", merge={"strategy": "deep", "sort_merged_arrays": True})
    # Sorting reaches lists nested anywhere in the merged structure.
    assert merged == {"items": ["a", "b", "c"], "nested": {"more": ["x", "y", "z"]}}


def test_deep_merge_without_sort_keeps_merge_order(make_tree):
    # The option must be opt-in: without it, merge order is preserved.
    root = _two_level(make_tree, "conf: {items: [c, a]}\n", "conf: {items: [b]}\n")
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("conf", merge="deep") == {"items": ["c", "a", "b"]}


def test_deep_merge_sort_tolerates_unsortable_lists(make_tree):
    # Mixed types have no total order in Python 3; leave them in merge order
    # rather than failing the whole lookup.
    root = _two_level(make_tree, "conf: {items: [2, 'a']}\n", "conf: {items: [1]}\n")
    h = Hiera(str(root / "hiera.yaml"))
    merged = h.get("conf", merge={"strategy": "deep", "sort_merged_arrays": True})
    assert merged == {"items": [2, "a", 1]}


def test_deep_merge_sort_applies_after_knockout(make_tree):
    # Knockout removes entries first; the survivors are then sorted.
    root = _two_level(
        make_tree,
        "conf: {items: [c, a, '--b']}\n",
        "conf: {items: [b, d]}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    merged = h.get(
        "conf",
        merge={
            "strategy": "deep",
            "knockout_prefix": "--",
            "sort_merged_arrays": True,
        },
    )
    assert merged == {"items": ["a", "c", "d"]}


def test_merge_hash_arrays(make_tree):
    root = _two_level(
        make_tree,
        "rows: [{name: a, v: 1}, {name: b}]\n",
        "rows: [{extra: 1}, {v: 2}]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    merged = h.get("rows", merge={"strategy": "deep", "merge_hash_arrays": True})
    assert merged == [{"name": "a", "v": 1, "extra": 1}, {"name": "b", "v": 2}]


def test_make_merge_first_is_none():
    assert make_merge("first") is None
    assert make_merge(None) is None


# --- lookup_options --------------------------------------------------


def test_lookup_options_sets_merge(make_tree):
    root = _two_level(
        make_tree,
        "classes: [web]\nlookup_options: {classes: {merge: unique}}\n",
        "classes: [base]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    # No merge= passed; lookup_options drives it.
    assert h.get("classes") == ["web", "base"]


def test_lookup_options_regex_pattern(make_tree):
    root = _two_level(
        make_tree,
        "app::ports: [80]\nlookup_options: {'^app::.*': {merge: unique}}\n",
        "app::ports: [443]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("app::ports") == [80, 443]


def test_lookup_options_unanchored_dotted_key_is_literal(make_tree):
    # Regression: `db.port` was treated as a regex, so `.` matched any
    # character and the entry also applied to `dbxport`.
    root = _two_level(
        make_tree,
        "dbxport: [80]\nlookup_options: {'db.port': {merge: unique}}\n",
        "dbxport: [443]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    # No merge should apply -> first match wins.
    assert h.get("dbxport") == [80]


def test_lookup_options_dotted_key_still_matches_its_own_key(make_tree):
    # The same entry must still apply to the key it literally names. (A
    # dotted lookup key resolves nested data, so `db.port` is `db` -> `port`.)
    root = _two_level(
        make_tree,
        "db: {port: [80]}\nlookup_options: {'db.port': {merge: unique}}\n",
        "db: {port: [443]}\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("db.port") == [80, 443]


def test_explicit_merge_overrides_lookup_options(make_tree):
    root = _two_level(
        make_tree,
        "classes: [web]\nlookup_options: {classes: {merge: unique}}\n",
        "classes: [base]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))
    # Explicit first wins over lookup_options unique.
    assert h.get("classes", merge="first") == ["web"]


def test_lookup_options_merged_once_per_context(make_tree):
    # Merging lookup_options walks every file in the hierarchy; a
    # default-merge get() needs it for every key, so it is cached per
    # resolved context rather than re-merged on each call.
    root = _two_level(
        make_tree,
        "classes: [web]\nother: [x]\nlookup_options: {classes: {merge: unique}}\n",
        "classes: [base]\nother: [y]\n",
    )
    h = Hiera(str(root / "hiera.yaml"))

    calls = []
    real_get_key = h.get_key

    def counting_get_key(key, paths, context, merge):
        calls.append(key)
        return real_get_key(key, paths, context, merge)

    h.get_key = counting_get_key
    assert h.get("classes") == ["web", "base"]
    assert h.get("other") == ["x"]
    assert h.get("classes") == ["web", "base"]
    # Three default-merge lookups, but lookup_options is merged at most once.
    assert calls.count("lookup_options") <= 1


def test_lookup_options_cache_is_per_context(make_tree):
    # Different contexts must not share a cached options mapping.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "env", "path": "environments/%{environment}.yaml"},
                {"name": "c", "path": "common.yaml"},
            ]
        },
        files={
            "data/environments/a.yaml": (
                "vals: [1]\nlookup_options: {vals: {merge: unique}}\n"
            ),
            "data/environments/b.yaml": "vals: [1]\n",
            "data/common.yaml": "vals: [2]\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    # Context a declares a unique merge; context b declares nothing.
    assert h.get("vals", environment="a") == [1, 2]
    assert h.get("vals", environment="b") == [1]
    # Re-run in the opposite order to catch a cache that ignores context.
    assert h.get("vals", environment="b") == [1]
    assert h.get("vals", environment="a") == [1, 2]


def test_convert_to_integer(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": "port: '8080'\n"
            "lookup_options: {port: {convert_to: Integer}}\n"
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("port") == 8080


def test_convert_to_sensitive(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": "secret: hunter2\n"
            "lookup_options: {secret: {convert_to: Sensitive}}\n"
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    val = h.get("secret")
    assert isinstance(val, Sensitive)
    assert val.unwrap() == "hunter2"
    assert "hunter2" not in str(val)


# --- mapped_paths ----------------------------------------------------


def test_mapped_paths(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "roles",
                    "mapped_paths": ["roles", "role", "roles/%{role}.yaml"],
                }
            ]
        },
        files={
            "data/roles/web.yaml": "web_setting: on\n",
            "data/roles/db.yaml": "db_setting: on\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), context={"roles": ["web", "db"]})
    assert h.get("web_setting") is True
    assert h.get("db_setting") is True


# --- default_hierarchy ----------------------------------------------


def test_default_hierarchy_fallback(make_tree):
    root = make_tree(
        {
            "hierarchy": [{"name": "main", "path": "main.yaml"}],
            "default_hierarchy": [{"name": "fallback", "path": "module_defaults.yaml"}],
        },
        files={
            "data/main.yaml": "from_main: 1\n",
            "data/module_defaults.yaml": "from_default: 2\nfrom_main: 99\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    # Only in default_hierarchy -> found via fallback.
    assert h.get("from_default") == 2
    # In both -> main wins, default_hierarchy not consulted.
    assert h.get("from_main") == 1
