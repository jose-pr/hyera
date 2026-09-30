"""Location and ``lookup_options`` caches keyed on the variables the
hierarchy's own interpolation reads (Puppet's ``scope_interpolations_
stable?``), not on the whole scope value -- so a scope that differs only in
an unreferenced fact or variable shares a cached entry with one already
seen, while a scope that differs in a referenced one gets its own.
"""

import concurrent.futures
import copy
import logging
import pickle
import random

import pytest

import hyera.core as core
from hyera import Hiera, HieraError, KeyNotFoundError, Scope
from hyera.backends import YAMLBackend


def _counting_resolver(monkeypatch):
    """Wrap ``core.resolve_locations`` with a call counter: one call per
    hierarchy level per build, so ``calls[0] // n_levels`` is the number of
    distinct location builds a test's assertions care about.
    """
    calls = [0]
    original = core.resolve_locations

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "resolve_locations", counting)
    return calls


def test_unreferenced_variable_shares_location_entry(make_tree, monkeypatch):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/nodes/n1.yaml": "k: node_n1\n", "data/common.yaml": "k: common\n"},
    )
    calls = _counting_resolver(monkeypatch)
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(variables={"clientcert": "n1"}, facts={"uptime_seconds": 0}),
    )
    builds_after_construct = calls[0] // 2

    for i in range(50):
        view = h.scoped(facts={"uptime_seconds": i})
        assert view.lookup("k") == "node_n1"

    assert calls[0] // 2 == builds_after_construct
    assert len(h._location_cache) == 1


def test_referenced_variable_change_rebuilds(make_tree, monkeypatch):
    root = make_tree(
        {"hierarchy": [{"name": "node", "path": "nodes/%{trusted.certname}.yaml"}]},
        files={"data/nodes/a.yaml": "k: node_a\n", "data/nodes/b.yaml": "k: node_b\n"},
    )
    calls = _counting_resolver(monkeypatch)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"clientcert": "a"}))

    results = []
    for cc in ("a", "b", "a", "b"):
        view = h.scoped(variables={"clientcert": cc})
        results.append(view.lookup("k"))

    assert results == ["node_a", "node_b", "node_a", "node_b"]
    assert len(h._location_cache) == 2
    assert calls[0] // 1 == 2  # one level -> one resolve_locations call per build


def test_type_tagged_values_do_not_collide(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "v", "path": "v/%{x}.yaml"}]},
        files={
            "data/v/true.yaml": "picked: bool\n",
            "data/v/1.yaml": "picked: int\n",
            "data/v/1.0.yaml": "picked: float\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"x": True}))
    int_view = h.scoped(variables={"x": 1})
    float_view = h.scoped(variables={"x": 1.0})

    assert h.lookup("picked") == "bool"
    assert int_view.lookup("picked") == "int"
    assert float_view.lookup("picked") == "float"
    # Reversed order, to catch a cache that conflates True/1/1.0.
    assert float_view.lookup("picked") == "float"
    assert int_view.lookup("picked") == "int"
    assert h.lookup("picked") == "bool"

    assert len(h._location_cache) == 3


def test_segment_reference_keys_on_segment_value(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "os", "path": "os/%{facts.os.family}.yaml"}]},
        files={
            "data/os/RedHat.yaml": "k: redhat_val\n",
            "data/os/Debian.yaml": "k: debian_val\n",
        },
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(facts={"os": {"family": "RedHat", "release": 8}}),
    )
    assert h.lookup("k") == "redhat_val"

    same_family = h.scoped(facts={"os": {"family": "RedHat", "release": 9}})
    assert same_family.lookup("k") == "redhat_val"
    assert len(h._location_cache) == 1

    other_family = h.scoped(facts={"os": {"family": "Debian", "release": 8}})
    assert other_family.lookup("k") == "debian_val"
    assert len(h._location_cache) == 2


