"""Copies, scoped views and the path intern table."""

import copy
import pickle

import pytest

from hyera import Hiera
from hyera._lookup import locations

# --- copies, views and the intern table -------------------------------------


def _single_file_tree(make_tree, text):
    return make_tree(
        {
            "hierarchy": [
                {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": text},
    )


_COPIERS = {
    "copy": copy.copy,
    "deepcopy": copy.deepcopy,
    "pickle": lambda h: pickle.loads(pickle.dumps(h)),
}


@pytest.mark.parametrize("copier", sorted(_COPIERS))
@pytest.mark.parametrize("revalidate", [True, False])
def test_copy_of_a_used_instance_reads_the_disk(make_tree, copier, revalidate):
    root = _single_file_tree(make_tree, "k: SECRET-PLAINTEXT-VALUE\n")
    h = Hiera(str(root / "hiera.yaml"), revalidate=revalidate)
    assert h.lookup("k") == "SECRET-PLAINTEXT-VALUE"
    assert b"SECRET-PLAINTEXT-VALUE" not in pickle.dumps(h)

    (root / "data" / "common.yaml").write_bytes(b"k: CHANGED-ON-DISK\n")
    clone = _COPIERS[copier](h)
    assert len(clone._store._file_cache) == 0
    assert clone.lookup("k") == "CHANGED-ON-DISK"


def test_shallow_copy_does_not_share_caches_with_the_original(make_tree):
    root = _single_file_tree(make_tree, "k: v1\n")
    h = Hiera(str(root / "hiera.yaml"), revalidate=False)
    assert h.lookup("k") == "v1"
    clone = copy.copy(h)
    (root / "data" / "common.yaml").write_bytes(b"k: v2\n")
    clone.clear_cache()
    assert clone.lookup("k") == "v2"
    assert h.lookup("k") == "v1"


def test_clear_cache_reaches_existing_views_both_ways(make_tree):
    root = _single_file_tree(make_tree, "k: v1\n")
    data = root / "data" / "common.yaml"
    h = Hiera(str(root / "hiera.yaml"), revalidate=False)
    view = h.scoped(trusted={"certname": "n1"})
    assert (h.lookup("k"), view.lookup("k")) == ("v1", "v1")

    data.write_bytes(b"k: v2\n")
    assert (h.lookup("k"), view.lookup("k")) == ("v1", "v1")
    h.clear_cache()
    assert (h.lookup("k"), view.lookup("k")) == ("v2", "v2")

    data.write_bytes(b"k: v3\n")
    view.clear_cache()
    assert (h.lookup("k"), view.lookup("k")) == ("v3", "v3")


def test_intern_table_is_bounded_by_live_entries(make_tree):
    root = _single_file_tree(make_tree, "k: v\n")
    h = Hiera(str(root / "hiera.yaml"), cache_size=4)
    for i in range(3000):
        assert h.scoped(trusted={"certname": "n{}".format(i)}).lookup("k") == "v"
    assert len(h._store._location_cache) <= 4
    assert len(h._store._paths) < 2200


def test_intern_table_still_shares_paths_of_live_entries(make_tree):
    root = _single_file_tree(make_tree, "k: v\n")
    h = Hiera(str(root / "hiera.yaml"))
    h.scoped(trusted={"certname": "a"}).lookup("k")
    h.scoped(trusted={"certname": "b"}).lookup("k")
    paths = [
        loc.location
        for entry in h._store._location_cache._entries.values()
        for locations in entry.levels
        for loc in locations
        if loc.location.endswith("common.yaml")
    ]
    assert len(paths) == 2 and paths[0] is paths[1]
