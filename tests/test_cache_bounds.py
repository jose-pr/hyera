"""Bounded caches, ``clear_cache()``, lazy file loads and pickling."""

import concurrent.futures
import copy
import pickle
import random
import threading

import pytest

from hyera import Hiera, HieraError, KeyNotFoundError, Scope
from hyera.backends import YAMLBackend
from cache_support import (  # noqa: F401
    _counting_resolver,
)

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

    assert len(h._store._location_cache) == 2

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

    for _ in range(3):
        assert h.lookup("k") == "v"
        # Every lookup rebuilds from scratch -- nothing is ever cached.
        assert calls[0] > builds_before
        builds_before = calls[0]

    assert len(h._store._location_cache) == 0
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
    assert len(h._store._file_cache) == 1


def test_internal_keyerror_is_not_a_miss(make_tree, monkeypatch):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))

    def broken_data_hash(self, path, options, context):
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
    assert len(h._store._location_cache) >= 1
    assert len(h._store._file_cache) >= 1

    v.clear_cache()

    assert len(h._store._location_cache) == 0
    assert len(h._store._file_cache) == 0


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
        assert len(clone._store._location_cache) == 0
        assert len(clone._lookup_options_cache) == 0
        assert len(clone._store._file_cache) == 0
        assert clone.lookup("k") == "v"
        assert len(clone._store._location_cache) >= 1
        assert clone._store is not h._store


def test_views_share_the_instances_store_and_a_copy_does_not(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    view = h.scoped(variables={"role": "web"})
    assert view._store is h._store
    assert view.lookup("k") == "v"
    assert len(h._store._file_cache) == 1
    h.clear_cache()
    assert len(view._store._file_cache) == 0


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


def test_lru_evicts_oldest_past_maxsize_and_reports_len():
    import threading

    from hyera._lookup.cache import _LRU

    lru = _LRU(threading.Lock(), maxsize=2)
    lru.put("a", 1)
    lru.put("b", 2)
    lru.put("c", 3)
    assert len(lru) == 2
    assert lru.get("a", "gone") == "gone"
    assert lru.get("b") == 2
    assert lru.get("c") == 3
