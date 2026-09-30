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
<py> benchmarks/run.py --revalidate off --save --name my-run-norevalidate
```

`--revalidate on|off` (default `on`) is passed straight to every `Hiera(...)`
this run builds, and recorded as `"revalidate": true|false` in the saved
JSON.

Runs are manual — there is no per-push CI job for this suite.

**Compare only results from the same machine and interpreter**, with the
overlay's comparator:

```
py -3 $ENGINEERING_OVERLAY_ROOT/tools/compare_bench.py <before.json> <after.json>
```

Local numbers are sanity checks, never release claims — see the repo's
`AGENTS.md` for that rule.

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
- `revalidate`: whether every `Hiera(...)` this run built used
  `revalidate=True` (the default) or `revalidate=False`.
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

## Results (2026-09-30, local)

Machine: `Windows-11-10.0.28000-SP0`, `ARMv8 (64-bit) Family 8 Model 2
Revision 201, Qualcomm Technologies Inc` (ARM64; both interpreters run
under x64 emulation, so these numbers are noisier than a native run — see
the repo's `AGENTS.md`). Interpreters: CPython 3.14.6 and 3.9.13.

**These are local sanity checks, never release claims** — see the repo's
`AGENTS.md`/the project's own standing rule on performance numbers.

`results/hyera-0.0.0a0-py314.json`/`-py39.json` (`revalidate=True`, the
default) against their baselines:

```
hyera-0.0.0a0-py314-baseline -> hyera-0.0.0a0-py314   (median ms/call)
metric                   before      after     change
construct              197.2520   275.5533     +39.7%
lookup.deep.glob500    569.6104   732.1993     +28.5%
lookup.first             0.2011    10.8141   +5276.3%
lookup.keys100           0.0633     7.3029  +11443.4%
lookup.scope.new_node    15.1641    11.4949     -24.2%
lookup.scope.volatile    13.1212    10.1911     -22.3%
```

```
hyera-0.0.0a0-py39-baseline -> hyera-0.0.0a0-py39   (median ms/call)
metric                   before      after     change
construct              224.5302   237.8708      +5.9%
lookup.deep.glob500    621.7106   731.4991     +17.7%
lookup.first             0.1920    11.3889   +5831.4%
lookup.keys100           0.0604    10.7388  +17671.2%
lookup.scope.new_node    15.0362    11.6630     -22.4%
lookup.scope.volatile    17.2018    14.0066     -18.6%
```

`lookup.first`/`lookup.keys100`/`lookup.deep.glob500` cost more with
revalidation on: each now pays one filesystem probe per candidate location
per lookup (about 523 on this tree) that the pre-rework code never paid.
This is the documented, intended cost of `revalidate=True` (see
`src/hyera/AGENTS.md`), not a regression. `lookup.scope.new_node`/
`lookup.scope.volatile` dropped sharply either way: this is the referenced-
variable-keyed cache doing its job — a scope differing only in an
unreferenced fact, or a brand-new node whose own location was already
built by an earlier scope's rebuild, now reuses a cached entry instead of
rebuilding one from scratch every time.

`results/hyera-0.0.0a0-py314-norevalidate.json`/`-py39-norevalidate.json`
(`revalidate=False`) against their baselines:

```
hyera-0.0.0a0-py314-baseline -> hyera-0.0.0a0-py314-norevalidate   (median ms/call)
metric                   before      after     change
construct              197.2520   228.5242     +15.9%
lookup.deep.glob500    569.6104   664.2431     +16.6%
lookup.first             0.2011     0.6842    +240.1%
lookup.keys100           0.0633     0.1147     +81.3%
lookup.scope.new_node    15.1641     3.1634     -79.1%
lookup.scope.volatile    13.1212     0.6026     -95.4%
```

```
hyera-0.0.0a0-py39-baseline -> hyera-0.0.0a0-py39-norevalidate   (median ms/call)
metric                   before      after     change
construct              224.5302   231.4462      +3.1%
lookup.deep.glob500    621.7106   733.0183     +17.9%
lookup.first             0.1920     0.7759    +304.1%
lookup.keys100           0.0604     0.1430    +136.6%
lookup.scope.new_node    15.0362     3.5206     -76.6%
lookup.scope.volatile    17.2018     0.7744     -95.5%
```

`construct` and `lookup.deep.glob500` did not reproduce as regressions on
repeated runs (each pair was re-run three times; those two settled back to
within/near the 10% threshold on the 2nd and 3rd run of both interpreters —
noise on this emulated-ARM64 machine, not a persistent effect).
`lookup.first` and `lookup.keys100`, however, stayed regressed by roughly
2-3.5x across all three runs on both interpreters — recorded, not hidden,
as `cache-rework-lookup-first-keys100-regressed-with-revalidate-false` in
the project's own findings queue: both are now sub-millisecond, "already
fast" operations whose absolute cost is dominated by the new per-lookup
bookkeeping (the referenced-variable cache's own lookup path, the
`Invocation`/filesystem-memo plumbing) even on a clean cache hit, rather
than by any filesystem work. `lookup.scope.new_node`/`lookup.scope.volatile`
improved sharply here too, for the same reason as above.