def test_mapped_paths_item_variable_not_recorded(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "apps", "mapped_paths": ["apps", "app", "apps/%{app}.yaml"]}
            ]
        },
        files={"data/apps/a1.yaml": "k: a1\n", "data/apps/a2.yaml": "k: a2\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(variables={"app": "topscope1"}, facts={"apps": ["a1", "a2"]}),
    )
    assert h.lookup("k") == "a1"

    diff_top_scope_app = h.scoped(variables={"app": "topscope2"})
    assert diff_top_scope_app.lookup("k") == "a1"
    assert len(h._location_cache) == 1

    diff_apps = h.scoped(facts={"apps": ["a1"]})
    assert diff_apps.lookup("k") == "a1"
    assert len(h._location_cache) == 2

    # A second, separate level reading the *top-scope* `app` variable
    # explicitly (`%{::app}`, outside any mapped_paths item layer) is a
    # genuine reference: changing it must still rebuild.
    root2 = make_tree(
        {
            "hierarchy": [
                {"name": "apps", "mapped_paths": ["apps", "app", "apps/%{app}.yaml"]},
                {"name": "top", "path": "top/%{::app}.yaml"},
            ]
        },
        files={
            "data/apps/a1.yaml": "k: a1\n",
            "data/top/topscope1.yaml": "topkey: top1\n",
            "data/top/topscope2.yaml": "topkey: top2\n",
        },
        root="cfg2",
    )
    h2 = Hiera(
        str(root2 / "hiera.yaml"),
        scope=Scope(variables={"app": "topscope1"}, facts={"apps": ["a1"]}),
    )
    assert h2.lookup("topkey") == "top1"
    v2 = h2.scoped(variables={"app": "topscope2"})
    assert v2.lookup("topkey") == "top2"
    assert len(h2._location_cache) == 2


def test_lookup_options_refs_key_their_cache(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "role", "path": "roles/%{role}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": (
                'lookup_options:\n  "%{prefix}::list":\n    merge: unique\n'
                "a::list: [c1]\nb::list: [c2]\n"
            ),
            "data/roles/web.yaml": "a::list: [r1]\nb::list: [r2]\n",
        },
    )
    h = Hiera(
        str(root / "hiera.yaml"), scope=Scope(facts={"role": "web", "prefix": "a"})
    )

    for prefix, expect_a, expect_b in [
        ("a", ["r1", "c1"], ["r2"]),
        ("b", ["r1"], ["r2", "c2"]),
        ("a", ["r1", "c1"], ["r2"]),
        ("b", ["r1"], ["r2", "c2"]),
    ]:
        view = h.scoped(facts={"prefix": prefix})
        assert view.lookup("a::list") == expect_a
        assert view.lookup("b::list") == expect_b

    assert len(h._location_cache) == 1
    assert len(h._lookup_options_cache) == 2


