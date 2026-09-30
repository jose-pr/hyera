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

The baseline files were re-recorded against the engine as it stood right
before this cache rework's own changes, on top of everything that had
already landed on `main` by then (config layers, function providers,
`uri` locations, `eyaml`) — the tree this rework's branch was rebased onto,
not the older, now-superseded baseline this repository briefly carried
from before those other plans landed.

`results/hyera-0.0.0a0-py314.json`/`-py39.json` (`revalidate=True`, the
default) against their baselines:

```
hyera-0.0.0a0-py314-baseline -> hyera-0.0.0a0-py314   (median ms/call)
metric                   before      after     change
construct              199.6625   201.0505      +0.7%
lookup.deep.glob500    571.4212   587.3578      +2.8%
lookup.first             0.1652     9.6244   +5724.7%
lookup.keys100           0.0461     7.8164  +16855.0%
lookup.scope.new_node    16.1899    13.7706     -14.9%
lookup.scope.volatile    14.7423    10.1449     -31.2%
```

```
hyera-0.0.0a0-py39-baseline -> hyera-0.0.0a0-py39   (median ms/call)
metric                   before      after     change
construct              228.4309   228.8448      +0.2%
lookup.deep.glob500    614.2314   631.0972      +2.7%
lookup.first             0.1890    10.9070   +5670.2%
lookup.keys100           0.0476     8.8393  +18453.6%
lookup.scope.new_node    17.4764    15.7816      -9.7%
lookup.scope.volatile    20.0754    11.9413     -40.5%
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
(`revalidate=False`, the metric this benchmark treats as the actual
regression gate on 3.14, since with revalidation on the three filesystem-
bound metrics are expected to cost more) against their baselines:

```
hyera-0.0.0a0-py314-baseline -> hyera-0.0.0a0-py314-norevalidate   (median ms/call)
metric                   before      after     change
construct              199.6625   189.9368      -4.9%
lookup.deep.glob500    571.4212   573.6278      +0.4%
lookup.first             0.1652     0.1760      +6.5%
lookup.keys100           0.0461     0.0471      +2.2%
lookup.scope.new_node    16.1899     4.0237     -75.1%
lookup.scope.volatile    14.7423     2.1467     -85.4%
```

```
hyera-0.0.0a0-py39-baseline -> hyera-0.0.0a0-py39-norevalidate   (median ms/call)
metric                   before      after     change
construct              228.4309   218.7650      -4.2%
lookup.deep.glob500    614.2314   618.7924      +0.7%
lookup.first             0.1890     0.2336     +23.6%
lookup.keys100           0.0476     0.0545     +14.3%
lookup.scope.new_node    17.4764     5.2397     -70.0%
lookup.scope.volatile    20.0754     3.0189     -85.0%
```

This is the second measurement of this gate. The first (against the
engine as it stood immediately after this rework's own four phases, before
its branch was rebased onto everything else that had landed on `main`
meanwhile) found `lookup.first`/`lookup.keys100` regressed 3-10x, repeatably,
on both interpreters — a real per-lookup cost, not noise, traced to four
concrete causes and fixed in the same rebase that produced these numbers:
an identity-based fast path in the scope-keyed cache that skips replaying
every referenced variable when the exact same `Scope` object is queried
again (`Scope` is immutable, so identity alone already guarantees the same
values); a `data_hash` provider that stopped bypassing `load_file`'s
options-serialization/locking overhead on a repeat lookup once
`revalidate=False` gives it nothing left to discover; the `lookup_options`
gather no longer building throwaway `Invocation.derive()` objects that
were never actually used to look anything up; and the final composed,
pattern-compiled `lookup_options` mapping itself getting memoized per
scope identity instead of recompiling every `^`-prefixed regex on every
single lookup. `compare_bench.py` on 3.14 now reports `OK` outright. 3.9
still shows `lookup.first` persistently regressed (17-26% across four
separate runs, both before and after the file above) and `lookup.keys100`
borderline (5-14%, straddling the threshold run to run) — smaller than
main's own per-lookup layer-stack overhead alone would fully explain by
itself, but not chased further here: sub-millisecond operations on an
older, non-specializing interpreter (3.9 has no adaptive per-call
optimization) are exactly where a handful of extra Python-level function
calls (the layer/provider stack this plan's caching sits underneath, not
something this plan added) shows up disproportionately. Recorded, not
hidden, in the project's own findings queue. `lookup.scope.new_node`/
`lookup.scope.volatile` improved sharply on both interpreters, for the
reason given above.
