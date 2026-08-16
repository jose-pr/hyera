"""Hiera 5 spec-compliance features: version, merges, lookup_options,
mapped_paths, default_hierarchy, convert_to."""

import textwrap

import pytest

from hiera import ConfigError, Hiera, Sensitive, make_merge


def build(tmp_path, config, files):
    (tmp_path / "hiera.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
    return tmp_path


# --- P1: version ---------------------------------------------------------

def test_version_5_accepted(tmp_path):
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy: [{name: c, path: common.yaml}]
        """,
        {"data/common.yaml": "k: v\n"},
    )
    assert Hiera(str(tmp_path / "hiera.yaml")).get("k") == "v"


def test_version_4_rejected(tmp_path):
    build(
        tmp_path,
        """\
        version: 4
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy: [{name: c, path: common.yaml}]
        """,
        {"data/common.yaml": "k: v\n"},
    )
    with pytest.raises(ConfigError, match="version"):
        Hiera(str(tmp_path / "hiera.yaml"))


# --- P2: merges ----------------------------------------------------------

def _two_level(tmp_path, high, low):
    return build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy:
          - {name: high, path: high.yaml}
          - {name: low, path: low.yaml}
        """,
        {"data/high.yaml": high, "data/low.yaml": low},
    )


def test_unique_merge_flattens_and_dedupes(tmp_path):
    _two_level(tmp_path, "vals: [a, b]\nscalar: x\n", "vals: [b, c]\nscalar: x\n")
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("vals", merge="unique") == ["a", "b", "c"]
    # A scalar present at multiple levels flattens+dedupes to a single entry.
    assert h.get("scalar", merge="unique") == ["x"]


