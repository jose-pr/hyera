"""``Hiera.keys``, ``Hiera.to_dict`` and the one-shot ``hyera.lookup``."""

import inspect
import logging

import pytest

import hyera
from hyera import (
    BackendError,
    Hiera,
    InterpolationError,
    KeyNotFoundError,
    Scope,
    Sensitive,
)
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


_CONFIG = {
    "hierarchy": [
        {"name": "node", "path": "nodes/%{facts.host}.yaml"},
        {"name": "globs", "glob": "globs/*.yaml"},
        {"name": "common", "path": "common.yaml"},
    ]
}

_ENV = "version: 5\nhierarchy:\n  - {name: e, path: e.yaml}\n"
_MODULE = (
    "version: 5\n"
    "hierarchy:\n  - {name: m, path: m.yaml}\n"
    "default_hierarchy:\n  - {name: d, path: d.yaml}\n"
)


def _layered(tmp_path, make_tree):
    """A global tree, one environment and two modules; returns ``(root, kwargs)``."""
    root = make_tree(
        _CONFIG,
        files={
            "data/nodes/n1.yaml": "node_key: n\nshared: from_node\n",
            "data/globs/a.yaml": "glob_a: 1\n",
            "data/globs/b.yaml": "glob_b: 1\nshared: from_glob\n",
            "data/common.yaml": (
                "lookup_options:\n  merged: {merge: deep}\n"
                "common_key: c\nshared: from_common\n"
                "merged: {x: 1}\n'dotted.key': dot\n"
            ),
        },
    )
    envs = tmp_path / "envs"
    _write(envs / "prod" / "hiera.yaml", _ENV)
    _write(envs / "prod" / "data" / "e.yaml", "env_key: e\nshared: from_env\n")
    mods = envs / "prod" / "modules"
    _write(mods / "modb" / "hiera.yaml", _MODULE)
    _write(
        mods / "modb" / "data" / "m.yaml",
        "lookup_options:\n  modb::opt: {merge: deep}\nmodb::one: 1\nstray: 2\n",
    )
    _write(mods / "modb" / "data" / "d.yaml", "modb::fallback: 3\nstray_d: 4\n")
    _write(mods / "moda" / "hiera.yaml", _MODULE)
    _write(mods / "moda" / "data" / "m.yaml", "moda::one: 1\n")
    return root, {
        "environmentpath": str(envs),
        "scope": Scope(facts={"host": "n1"}, environment="prod"),
    }


def _hiera(tmp_path, make_tree, **extra):
    root, kwargs = _layered(tmp_path, make_tree)
    kwargs.update(extra)
    return Hiera(str(root / "hiera.yaml"), **kwargs)


