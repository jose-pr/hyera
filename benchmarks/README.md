# Benchmarks

Measures `hyera`'s per-call CPU cost on a generated 771-file Hiera 5 tree (no
network, no external fixtures). Six scenarios cover a first lookup, a run of
distinct keys, a deep merge across 500 glob-matched files, and lookups
against volatile or brand-new scopes.

## Run it

```
<py> benchmarks/run.py               # full tree, prints a report
<py> benchmarks/run.py --quick       # a much smaller tree, for a fast smoke run
<py> benchmarks/run.py --save        # also writes benchmarks/results/<name>.json
<py> benchmarks/run.py --save --name my-run --results-dir ./out
```

Runs are manual — there is no per-push CI job for this suite.

**Compare only results from the same machine and interpreter**, with the
overlay's comparator:

```
py -3 $ENGINEERING_OVERLAY_ROOT/tools/compare_bench.py <before.json> <after.json>
```

Local numbers are sanity checks, never release claims — see the repo's
`AGENTS.md`/`.agents/AGENTS.md` for that rule.

## Schema

One JSON object per `--save` run:

- `name`, `hyera_version`, `python` (`platform.python_version()`),
  `implementation`, `platform` (`platform.platform()`), `processor`
  (`platform.processor()` or `platform.machine()`), `timestamp` (UTC, ISO,
  second precision).
- `iterations`: the inner-call count used for each metric, plus `repeat`
  (the number of timed samples per metric).
- `tree`: `{"files": <n>, "reachable": <m>}` for the generated tree (771/523
  at full size, smaller under `--quick`).
- `metrics.<name>.{min_ms, median_ms, max_ms}`: milliseconds per call,
  rounded to 6 places, across `repeat` samples of `iterations.<name>` calls
  each.

## Metrics

- `construct` — `Hiera(cfg, scope=S0)`: cold construction cost (pre-warms the
  bound scope's own data).
- `lookup.first` — `h.lookup("common::key5")` on one warm instance.
- `lookup.keys100` — 100 (quick: 20) distinct `mod<m>::key<k>` lookups on one
  warm instance.
- `lookup.deep.glob500` — `h.lookup("settings", merge="deep")`: a deep merge
  over every one of the 500 glob-matched module files.
- `lookup.scope.volatile` — a lookup through a fresh view differing only in
  an unreferenced fact (`h.scoped(facts={"uptime_seconds": i})`); the point
  is the first call per scope, never warmed.
- `lookup.scope.new_node` — the same, differing in the referenced
  `clientcert` variable instead (`h.scoped(variables={"clientcert": ...})`),
  so a new node's own data provider must be rebuilt.

## Baseline

`results/hyera-<ver>-py314-baseline.json` and `results/hyera-<ver>-py39-baseline.json`
were recorded before the cache rework: the original unbounded, whole-scope-keyed
caches, no revalidation. Later results in this directory are comparable
against them via `compare_bench.py`.
