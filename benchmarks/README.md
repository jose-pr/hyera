# Benchmarks

Measures `hyera`'s per-call CPU cost on a generated 771-file Hiera 5 tree (no
network, no external fixtures): a cold start, first and repeated lookups, a
miss, a 40-level hierarchy, an `explain`, a deep merge across 500
glob-matched files, lookups against volatile or brand-new scopes, and one
run of the `hyera` command line. The command-line metric needs the `cli`
extra (`pip install "hyera[cli]"`).

## Run it

```
<py> benchmarks/run.py               # full tree, prints a report
<py> benchmarks/run.py --quick       # a much smaller tree, for a fast smoke run
<py> benchmarks/run.py --save        # also writes benchmarks/results/<name>.json
<py> benchmarks/run.py --save --name my-run --results-dir ./out
<py> benchmarks/run.py --revalidate off --save
```

`--revalidate on|off` (default `on`) is passed straight to every `Hiera(...)`
this run builds, and recorded as `"revalidate": true|false` in the saved
JSON. The default result name is `hyera-<version>-py<MAJOR><MINOR>`, with
`-norevalidate` appended under `--revalidate off`.

## Results and how to read them

`results/` holds saved results. A file named for a release
(`hyera-<version>-py314.json`, `hyera-<version>-py39.json`, each with a
`-norevalidate` twin) is the output of the `benchmark` job of the `Test`
workflow for that release: run it with `benchmark` set on a manual dispatch,
or push a throwaway `ci-bench-*` tag. The job never gates anything.

Compare two files recorded on the same machine and interpreter by their
`metrics.<name>.median_ms`:

```
python - before.json after.json <<'EOF'
import json, sys
a, b = (json.load(open(f))["metrics"] for f in sys.argv[1:3])
for name in sorted(a.keys() & b.keys()):
    x, y = a[name]["median_ms"], b[name]["median_ms"]
    print("{:24s} {:10.4f} {:10.4f} {:+7.1f}%".format(name, x, y, (y / x - 1) * 100))
EOF
```

A local run is a sanity check, never a release claim; a number quoted in a
changelog or a release note comes from a file in `results/`.

## Schema

One JSON object per `--save` run:

- `name`, `hyera_version`, `python` (`platform.python_version()`),
  `implementation`, `platform` (`platform.platform()`), `processor`
  (`platform.processor()` or `platform.machine()`), `timestamp` (UTC, ISO,
  second precision).
- `iterations`: the inner-call count used for each metric, plus `repeat`
  (the number of timed samples per metric; `cli.run` takes 5 samples of one
  call each, whatever `repeat` is).
- `tree`: `{"files": <n>, "reachable": <m>}` for the generated tree (771/523
  at full size, smaller under `--quick`).
- `revalidate`: whether every `Hiera(...)` this run built used
  `revalidate=True` (the default) or `revalidate=False`.
- `metrics.<name>.{min_ms, median_ms, max_ms}`: milliseconds per call,
  rounded to 6 places, across the samples of `iterations.<name>` calls each.

## Metrics

- `construct` — `Hiera(cfg, scope=S0)`: construction cost. The constructor
  reads the base config and the environment layer, and no data file.
- `lookup.cold` — construction plus the first `h.lookup("common::key5")`, on
  an instance nothing has warmed.
- `lookup.first` — `h.lookup("common::key5")` on one warm instance.
- `lookup.keys100` — 100 (quick: 20) distinct `mod<m>::key<k>` lookups on one
  warm instance.
- `lookup.miss` — `h.lookup("no::such::key", default_value=None)` on one warm
  instance: every level is searched and none holds the key.
- `lookup.levels40` — a key that exists only in the last of 40 hierarchy
  levels, on one warm instance; the other 39 name files that do not exist.
- `explain` — `h.explain("common::key5")` on one warm instance.
- `lookup.deep.glob500` — `h.lookup("settings", merge="deep")`: a deep merge
  over every one of the 500 glob-matched module files.
- `lookup.scope.volatile` — a lookup through a fresh view differing only in
  an unreferenced fact (`h.scoped(facts={"uptime_seconds": i})`); the point
  is the first call per scope, never warmed.
- `lookup.scope.new_node` — the same, differing in the referenced
  `clientcert` variable instead (`h.scoped(variables={"clientcert": ...})`),
  so a new node's own data provider must be rebuilt.
- `cli.run` — one `python -m hyera --hiera_config ... --facts ... common::key5`
  subprocess, interpreter start-up included; the median of 5 runs.
