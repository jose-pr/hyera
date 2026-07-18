# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `src/` package layout, `pyproject.toml`, and PyPI-ready metadata.
- Glob hierarchy levels (`glob:` / `globs:`), expanded via `pathlib_next` and
  resolved in sorted (deterministic) order.
- Command-line interface `hiera KEY` (built on `duho`), with
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
- Function calls resolving to a falsy value (`0`, `""`, `False`) no longer
  raise `InterpolationError` — only a genuinely absent value is rejected. A
  `%{hiera(...)}` whose key is missing now degrades to that rejection instead
  of propagating a `KeyError`.
- The bare-`%{var}` interpolation regex no longer also matches function-style
  `%{hiera(...)}` tokens, so an unresolved function leftover is not blanked.
- Invalid `--scope` values are reported through the logger and exit `2`, in
  line with the CLI's exit-code contract (previously a raw `SystemExit`).
- Deep hash merge now respects hiera precedence: a scalar provided by an
  earlier (higher-priority) hierarchy level is no longer clobbered by a later
  level. Previously the last level won for scalars, inverting precedence.
- Non-string scalar values (ints, floats, booleans) resolved by a
  `%{hiera(...)}`/`%{lookup(...)}` call embedded in a larger string are now
  stringified instead of raising; a single stand-alone call still preserves
  the resolved value's native type.

### Changed

- YAML is parsed with `SafeLoader` — hiera data is untrusted config and must
  not be able to construct arbitrary Python objects.
- `SopsYAMLBackend` hardened for unattended use: finite subprocess timeout,
  captured stderr surfaced in `BackendError`, and a clear error when the
  `sops` binary is missing.
- Backends register under multiple `data_hash` names (e.g. `yaml_data`/`yaml`,
  `json_data`/`json`).
- The per-context filesystem walk in `sources()` is cached, so a merge lookup
  across many keys no longer re-globs/re-stats the tree for each key.