def test_keys_follow_precedence_across_levels_and_layers(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    assert h.keys() == [
        "node_key",
        "shared",
        "glob_a",
        "glob_b",
        "common_key",
        "merged",
        "dotted.key",
        "env_key",
        "moda::one",
        "modb::one",
        "modb::fallback",
    ]


def test_keys_returns_a_new_list_of_str(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    first = h.keys()
    first.append("mutated")
    assert "mutated" not in h.keys()
    assert all(type(key) is str for key in first)


def test_a_key_at_two_levels_is_listed_once(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    assert h.keys().count("shared") == 1


def test_lookup_options_is_never_listed(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    assert "lookup_options" not in h.keys()


def test_a_module_lists_only_keys_in_its_own_namespace(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    keys = h.keys()
    assert "stray" not in keys and "stray_d" not in keys
    assert "modb::one" in keys


def test_default_hierarchy_keys_follow_the_regular_ones(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    keys = h.keys()
    assert keys.index("modb::fallback") > keys.index("modb::one")
    assert keys[-1] == "modb::fallback"


def test_a_lookup_key_level_is_skipped(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "s.yaml"},
                {"name": "c", "path": "c.yaml"},
            ]
        },
        files={"data/s.yaml": "x", "data/c.yaml": "only: 1\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.keys() == ["only"]
    assert calls == []


def test_a_mapped_paths_level_is_enumerated(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "apps", "mapped_paths": ["apps", "app", "%{app}.yaml"]}
            ]
        },
        files={"data/a1.yaml": "k1: 1\n", "data/a2.yaml": "k2: 2\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"apps": ["a1", "a2"]}))
    assert h.keys() == ["k1", "k2"]


def test_a_missing_file_contributes_nothing(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "gone", "path": "gone.yaml"},
                {"name": "c", "path": "c.yaml"},
            ]
        },
        files={"data/c.yaml": "only: 1\n"},
    )
    assert Hiera(str(root / "hiera.yaml")).keys() == ["only"]


def test_a_scoped_view_enumerates_its_own_files(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    other = h.scoped(facts={"host": "nobody"})
    assert "node_key" in h.keys()
    assert "node_key" not in other.keys()
    assert "common_key" in other.keys()


def test_a_file_added_between_calls_appears_only_when_revalidating(tmp_path, make_tree):
    root, kwargs = _layered(tmp_path, make_tree)
    live = Hiera(str(root / "hiera.yaml"), **kwargs)
    frozen = Hiera(str(root / "hiera.yaml"), revalidate=False, **kwargs)
    assert "late" not in live.keys() and "late" not in frozen.keys()

    _write(root / "data" / "globs" / "z.yaml", "late: 1\n")

    assert "late" in live.keys()
    assert "late" not in frozen.keys()
    frozen.clear_cache()
    assert "late" in frozen.keys()


def test_a_changed_file_is_seen_when_revalidating(tmp_path, make_tree):
    root, kwargs = _layered(tmp_path, make_tree)
    h = Hiera(str(root / "hiera.yaml"), **kwargs)
    assert "renamed" not in h.keys()
    _write(root / "data" / "common.yaml", "renamed: 1\n")
    assert h.keys() == ["node_key", "shared", "glob_a", "glob_b", "renamed"] + [
        "env_key",
        "moda::one",
        "modb::one",
        "modb::fallback",
    ]


def test_a_non_string_key_is_not_listed(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "1: one\ntext: t\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.keys() == ["text"]


def test_a_non_string_module_key_warns_as_a_lookup_does(tmp_path, make_tree, caplog):
    root, kwargs = _layered(tmp_path, make_tree)
    _write(
        tmp_path / "envs" / "prod" / "modules" / "moda" / "data" / "m.yaml",
        "moda::one: 1\n7: seven\n",
    )
    config = str(root / "hiera.yaml")
    with caplog.at_level(logging.WARNING):
        keys = Hiera(config, **kwargs).keys()
    assert "moda::one" in keys and 7 not in keys
    listed = [m for m in (r.getMessage() for r in caplog.records) if "got 7" in m]
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Hiera(config, **kwargs).lookup("moda::one")
    looked_up = [m for m in (r.getMessage() for r in caplog.records) if "got 7" in m]
    assert listed and listed == looked_up


def test_a_data_file_that_is_not_a_hash_fails_as_a_lookup_does(make_tree):
    root = make_tree(
        {
            "defaults": {"data_hash": "json_data"},
            "hierarchy": [{"name": "c", "path": "c.json"}],
        },
        files={"data/c.json": "[1, 2]"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as lookup_error:
        h.lookup("k")
    with pytest.raises(BackendError) as keys_error:
        h.keys()
    assert str(keys_error.value) == str(lookup_error.value)


def test_every_listed_key_is_found_by_an_exact_lookup(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    for key in h.keys():
        assert (key,) in h


def test_a_hiera_with_no_data_has_no_keys(make_tree):
    root = make_tree({"hierarchy": [{"name": "c", "path": "c.yaml"}]})
    assert Hiera(str(root / "hiera.yaml")).keys() == []


# --- to_dict ---------------------------------------------------------


def test_to_dict_equals_one_exact_lookup_per_key(tmp_path, make_tree):
    h = _hiera(tmp_path, make_tree)
    expected = {key: h.lookup((key,)) for key in h.keys()}
    result = h.to_dict()
    assert result == expected
    assert list(result) == h.keys()
    assert result["dotted.key"] == "dot"
    assert result["shared"] == "from_node"


def test_to_dict_applies_an_explicit_merge_to_every_key(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "b", "path": "b.yaml"},
            ]
        },
        files={"data/a.yaml": "h: {x: 1}\n", "data/b.yaml": "h: {y: 2}\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.to_dict() == {"h": {"x": 1}}
    assert h.to_dict(merge="deep") == {"h": {"x": 1, "y": 2}}


def test_to_dict_applies_each_keys_own_lookup_options(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "b", "path": "b.yaml"},
            ]
        },
        files={
            "data/a.yaml": "h: {x: 1}\nplain: {x: 1}\n",
            "data/b.yaml": (
                "lookup_options:\n  h: {merge: deep}\n" "h: {y: 2}\nplain: {y: 2}\n"
            ),
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.to_dict() == {"h": {"x": 1, "y": 2}, "plain": {"x": 1}}


def test_to_dict_resolves_interpolation(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={
            "data/c.yaml": "base: b\nderived: 'x-%{lookup(\"base\")}-%{facts.host}'\n"
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"host": "n1"}))
    assert h.to_dict() == {"base": "b", "derived": "x-b-n1"}


def test_to_dict_keeps_a_sensitive_value_sensitive(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={
            "data/c.yaml": (
                "lookup_options:\n  secret: {convert_to: Sensitive}\nsecret: hush\n"
            )
        },
    )
    value = Hiera(str(root / "hiera.yaml")).to_dict()["secret"]
    assert isinstance(value, Sensitive)
    assert value.unwrap() == "hush"


def test_mutating_the_result_does_not_change_a_later_lookup(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "h: {x: [1, 2]}\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    result = h.to_dict()
    result["h"]["x"].append(3)
    result["h"]["added"] = True
    result["other"] = 1
    assert h.lookup("h") == {"x": [1, 2]}
    assert h.to_dict() == {"h": {"x": [1, 2]}}


def test_an_interpolation_error_in_one_value_propagates(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "good: 1\nbad: '%{nowhere}'\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(strict="error"))
    with pytest.raises(InterpolationError, match="Undefined variable 'nowhere'"):
        h.to_dict()


def test_to_dict_leaves_out_a_key_whose_lookup_misses(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "kept: 1\ngone: 2\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    real = h.lookup

    def lookup(name, *args, **kwargs):
        if name == ("gone",):
            raise KeyNotFoundError("gone")
        return real(name, *args, **kwargs)

    h.lookup = lookup
    assert h.to_dict() == {"kept": 1}