def test_lookup_options_with_sub_lookup_recomputed(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "role", "path": "roles/%{role}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": (
                "lookup_options:\n"
                "  \"%{lookup('prefix')}::list\":\n"
                "    merge: unique\n"
                'prefix: "%{team}"\n'
                "a::list: [c1]\nb::list: [c2]\n"
            ),
            "data/roles/web.yaml": "a::list: [r1]\nb::list: [r2]\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"role": "web", "team": "a"}))

    for team, expect_a, expect_b in [
        ("a", ["r1", "c1"], ["r2"]),
        ("b", ["r1"], ["r2", "c2"]),
        ("a", ["r1", "c1"], ["r2"]),
        ("b", ["r1"], ["r2", "c2"]),
    ]:
        view = h.scoped(facts={"team": team})
        assert view.lookup("a::list") == expect_a
        assert view.lookup("b::list") == expect_b

    assert len(h._lookup_options_cache) == 0


def test_undefined_location_variable_replays_warning(make_tree, monkeypatch, caplog):
    with caplog.at_level(logging.WARNING):
        root = make_tree(
            {
                "hierarchy": [
                    {"name": "role", "path": "roles/%{role}.yaml"},
                    {"name": "common", "path": "common.yaml"},
                ]
            },
            files={"data/common.yaml": "k: v\n"},
        )
        h = Hiera(str(root / "hiera.yaml"))

        calls = []
        original = Scope.lookupvar

        def counting_lookupvar(self, name, *, lenient=False):
            calls.append((name, lenient))
            return original(self, name, lenient=lenient)

        monkeypatch.setattr(Scope, "lookupvar", counting_lookupvar)

        for i in range(5):
            before = len(calls)
            view = h.scoped(facts={"tag": i})
            assert view.lookup("k") == "v"
            assert any(
                name == "role" and lenient for name, lenient in calls[before:]
            ), "expected a lookupvar('role', lenient=True) replay for view {}".format(i)

    role_warnings = [
        r for r in caplog.records if "Undefined variable 'role'" in r.getMessage()
    ]
    assert len(role_warnings) == 1


def test_location_entries_share_path_strings(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": "k: v\n",
            "data/nodes/a.yaml": "k: a\n",
            "data/nodes/b.yaml": "k: b\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"clientcert": "a"}))
    v = h.scoped(variables={"clientcert": "b"})
    assert v.lookup("k") == "b"

    entries = list(h._location_cache._entries.values())
    assert len(entries) == 2
    common_paths = []
    for entry in entries:
        for _function_name, locations in entry.levels:
            for loc in locations:
                if loc.path.endswith("common.yaml"):
                    common_paths.append(loc.path)
    assert len(common_paths) == 2
    assert common_paths[0] is common_paths[1]


@pytest.mark.parametrize("cache_size", [0, 2, None])
def test_shared_instance_matches_fresh_instances(make_tree, cache_size):
    config = {
        "hierarchy": [
            {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
            {"name": "os", "path": "os/%{facts.os.family}.yaml"},
            {"name": "role", "path": "roles/%{role}.yaml"},
            {"name": "apps", "mapped_paths": ["apps", "app", "apps/%{app}.yaml"]},
            {"name": "mods", "glob": "mods/*.yaml"},
            {"name": "common", "path": "common.yaml"},
        ]
    }
    clientcerts = ["n0", "n1", "n2", "n3", "n4"]
    families = ["RedHat", "Debian", "Ubuntu"]
    roles = ["web", "db", "cache"]
    all_apps = ["app0", "app1", "app2"]

    files = {
        "data/common.yaml": (
            "lookup_options:\n"
            "  classes: {merge: unique}\n"
            "  settings: {merge: deep}\n"
            "first_key: common_first\n"
            "classes: [base]\n"
            "settings: {common: {c: 1}}\n"
            'motd: "%{trusted.certname}"\n'
        )
    }
    for cc in clientcerts:
        files["data/nodes/{}.yaml".format(cc)] = (
            "first_key: node_{0}\n"
            "classes: [node_{0}]\n"
            "settings: {{node_{0}: {{n: 1}}}}\n".format(cc)
        )
    for fam in families:
        files["data/os/{}.yaml".format(fam)] = "classes: [{}]\n".format(fam.lower())
    for role in roles:
        files["data/roles/{}.yaml".format(role)] = "classes: [{}]\n".format(role)
    for app in all_apps:
        files["data/apps/{}.yaml".format(app)] = "classes: [{}]\n".format(app)
    for m in range(3):
        files["data/mods/mod{}.yaml".format(m)] = (
            "classes: [mod{0}]\nsettings: {{mod{0}: {{m: {0}}}}}\n".format(m)
        )

    root = make_tree(config, files=files)
    cfg_path = str(root / "hiera.yaml")

    shared = Hiera(cfg_path, cache_size=cache_size)

    rng = random.Random(20260929)
    for _ in range(40):
        cc = rng.choice(clientcerts)
        fam = rng.choice(families)
        role = rng.choice(roles)
        apps = rng.sample(all_apps, rng.randint(0, len(all_apps)))
        uptime = rng.randint(0, 10000)
        scope = Scope(
            variables={"clientcert": cc, "role": role},
            facts={"os": {"family": fam}, "apps": apps, "uptime_seconds": uptime},
        )
        view = shared._view(scope)
        fresh = Hiera(cfg_path, scope=scope)
        for key in ("first_key", "classes", "settings", "motd"):
            assert view.lookup(key) == fresh.lookup(key), (scope, key)


# --- bounded caches, clear_cache(), lazy file loads, private caches -------


def test_cache_size_bounds_and_evicts_lru(make_tree, monkeypatch):
    root = make_tree(
        {"hierarchy": [{"name": "node", "path": "nodes/%{trusted.certname}.yaml"}]},
        files={
            "data/nodes/a.yaml": "k: node_a\n",
            "data/nodes/b.yaml": "k: node_b\n",
            "data/nodes/c.yaml": "k: node_c\n",
        },
    )
    calls = _counting_resolver(monkeypatch)
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(variables={"clientcert": "a"}),
        cache_size=2,
    )
    assert h.lookup("k") == "node_a"
    assert h.scoped(variables={"clientcert": "b"}).lookup("k") == "node_b"
    assert h.scoped(variables={"clientcert": "a"}).lookup("k") == "node_a"
    assert h.scoped(variables={"clientcert": "c"}).lookup("k") == "node_c"

    assert len(h._location_cache) == 2

    builds_before = calls[0]
    assert h.scoped(variables={"clientcert": "a"}).lookup("k") == "node_a"
    assert calls[0] == builds_before, "'a' should still be cached (most recently used)"

    assert h.scoped(variables={"clientcert": "b"}).lookup("k") == "node_b"
    assert calls[0] == builds_before + 1, "'b' should have been evicted"


def test_cache_size_zero_caches_nothing(make_tree, monkeypatch):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    calls = _counting_resolver(monkeypatch)
    h = Hiera(str(root / "hiera.yaml"), cache_size=0)
    builds_before = calls[0]
    assert builds_before > 0  # construction itself already rebuilt

    for _ in range(3):
        assert h.lookup("k") == "v"
        # Every lookup rebuilds from scratch -- nothing is ever cached.
        assert calls[0] > builds_before
        builds_before = calls[0]

    assert len(h._location_cache) == 0
    assert len(h._lookup_options_cache) == 0


@pytest.mark.parametrize(
    "cache_size, error",
    [(-1, ValueError), ("2", TypeError), (True, TypeError)],
    ids=["-1", '"2"', "True"],
)
def test_cache_size_validation(make_tree, cache_size, error):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    with pytest.raises(error):
        Hiera(str(root / "hiera.yaml"), cache_size=cache_size)


def test_clear_cache_then_lookup_finds_value(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: common\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "common"

    h.clear_cache()

    assert h.lookup("k") == "common"
    assert "k" in h
    assert len(h._file_cache) == 1


def test_internal_keyerror_is_not_a_miss(make_tree, monkeypatch):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    # Construct first: the pre-warm's own lookup_options gather parses
    # every file while the real backend is still in place.
    h = Hiera(str(root / "hiera.yaml"))

    def broken_data_hash(self, path, options):
        raise KeyError("boom")

    monkeypatch.setattr(YAMLBackend, "data_hash", broken_data_hash)
    h.clear_cache()

    with pytest.raises(HieraError) as excinfo:
        h.lookup("k")
    assert not isinstance(excinfo.value, KeyNotFoundError)

    with pytest.raises(HieraError):
        "k" in h


def test_clear_cache_through_scoped_view(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    v = h.scoped(environment="production")
    assert v.lookup("k") == "v"
    assert len(h._location_cache) >= 1
    assert len(h._file_cache) >= 1

    v.clear_cache()

    assert len(h._location_cache) == 0
    assert len(h._file_cache) == 0


def test_no_public_cache_attribute(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert not hasattr(h, "cache")


def test_pickle_and_deepcopy_start_with_empty_caches(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"

    for clone in (pickle.loads(pickle.dumps(h)), copy.deepcopy(h)):
        assert len(clone._location_cache) == 0
        assert len(clone._lookup_options_cache) == 0
        assert len(clone._file_cache) == 0
        assert clone.lookup("k") == "v"
        assert len(clone._location_cache) >= 1


def test_concurrent_lookups_with_eviction(make_tree):
    n_nodes = 16
    files = {"data/common.yaml": "k: common\n"}
    for i in range(n_nodes):
        files["data/nodes/n{}.yaml".format(i)] = "k: node_{}\n".format(i)
    root = make_tree(
        {
            "hierarchy": [
                {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files=files,
    )
    h = Hiera(str(root / "hiera.yaml"), cache_size=4)
    views = [
        h.scoped(variables={"clientcert": "n{}".format(i)}) for i in range(n_nodes)
    ]
    expected = {i: "node_{}".format(i) for i in range(n_nodes)}
    rng = random.Random(20260930)
    plans = [[rng.randrange(n_nodes) for _ in range(200)] for _ in range(8)]

    def worker(plan):
        return [(i, views[i].lookup("k")) for i in plan]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(worker, plan) for plan in plans]
        for f in futures:
            for i, value in f.result():
                assert value == expected[i]
