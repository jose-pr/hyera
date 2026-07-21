# hiera

A small, dependency-light Python implementation of [Puppet
Hiera](https://www.puppet.com/docs/puppet/7/hiera.html) hierarchical data
lookup — a `src/hiera` packaged library plus an optional `hiera` CLI, built on
the `duho`/`pathlib_next` stack.

It reads a Hiera 5 base config, walks a hierarchy of data sources for a given
context, and fully resolves a key's value — including `%{...}` interpolation
and the `hiera`/`lookup`/`scope`/`literal`/`alias` functions — with optional
array, hash, and deep-hash merging.

## Code layout

```
src/hiera/
├── __init__.py    # public re-exports (see src/hiera/AGENTS.md for the header)
├── phiera.py       # core engine: Hiera, ScopedHiera, HieraLevel, Merge, make_merge
├── backends.py     # Backend + YAMLBackend/JSONBackend/SopsYAMLBackend/HOCONBackend
├── util.py         # LookupDict (dotted-path lookup), sym_lookup
├── exceptions.py   # HieraError -> ConfigError, BackendError, InterpolationError
└── cli.py          # duho-based `hiera` console script (Lookup command, main())
```

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
`src/hiera/AGENTS.md` for the table) and only need to implement
`read_file`/`load`; `YAMLBackend` and `JSONBackend` both parse into
`LookupDict` so `a.b.0.c`-style dotted lookups work uniformly.

The CLI (`src/hiera/cli.py`) is a thin `duho.Cli` wrapper around
`Hiera.get`, designed for unattended use: no interactive prompts,
deterministic output, and exit codes `0` (found) / `1` (key missing) / `2`
(usage or config error).

See **`src/hiera/AGENTS.md`** for the header-file-style public API — every
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
- Editable install: `<py> -m pip install -e ".[dev]"`.
- Package: built with `hatchling`; `hiera[cli]` pulls in `duho` for the
  console script, `hiera[hocon]` pulls in `pyhocon` for `HOCONBackend`.

## License

MIT.
