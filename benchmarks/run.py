"""Benchmark suite: per-call CPU cost of ``hyera`` lookups on a generated tree.

Generates a Hiera 5 tree modeled on the 2026-09-28 review's ``mkperf.py``
fixture (five levels: a per-node path, a per-role path, a mapped ``apps``
level, a glob ``modules/*.yaml`` level, and ``common.yaml``) into a temporary
directory, times six scenarios against it, and prints one aligned line per
metric. ``--save`` also writes a JSON result file; see ``benchmarks/README.md``
for the schema and how to compare two files with the overlay's
``compare_bench.py``.

Only ``hyera``'s public API is used here (``Hiera``, ``Scope``, ``.lookup()``,
``.scoped()``), so this file runs unchanged across every phase of the cache
rework it benchmarks.

Local numbers from this script are sanity checks only, never a performance
claim -- see ``benchmarks/README.md``.
"""

import argparse
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import hyera  # noqa: E402
from hyera import Hiera, Scope  # noqa: E402

#: The full-size tree: 771 files, 523 reachable from the base scope below
#: (1 node + 1 role + 20 apps + 500 modules + 1 common).
_FULL = {
    "nodes": 200,
    "roles": 50,
    "apps": 20,
    "modules": 500,
    "keys_per_mod": 40,
    "common": 1000,
}
#: A much smaller tree for a fast smoke run (``--quick``).
_QUICK = {
    "nodes": 5,
    "roles": 3,
    "apps": 3,
    "modules": 20,
    "keys_per_mod": 5,
    "common": 50,
}

_RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _dump(path: Path, obj) -> None:
    """Write ``obj`` as YAML, UTF-8, LF -- never the platform's line ending."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)
    path.write_bytes(text.encode("utf-8"))


def build_tree(root: Path, *, quick: bool = False) -> dict:
    """Write the benchmark hierarchy under ``root``.

    Same shape as mkperf's fixture, with ``nodes/%{trusted.certname}.yaml``
    in place of its ``%{certname}`` node level (Hiera 5 has no bare
    ``certname`` fact; ``trusted.certname`` is what a real config would use)
    and a value template inside every module file's keys following the same
    substitution, so no scope variable is left permanently undefined. Sizes
    are mkperf's full counts, or the much smaller ``_QUICK`` set; mkperf's
    5000-entry ``big`` key is dropped (not needed by any metric here).

    Returns ``{"files": n, "reachable": m}`` -- 771/523 at full size.
    """
    sizes = _QUICK if quick else _FULL
    n_nodes, n_roles, n_apps = sizes["nodes"], sizes["roles"], sizes["apps"]
    n_mods = sizes["modules"]
    keys_per_mod = sizes["keys_per_mod"]
    n_common = sizes["common"]
    data = root / "data"

    cfg = {
        "version": 5,
        "defaults": {"datadir": "data", "data_hash": "yaml_data"},
        "hierarchy": [
            {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
            {"name": "role", "path": "roles/%{role}.yaml"},
            {"name": "apps", "mapped_paths": ["apps", "app", "apps/%{app}.yaml"]},
            {"name": "modules", "glob": "modules/*.yaml"},
            {"name": "common", "path": "common.yaml"},
        ],
    }
    _dump(root / "hiera.yaml", cfg)

    for i in range(n_nodes):
        _dump(
            data / "nodes" / "node{}.yaml".format(i),
            {"node_key": "n{}".format(i), "classes": ["nodecls{}".format(i)]},
        )
    for i in range(n_roles):
        name = "web" if i == 0 else "role{}".format(i)
        _dump(
            data / "roles" / "{}.yaml".format(name),
            {"role_key": "r{}".format(i), "classes": ["rolecls{}".format(i)]},
        )
    for i in range(n_apps):
        _dump(
            data / "apps" / "app{}.yaml".format(i),
            {"app_key": "a{}".format(i), "classes": ["appcls{}".format(i)]},
        )
    for m in range(n_mods):
        entry = {
            "mod{}::key{}".format(m, k): "v{}.{} %{{trusted.certname}}".format(m, k)
            for k in range(keys_per_mod)
        }
        entry["classes"] = ["modcls{}".format(m), "shared"]
        entry["settings"] = {
            "m{}".format(m): {"x": m, "list": [m, m + 1]},
            "common": {"k": m},
        }
        _dump(data / "modules" / "mod{:04d}.yaml".format(m), entry)
    common = {"common::key{}".format(k): "c{}".format(k) for k in range(n_common)}
    common["classes"] = ["base"]
    common["lookup_options"] = {
        "^settings$": {"merge": "deep"},
        "classes": {"merge": "unique"},
    }
    _dump(data / "common.yaml", common)

    total = n_nodes + n_roles + n_apps + n_mods + 1
    reachable = 1 + 1 + n_apps + n_mods + 1  # node + role + apps + modules + common
    return {"files": total, "reachable": reachable}


def _base_scope(n_apps: int) -> Scope:
    return Scope(
        variables={"role": "web", "clientcert": "node7"},
        facts={
            "os": {"family": "RedHat"},
            "apps": ["app{}".format(i) for i in range(n_apps)],
            "uptime_seconds": 0,
        },
    )


def _time_calls(repeat: int, inner: int, call, *, warm: bool) -> "list[float]":
    """Run ``call(i)`` ``inner`` times per sample, ``repeat`` samples; return
    one ms-per-call value per sample. ``call`` receives a globally increasing
    call index, never reset between samples, so a metric that cycles through
    a pre-built sequence (a set of keys, a set of pre-built scoped views) does
    not repeat the same early elements every sample.
    """
    index = 0
    if warm:
        call(-1)
    samples = []
    for _ in range(repeat):
        start = time.perf_counter()
        for _ in range(inner):
            call(index)
            index += 1
        elapsed = time.perf_counter() - start
        samples.append(elapsed * 1000.0 / inner)
    return samples


def _build_metrics(cfg_path: Path, quick: bool, revalidate: bool = True):
    """Return ``(iterations, metric_fns)``: ``iterations`` maps each metric
    name to its inner-call count (for the saved JSON); ``metric_fns`` maps it
    to ``(warm, inner, call)`` per :func:`_time_calls`.
    """
    sizes = _QUICK if quick else _FULL
    n_apps = sizes["apps"]
    keys_per_mod = sizes["keys_per_mod"]
    keys100_n = 20 if quick else 100

    scope0 = _base_scope(n_apps)

    iterations = {}
    metrics = {}

    def construct_call(_i):
        Hiera(str(cfg_path), scope=scope0, revalidate=revalidate)

    inner = 1 if quick else 3
    iterations["construct"] = inner
    metrics["construct"] = (False, inner, construct_call)

    h = Hiera(str(cfg_path), scope=scope0, revalidate=revalidate)

    def first_call(_i):
        h.lookup("common::key5")

    inner = 20 if quick else 200
    iterations["lookup.first"] = inner
    metrics["lookup.first"] = (True, inner, first_call)

    def keys100_call(i):
        m = i % keys100_n
        h.lookup("mod{}::key{}".format(m, m % keys_per_mod))

    inner = keys100_n
    iterations["lookup.keys100"] = inner
    metrics["lookup.keys100"] = (True, inner, keys100_call)

    def deep_call(_i):
        h.lookup("settings", merge="deep")

    inner = 3 if quick else 10
    iterations["lookup.deep.glob500"] = inner
    metrics["lookup.deep.glob500"] = (True, inner, deep_call)

    repeat = 2 if quick else 7

    volatile_inner = 10 if quick else 50
    volatile_views = [
        h.scoped(facts={"uptime_seconds": i}) for i in range(repeat * volatile_inner)
    ]

    def volatile_call(i):
        volatile_views[i].lookup("common::key5")

    iterations["lookup.scope.volatile"] = volatile_inner
    metrics["lookup.scope.volatile"] = (False, volatile_inner, volatile_call)

    new_node_inner = 5 if quick else 20
    new_node_views = [
        h.scoped(variables={"clientcert": "bench{}".format(i)})
        for i in range(repeat * new_node_inner)
    ]

    def new_node_call(i):
        new_node_views[i].lookup("common::key5")

    iterations["lookup.scope.new_node"] = new_node_inner
    metrics["lookup.scope.new_node"] = (False, new_node_inner, new_node_call)

    iterations["repeat"] = repeat
    return repeat, iterations, metrics


#: Print/save order (also the order metrics are built in, above).
_METRIC_ORDER = (
    "construct",
    "lookup.first",
    "lookup.keys100",
    "lookup.deep.glob500",
    "lookup.scope.volatile",
    "lookup.scope.new_node",
)


def run(*, quick: bool, revalidate: bool = True) -> dict:
    """Build the tree, run every metric, and return the result dict (the
    same shape ``--save`` writes to JSON, without the file-only bookkeeping
    fields)."""
    with tempfile.TemporaryDirectory(prefix="hyera-bench-") as tmp:
        root = Path(tmp)
        tree = build_tree(root, quick=quick)
        cfg_path = root / "hiera.yaml"
        repeat, iterations, metric_fns = _build_metrics(cfg_path, quick, revalidate)

        results = {}
        for name in _METRIC_ORDER:
            warm, inner, call = metric_fns[name]
            samples = _time_calls(repeat, inner, call, warm=warm)
            results[name] = {
                "min_ms": round(min(samples), 6),
                "median_ms": (
                    round(sorted(samples)[len(samples) // 2], 6)
                    if len(samples) % 2
                    else round(
                        (
                            sorted(samples)[len(samples) // 2 - 1]
                            + sorted(samples)[len(samples) // 2]
                        )
                        / 2,
                        6,
                    )
                ),
                "max_ms": round(max(samples), 6),
            }

    return {
        "hyera_version": hyera.__version__,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "iterations": iterations,
        "tree": tree,
        "revalidate": revalidate,
        "metrics": results,
    }


def _print_report(name: str, result: dict) -> None:
    print(
        "{}   ({} files, {} reachable)".format(
            name, result["tree"]["files"], result["tree"]["reachable"]
        )
    )
    print("{:22s} {:>10s} {:>10s} {:>10s}".format("metric", "median", "min", "max"))
    for metric in _METRIC_ORDER:
        m = result["metrics"][metric]
        print(
            "{:22s} {:10.4f} {:10.4f} {:10.4f}".format(
                metric, m["median_ms"], m["min_ms"], m["max_ms"]
            )
        )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Benchmark hyera's per-call CPU cost on a generated tree."
    )
    ap.add_argument("--save", action="store_true", help="write a JSON result file")
    ap.add_argument(
        "--name", default=None, help="result name (default: hyera-<ver>-py<NN>)"
    )
    ap.add_argument(
        "--quick", action="store_true", help="a much smaller tree, for a fast smoke run"
    )
    ap.add_argument(
        "--results-dir",
        type=Path,
        default=None,
        help="where --save writes (default: benchmarks/results)",
    )
    ap.add_argument(
        "--revalidate",
        choices=("on", "off"),
        default="on",
        help="Hiera(..., revalidate=...) for every instance this run builds (default: on)",
    )
    args = ap.parse_args(argv)

    revalidate = args.revalidate == "on"
    result = run(quick=args.quick, revalidate=revalidate)
    default_name = "hyera-{}-py{}{}".format(
        hyera.__version__, sys.version_info.major, sys.version_info.minor
    )
    name = args.name or default_name
    result = dict(result)
    result["name"] = name

    _print_report(name, result)

    if args.save:
        results_dir = args.results_dir or _RESULTS_DIR
        results_dir.mkdir(parents=True, exist_ok=True)
        out_path = results_dir / "{}.json".format(name)
        # `name` is user/default supplied and never carries a path; write
        # `sort_keys=True` for a stable diff, a trailing newline, no host
        # identifiers anywhere in the payload (see the module docstring).
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(result, f, indent=2, sort_keys=True)
            f.write("\n")
        print("\nSaved {}".format(out_path))

    return 0


if __name__ == "__main__":
    sys.exit(main())