def test_deep_merge_via_string_strategy(tmp_path):
    _two_level(
        tmp_path,
        "conf: {a: 1, nested: {x: 1}}\n",
        "conf: {b: 2, nested: {y: 2}}\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("conf", merge="deep") == {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}


def test_deep_merge_knockout_prefix(tmp_path):
    _two_level(
        tmp_path,
        "conf: {keep: 1, '--drop': true}\n",
        "conf: {drop: 99, other: 2}\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    merged = h.get("conf", merge={"strategy": "deep", "knockout_prefix": "--"})
    assert merged == {"keep": 1, "other": 2}
    assert "drop" not in merged


def test_unique_sort_merged_arrays(tmp_path):
    # Renamed: this exercises the *unique* strategy, not deep. The deep case
    # it was named for is covered below and was previously unimplemented.
    _two_level(tmp_path, "items: [c, a]\n", "items: [b]\n")
    h = Hiera(str(tmp_path / "hiera.yaml"))
    merged = h.get(
        "items", merge={"strategy": "unique", "sort_merged_arrays": True}
    )
    assert merged == ["a", "b", "c"]


def test_deep_merge_sort_merged_arrays(tmp_path):
    # sort_merged_arrays is a deep-merge option in Puppet; it used to be
    # swallowed by Merge.__init__ and never applied on the deep path.
    _two_level(
        tmp_path,
        "conf: {items: [c, a], nested: {more: [z, x]}}\n",
        "conf: {items: [b], nested: {more: [y]}}\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    merged = h.get(
        "conf", merge={"strategy": "deep", "sort_merged_arrays": True}
    )
    # Sorting reaches lists nested anywhere in the merged structure.
    assert merged == {"items": ["a", "b", "c"], "nested": {"more": ["x", "y", "z"]}}


def test_deep_merge_without_sort_keeps_merge_order(tmp_path):
    # The option must be opt-in: without it, merge order is preserved.
    _two_level(tmp_path, "conf: {items: [c, a]}\n", "conf: {items: [b]}\n")
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("conf", merge="deep") == {"items": ["c", "a", "b"]}


def test_deep_merge_sort_tolerates_unsortable_lists(tmp_path):
    # Mixed types have no total order in Python 3; leave them in merge order
    # rather than failing the whole lookup.
    _two_level(tmp_path, "conf: {items: [2, 'a']}\n", "conf: {items: [1]}\n")
    h = Hiera(str(tmp_path / "hiera.yaml"))
    merged = h.get(
        "conf", merge={"strategy": "deep", "sort_merged_arrays": True}
    )
    assert merged == {"items": [2, "a", 1]}


def test_deep_merge_sort_applies_after_knockout(tmp_path):
    # Knockout removes entries first; the survivors are then sorted.
    _two_level(
        tmp_path,
        "conf: {items: [c, a, '--b']}\n",
        "conf: {items: [b, d]}\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    merged = h.get(
        "conf",
        merge={
            "strategy": "deep",
            "knockout_prefix": "--",
            "sort_merged_arrays": True,
        },
    )
    assert merged == {"items": ["a", "c", "d"]}


def test_merge_hash_arrays(tmp_path):
    _two_level(
        tmp_path,
        "rows: [{name: a, v: 1}, {name: b}]\n",
        "rows: [{extra: 1}, {v: 2}]\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    merged = h.get(
        "rows", merge={"strategy": "deep", "merge_hash_arrays": True}
    )
    assert merged == [{"name": "a", "v": 1, "extra": 1}, {"name": "b", "v": 2}]


def test_make_merge_first_is_none():
    assert make_merge("first") is None
    assert make_merge(None) is None


# --- P3: lookup_options --------------------------------------------------

def test_lookup_options_sets_merge(tmp_path):
    _two_level(
        tmp_path,
        "classes: [web]\nlookup_options: {classes: {merge: unique}}\n",
        "classes: [base]\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    # No merge= passed; lookup_options drives it.
    assert h.get("classes") == ["web", "base"]


def test_lookup_options_regex_pattern(tmp_path):
    _two_level(
        tmp_path,
        "app::ports: [80]\nlookup_options: {'^app::.*': {merge: unique}}\n",
        "app::ports: [443]\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("app::ports") == [80, 443]


def test_lookup_options_unanchored_dotted_key_is_literal(tmp_path):
    # Regression: `db.port` was treated as a regex, so `.` matched any
    # character and the entry also applied to `dbxport`.
    _two_level(
        tmp_path,
        "dbxport: [80]\nlookup_options: {'db.port': {merge: unique}}\n",
        "dbxport: [443]\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    # No merge should apply -> first match wins.
    assert h.get("dbxport") == [80]


def test_lookup_options_dotted_key_still_matches_its_own_key(tmp_path):
    # The same entry must still apply to the key it literally names. (A
    # dotted lookup key resolves nested data, so `db.port` is `db` -> `port`.)
    _two_level(
        tmp_path,
        "db: {port: [80]}\nlookup_options: {'db.port': {merge: unique}}\n",
        "db: {port: [443]}\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("db.port") == [80, 443]


def test_explicit_merge_overrides_lookup_options(tmp_path):
    _two_level(
        tmp_path,
        "classes: [web]\nlookup_options: {classes: {merge: unique}}\n",
        "classes: [base]\n",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    # Explicit first wins over lookup_options unique.
    assert h.get("classes", merge="first") == ["web"]


def test_convert_to_integer(tmp_path):
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy: [{name: c, path: common.yaml}]
        """,
        {
            "data/common.yaml": "port: '8080'\n"
            "lookup_options: {port: {convert_to: Integer}}\n"
        },
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("port") == 8080


def test_convert_to_sensitive(tmp_path):
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy: [{name: c, path: common.yaml}]
        """,
        {
            "data/common.yaml": "secret: hunter2\n"
            "lookup_options: {secret: {convert_to: Sensitive}}\n"
        },
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    val = h.get("secret")
    assert isinstance(val, Sensitive)
    assert val.unwrap() == "hunter2"
    assert "hunter2" not in str(val)


# --- P4: mapped_paths ----------------------------------------------------

def test_mapped_paths(tmp_path):
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy:
          - name: roles
            mapped_paths: [roles, role, "roles/%{role}.yaml"]
        """,
        {
            "data/roles/web.yaml": "web_setting: on\n",
            "data/roles/db.yaml": "db_setting: on\n",
        },
    )
    h = Hiera(str(tmp_path / "hiera.yaml"), context={"roles": ["web", "db"]})
    assert h.get("web_setting") is True
    assert h.get("db_setting") is True


# --- P5: default_hierarchy ----------------------------------------------

def test_default_hierarchy_fallback(tmp_path):
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy: [{name: main, path: main.yaml}]
        default_hierarchy: [{name: fallback, path: module_defaults.yaml}]
        """,
        {
            "data/main.yaml": "from_main: 1\n",
            "data/module_defaults.yaml": "from_default: 2\nfrom_main: 99\n",
        },
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    # Only in default_hierarchy -> found via fallback.
    assert h.get("from_default") == 2
    # In both -> main wins, default_hierarchy not consulted.
    assert h.get("from_main") == 1
