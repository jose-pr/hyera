# pyera

A small, dependency-light Python implementation of [Puppet
Hiera](https://www.puppet.com/docs/puppet/7/hiera.html) hierarchical data
lookup — a `src/pyera` packaged library plus an optional `pyera` CLI, built on
the `duho`/`pathlib_next` stack.

It reads a Hiera 5 base config, walks a hierarchy of data sources for a given
context, and fully resolves a key's value — including `%{...}` interpolation
and the `hiera`/`lookup`/`scope`/`literal`/`alias` functions — with optional
array, hash, and deep-hash merging.

## Code layout

```
src/pyera/
├── __init__.py            # public re-exports (see src/pyera/AGENTS.md for the header)
├── core.py                 # Hiera, ScopedHiera: entry point and top-level lookup (lookup.rb + lookup_adapter.rb)
├── _hiera_config.py        # HieraLevel, base config reading, hierarchy building (hiera_config.rb)
├── _location_resolver.py   # hierarchy level path resolution (location_resolver.rb)
├── _interpolation.py       # Interpolation mixin: function and %{} variable resolution (interpolation.rb)
├── _merge_strategy.py      # Merge, make_merge: merge strategies (merge_strategy.rb)
├── _navigation.py          # sentinel + dotted context lookup (sub_lookup.rb)
├── _lookup_adapter.py      # lookup_options matching (lookup_adapter.rb)
├── _types.py               # Sensitive, convert_to (pops/types)
├── backends.py             # self-registering Backend registry, Puppet-only names + YAMLBackend/JSONBackend/HOCONBackend/SopsBackend
├── util.py                 # LookupDict (dotted-path lookup), sym_lookup
├── exceptions.py           # HieraError -> ConfigError, BackendError, HieraLookupError (InterpolationError, MergeError, KeyNotFoundError)
└── cli.py                  # duho-based `pyera` console script (Lookup command, main())
```

`pyera._*` modules are private engine internals mirroring Puppet's own file
split; import public names from `pyera` itself.

`pathlib_next.Path` is used throughout instead of stdlib `pathlib` (hierarchy
glob levels rely on its `Path.glob`).

## How it fits together

`Hiera(base_config, ...)` loads a Hiera 5 base config (path, file-like, or
dict), builds a `HieraLevel` per hierarchy entry (each pairing a `Backend`
with its source path template(s)), and pre-warms the context-free cache.
`Hiera.get(key, ...)` resolves the ordered candidate source paths for the
call's context, walks them looking up `key` in each parsed file, and — for a
merge strategy other than first-match — accumulates across every matching
level via a `Merge` accumulator before fully resolving interpolation and
hiera function calls in the result.

Backends register under one or more Hiera `data_hash` names (see
`src/pyera/AGENTS.md` for the table) and only need to implement
`read_file`/`load`; `YAMLBackend` and `JSONBackend` both parse into
`LookupDict` so `a.b.0.c`-style dotted lookups work uniformly.

The CLI (`src/pyera/cli.py`) is a thin `duho.Cli` wrapper around
`Hiera.get`, designed for unattended use: no interactive prompts,
deterministic output, and exit codes `0` (found) / `1` (key missing) / `2`
(usage or config error).

See **`src/pyera/AGENTS.md`** for the header-file-style public API — every
export with its signature, arguments, and gotchas.

## Hiera 5 spec coverage

Supported: `version: 5` validation, `defaults`, `hierarchy`/`default_hierarchy`,
`name`, `path`/`paths`/`glob`/`globs`/`mapped_paths`, `datadir`/`data_dir`,
`data_hash` backends (yaml/json/hocon/sops), all five interpolation methods
(`hiera`/`lookup`/`alias`/`scope`/`literal`) with dotted subkeys, merges
`first`/`unique`/`hash`/`deep` (with `knockout_prefix`/`sort_merged_arrays`/
`merge_hash_arrays`), and `lookup_options` (per-key/regex merge strategy +
`convert_to`).

Not implemented: `lookup_key`/`data_dig` provider backends, `uri`/`uris`
sources, `eyaml_lookup_key` (use the `sops` backend instead), and the legacy
`hiera3_backend` shim.

## Develop

- venvs: `.venv/3.14-nt-amd64` and `.venv/3.9-nt-amd64` (both interpreter
  bounds are supported and kept green).
- Tests: `<py> -m pytest -q` (pytest config in `pyproject.toml` puts `src/`
  on the path).
- Editable install: `<py> -m pip install -e ".[dev]"`. `dev` pulls in the
  `cli` and `hocon` extras, so nothing else needs adding by hand.
- Package: built with `hatchling`. The PyPI distribution, the import
  package and the console script are all `pyera`. `pyera[cli]` pulls in
  `duho` for the console script, `pyera[hocon]` pulls in `pyhocon` for
  `HOCONBackend`.
- **Version**: lives in exactly one place, `src/pyera/__init__.py`
  (`__version__`); `[tool.hatch.version]` reads it to build the package.
  Bump it in the same commit as the CHANGELOG entry for that release.
- **CI**: `test.yml` runs the full OS/Python matrix, a `black --check`, and
  a `floors` job that installs every declared dependency at its floor
  (`pyproject.toml`'s `>=` bound) on the oldest supported Python, so a floor
  that stops working is caught before a release does.
- **Releases**: tags are SemVer (`v1.0.0`, `v1.0.0-rc.1`); the PEP 440 form
  of the tag (`v1.0.0-rc.1` -> `1.0.0rc1`) must equal the built version, or
  `release.yml` stops before publishing anything. Pre-release tags create a
  GitHub pre-release and are never uploaded to PyPI; a hyphenless tag such
  as `v1.0.0rc1` counts as final.
- **Docs**: `<py> -m pip install -e ".[dev,docs]"` on the 3.14 venv (the docs
  tools need Python 3.10+), then `<py> -m mkdocs build --strict` from the
  repo root. `docs/index.md` is hand-written; its runnable examples and its
  extras table are checked by `tests/test_docs_examples.py`, so an API or
  extras change that breaks the page fails the suite instead of going
  unnoticed. `docs/api/` holds exactly one `:::` page per public module
  (`pyera`, `pyera.backends`, `pyera.cli`) — renaming or removing one updates
  both its page and `mkdocs.yml`'s nav. `docs/changelog.md` snippet-embeds
  `CHANGELOG.md`.
- **CI (docs)**: `test.yml`'s `docs` job builds the docs strictly on every
  run (the same build the release gates on, exercised before a release
  rather than by one); `docs.yml` deploys to GitHub Pages on a push to
  `main` touching `docs/`, `mkdocs.yml`, `src/` or `CHANGELOG.md`, and on
  `workflow_dispatch`; `release.yml` gates the GitHub release on a strict
  docs build (`docs-gate`, no deploy) and, for a final tag only, dispatches
  `docs.yml` to redeploy the docs for that release (`docs-deploy`) — kept
  off the publish chain so a docs problem never blocks a package that
  already passed its tests.

### Conformance goldens

`tests/conformance/` replays real Puppet's `puppet lookup` output against
this implementation, so a fidelity fix has an oracle-backed acceptance
test instead of a hand-written expectation.

- Layout: `_golden.py` (schema/digest/lint, no `pyera` import), `_ours.py`
  (the only module that calls into `pyera`'s API/CLI), `record.py`
  (recorder, dev-only), `test_conformance.py` / `test_conformance_cli.py`
  (replay). Cases live under `cases/<area>-<topic>/` with a hand-written
  `case.yaml` and a generated `golden.json`.
- Replay needs no Puppet: `<py> -m pytest -q -rs tests/conformance`.
- Recording needs Puppet 8.10's `puppet lookup` on `PATH` (`--runner
  local`) or reachable inside a WSL distribution (`--runner wsl` or
  `--runner wsl:<distro>`): `<py> tests/conformance/record.py --runner
  local|wsl [--jobs N] [CASE ...]`. Add `--check` to re-record in memory
  and diff against the committed goldens (exit 1 on drift, writes
  nothing), or `--list-markers` to list every divergence id and deviation
  without needing Puppet at all. CI only replays; it never records.
- A query marked `divergence: <finding-id>` in `case.yaml` is a strict
  `xfail` against Puppet's recorded result — fixing the underlying
  behavior turns the run red (`XPASS(strict)`) until the marker is
  removed. A `deviation:` is a different, permanent, asserted-as-passing
  outcome (never a strict xfail).
- **Never hand-edit `golden.json`.** It is only ever written by
  `record.py`, keyed by query id, and lint-checked (`test_case_is_current`)
  against a digest of everything that was asked of Puppet.

## License

MIT, for this project's own code. It is derived from
[phiera](https://github.com/Nike-Inc/phiera), which is Apache-2.0; the files
taken from it keep that license. See `NOTICE` and
`LICENSES/phiera-Apache-2.0.txt`.
