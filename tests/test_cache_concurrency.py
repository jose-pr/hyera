"""``lookup_options`` under concurrent lookups."""

import threading

import pytest

from hyera import Hiera, Scope

# --- lookup_options under concurrent lookups -------------------------------


def _race(h, work, threads=8):
    """Run ``work(h, i)`` on ``threads`` threads released together."""
    barrier = threading.Barrier(threads)
    results = [None] * threads

    def run(i):
        barrier.wait()
        results[i] = work(h, i)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"cache_size": 0}, {"revalidate": False}],
    ids=["default", "no-cache", "no-revalidate"],
)
def test_concurrent_lookups_all_apply_lookup_options(make_tree, kwargs):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": "lookup_options:\n  h: {merge: deep}\nh: {b: 2}\n",
            "data/nodes/n1.yaml": "h: {a: 1}\n",
        },
    )
    scope = Scope(trusted={"certname": "n1"})
    wrong = []
    for _ in range(25):
        h = Hiera(str(root / "hiera.yaml"), scope=scope, **kwargs)
        wrong += [
            r for r in _race(h, lambda h, i: h.lookup("h")) if r != {"a": 1, "b": 2}
        ]
    assert wrong == []


def test_concurrent_cold_start_does_not_leave_options_wrong(make_tree):
    layer = (
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data}\n"
        "hierarchy:\n  - {name: common, path: common.yaml}\n"
    )
    root = make_tree(
        layer,
        files={
            "data/common.yaml": "lookup_options:\n  g: {merge: deep}\n"
            "g: {global: 1}\ne: {global: 1}\n",
            "envs/production/hiera.yaml": layer,
            "envs/production/data/common.yaml": "lookup_options:\n"
            "  e: {merge: deep}\ng: {env: 1}\ne: {env: 1}\n",
        },
    )
    expect = {"g": {"global": 1, "env": 1}, "e": {"global": 1, "env": 1}}
    stuck = 0
    for _ in range(40):
        h = Hiera(
            str(root / "hiera.yaml"),
            environmentpath=str(root / "envs"),
            revalidate=False,
        )
        _race(h, lambda h, i: h.lookup("ge"[i % 2]))
        if {k: h.lookup(k) for k in "ge"} != expect:
            stuck += 1
    assert stuck == 0
