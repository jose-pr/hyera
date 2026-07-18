# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `src/` package layout, `pyproject.toml`, and PyPI-ready metadata.
- Glob hierarchy levels (`glob:` / `globs:`), expanded via `pathlib_next` and
  resolved in sorted (deterministic) order.
- Command-line interface `hiera lookup KEY` (built on `duho`), with
  `--config`, repeatable `--scope key=value`, `--merge`, `--deep`,
  `--output raw|json|yaml`, and `--default`. Exit codes: `0` found, `1`
  missing, `2` usage/config error. Installed as the `hiera` console script and
  runnable via `python -m hiera`.
- Typed exception hierarchy: `HieraError` → `ConfigError`, `BackendError`,
  `InterpolationError`.
- Test suite (pytest) covering lookup, interpolation, merge, glob, backends,
  and the CLI; green on Python 3.9 and 3.14.

### Fixed

- Interpolation no longer treats resolved values as `re.sub` replacement
  templates — backslashes and `\g<...>` sequences in data now pass through
  literally instead of raising or being mangled.
- `Hiera.format()` now formats with the context mapping (`format_map`) instead
  of passing the dict as a single positional argument.
- Mutable default arguments (`context={}`) replaced with `None` sentinels,
  fixing cross-call context contamination in `scoped()` and others.
- `Backend.__init__` reads the data dir from either `datadir` or `data_dir`
  instead of raising `KeyError` on the Hiera-5 spelling.
- Unknown/missing `data_hash` backends now raise a clear `ConfigError` naming
  the known backends, rather than an opaque `KeyError`.
- `LookupDict` is no longer (unsafely) hashable.

### Changed

- YAML is parsed with `SafeLoader` — hiera data is untrusted config and must
  not be able to construct arbitrary Python objects.
- `SopsYAMLBackend` hardened for unattended use: finite subprocess timeout,
  captured stderr surfaced in `BackendError`, and a clear error when the
  `sops` binary is missing.
- Backends register under multiple `data_hash` names (e.g. `yaml_data`/`yaml`,
  `json_data`/`json`).
