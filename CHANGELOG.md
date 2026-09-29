# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `hiera.yaml` is read as UTF-8 bytes (a BOM or UTF-16 is detected) instead
  of the locale encoding, and closed right after reading. On Windows a
  non-ASCII config was silently misread (a `datadir` with an accent found
  nothing), a UTF-8 BOM config failed to parse, and the previously-open
  handle stopped the file from being replaced and the instance from being
  pickled or deep-copied. `Hiera.base_config` now keeps the exact path it
  was given.
- A `glob`/`globs` hierarchy level whose directory does not exist now
  contributes no files instead of raising `FileNotFoundError` from
  `Hiera()` or `.get()`. The documented example config crashed at
  construction when `data/modules` was absent, and a per-node glob
  directory crashed lookups for any node without one.
- A HOCON file that is not valid UTF-8 now raises `BackendError` instead of
  a raw `UnicodeDecodeError`.
- An installed but broken `pyhocon` (an import-time exception other than
  `ImportError`, e.g. against a too-new stdlib) no longer breaks every
  `Hiera()`; `HOCONBackend` is simply left unregistered, and constructing
  one directly raises `BackendError`.
- CLI `--output yaml` now renders hashes and redacted `Sensitive` values
  instead of crashing with `RepresenterError` and exit 1.
- An explicit `--merge first` on the CLI now overrides a `lookup_options`
  merge, as `puppet lookup --merge first` does (omitting `--merge` still
  lets `lookup_options` decide).
- The `pyera` console script and `python -m pyera` now print `pip install
  "pyera[cli]"` and exit 2 when the `cli` extra is missing, instead of
  crashing with a `ModuleNotFoundError` traceback.
- `ScopedHiera` can be copied, deep-copied and pickled; each previously
  raised `RecursionError`.
- The `PYERA_MCP` trigger env var name (and the served tool's name) no
  longer depends on `sys.argv[0]`. Running the CLI as `python -m
  pyera.cli` (trigger var `CLI_MCP`) or embedding `Lookup` in a
  differently-named script previously changed which environment variable
  launched the MCP server, silently breaking the documented `PYERA_MCP`
  contract.
- The missing-extra install hints (`pip install "pyera[cli]"` and `pip
  install "pyera[hocon]"`) are now double-quoted throughout; the old
  single-quoted form fails when pasted into `cmd.exe`, where single quotes
  are literal.

### Security

- Decrypted `sops` plaintext no longer appears in error messages or logs
  when a decrypted file fails to parse.
- The data file passed to `sops` is always an absolute path after a
  literal `--`, so a name starting with `-` can never become a `sops`
  option; the `sops` found on `PATH` is executed by its full resolved
  path, and a `sops.bat`/`sops.cmd` shim is refused.
- HOCON data files no longer read files or fetch URLs through `include`.
  A plain `include "file"` contributes nothing, as in Puppet;
  `include file(...)`, `url(...)`, `classpath(...)`, `required(...)` and
  other forms raise `BackendError`. Previously pyhocon resolved plain and
  `file()` includes against the process working directory and fetched
  `http(s)` URLs named in a data file.
- `sops` is refused when it resolves to a relative path (e.g. from the
  current directory or a relative `PATH` entry), closing a gap where
  Python 3.9's `shutil.which` could still return such a path even with the
  Windows implicit-current-directory opt-out set.
- Three sops-decrypted YAML parse errors (an undefined alias, an unknown
  tag, a duplicate anchor) no longer quote the offending value verbatim in
  the raised error, the log, or the CLI's output. A sops timeout also no
  longer chains the underlying `TimeoutExpired` (which carries any partial
  decrypted stdout).
- Closed several remaining gaps in the HOCON `include` text scanner: a
  caselessly-matched keyword using a non-ASCII look-alike character (e.g. a
  dotless "ı"), a triple-quoted string ending in extra quote characters, a
  backslash-escaped `"`/`#`/`${` in unquoted text, and a `//` that is part
  of ordinary unquoted text (as in a URL-shaped value) rather than a
  comment could each let a real `include file(...)`/`url(...)` reach
  pyhocon's own include machinery. `include` inside a `[...]` array is now
  also treated as value position (raises) rather than being blanked into a
  shorter array. As a fail-closed backstop, pyhocon's own include-resolving
  methods now also raise for the duration of a HOCON parse, so even an
  undiscovered scanner gap cannot read a file or reach the network; they
  behave normally for any other use of pyhocon in the same process.

### Added

- `PYERA_MCP=stdio pyera` serves the command over MCP (stdio): one tool,
  `pyera`, taking the command-line fields as arguments and returning what
  the command prints. Any other `PYERA_MCP` value exits `2`.
- `NOTICE` and `LICENSES/phiera-Apache-2.0.txt`: credits
  [phiera](https://github.com/Nike-Inc/phiera), the Apache-2.0 project this
  library is derived from. The package license is now `MIT AND Apache-2.0`.
- Hiera 5 spec compliance: `version: 5` validation (a non-5 version is
  rejected); the `unique`/`hash`/`deep` merge strategies (plus `first`),
  selectable by name, legacy type, or an options hash
  (`knockout_prefix`, `sort_merged_arrays`, `merge_hash_arrays`); the reserved
  `lookup_options` data key (per-key and regex-pattern merge strategy +
  `convert_to`, with an explicit `merge=` argument overriding it);
  `mapped_paths` source levels; and `default_hierarchy` fallback.
- `convert_to` casts: `Integer`, `Float`, `String`, `Boolean`, `Array`, and
  `Sensitive` (a redacting `pyera.Sensitive` wrapper). Unknown/failed casts
  leave the value unchanged (never raise — unattended-safe).
- `HOCONBackend` (`hocon_data`/`hocon`) via the optional `pyhocon` dependency
  (`pip install pyera[hocon]`); registered automatically when importable.
- CLI merge surface: `--merge first|unique|hash|deep` (with `array`/`set`
  aliases) and `--knockout-prefix`.
- `src/` package layout, `pyproject.toml`, and PyPI-ready metadata. The
  distribution, import package and console script are all `pyera`.
- Glob hierarchy levels (`glob:` / `globs:`), expanded via `pathlib_next` and
  resolved in sorted (deterministic) order.
- Command-line interface `pyera KEY` (built on `duho`), with
  `--config`, repeatable `--scope key=value`, `--merge`, `--deep`,
  `--output raw|json|yaml`, and `--default`. Exit codes: `0` found, `1`
  missing, `2` usage/config error. Installed as the `pyera` console script and
  runnable via `python -m pyera`.
- Typed exception hierarchy: `HieraError` → `ConfigError`, `BackendError`,
  `InterpolationError`.
- Test suite (pytest) covering lookup, interpolation, merge, glob, backends,
  and the CLI; green on Python 3.9 and 3.14.

### Fixed

- Per-call context now reaches hierarchy path resolution. `Hiera.get()` built
  its context from `context=` plus `**kwargs` but resolved sources from the
  raw `context` argument, so `get(key, environment="production")` silently
  skipped the `environments/%{environment}.yaml` level and fell through to
  `common.yaml`. `has()` funnels all context through `**kwargs` and so was
  affected wholesale; it now takes an explicit `context=`.
- `ScopedHiera.has()` no longer lets its bound context override per-call
  arguments. It layered the bound context *over* `**kwargs`, the inverse of
  `ScopedHiera.get()` and of the documented contract, so `.has()` and `.get()`
  could disagree about the same lookup.
- Dotted context references (`%{trusted.certname}`) resolve as nested lookups
  instead of raising. They became `str.format` attribute access, which raises
  `AttributeError` on the dict contexts hiera actually uses — and
  `HieraLevel.paths()` caught only `KeyError`, so the error escaped and
  crashed `get()`. The documented example config, which leads with
  `nodes/%{trusted.certname}.yaml`, failed at construction time. Paths,
  `data_dir`, `mapped_paths` templates, values, `format()` and
  `%{scope('a.b')}` now share one nested-lookup rule; numeric segments index
  lists, a flat context key containing dots still wins, and an unresolvable
  reference skips the level or yields `""` rather than raising.
- `sort_merged_arrays` now applies to `deep` merges, where Puppet defines it.
  It was honoured only on the `unique` branch and silently swallowed on
  `deep`, which both the README and the API header advertised as supported.
  Sorting runs after knockout and reaches lists nested anywhere in the
  result; a list with no total order is left in merge order.
- A `lookup_options` key is treated as a regular expression only when it
  starts with `^`, per Hiera 5. Any key containing a regex metacharacter was
  compiled as a pattern, so an entry for `db.port` also matched `dbxport`.
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
- The merged `lookup_options` mapping is cached per resolved context too. It
  hash-merges every file in the hierarchy and a default-merge `get()` consults
  it for every key, so a lookup of N keys previously redid that walk N times.
- Test and release GitHub Actions workflows (the repo previously had no CI).
  `test.yml` runs on demand or from a `ci-*` tag across 3.9–3.14; `release.yml`
  gates a `v*` tag on the suite before building and publishing.
- `black` is the formatting standard, pinned to the `py39` floor; the `dev`
  extra now installs it along with `pyhocon`, which was missing and left the
  HOCON backend tests skipping in a dev install.
- Unshared `*.local.*` files are excluded from the sdist and the wheel, and
  ignored by git — previously any such file other than `*.local.md` was
  packaged into both artifacts.
- Dependency ranges pinned to a minor series: `pathlib_next>=0.9.0,<0.10`,
  and `duho>=0.5.0,<0.6` in both the `cli` and `dev` extras. Both were
  previously unversioned, so a resolver could pick any release ever
  published. The upper bounds stop the next pre-1.0 minor, where these
  projects are free to break their API.
- `duho` pin moved to `>=0.6.0,<0.7` in both the `cli` and `dev` extras. No
  source change was required: hiera's own duho surface (`Cli`, `LoggingArgs`,
  `Arg`/`NS`/`Append`/`Choice`, `main()`) is untouched by every documented
  0.6.0 API change.
- `pyyaml` is now required as `>=6.0,<7` and the `hocon` extra as
  `pyhocon>=0.3.62,<0.4`; both were unversioned. pyhocon 0.3.0-0.3.28 pin
  pyparsing 2.0.3-2.1.1, which fails to import on Python 3.10 and later;
  0.3.29-0.3.61 call a pyparsing API that is deprecated on current
  pyparsing releases.
- Files named `CLAUDE*` or `.claude` are excluded from the sdist and the
  wheel, and the repository's contributor `AGENTS.md` is no longer in the
  sdist. The API reference `pyera/AGENTS.md` still ships in both.
- Building requires `hatchling>=1.27`, so the package metadata declares
  `License-Expression: MIT AND Apache-2.0` and lists `LICENSE`, `NOTICE` and
  `LICENSES/phiera-Apache-2.0.txt` as license files; older hatchling wrote
  only a free-text `License:` field.
- Releases: a `v*` tag must name the version being built, or the release
  stops before anything is published. Pre-release tags (`v1.0.0-rc.1`)
  create a GitHub pre-release and are not uploaded to PyPI, and re-running a
  release skips files already on PyPI.
- The engine is split into private modules; import public names from
  `pyera`. `pyera.core` now defines only `Hiera` and `ScopedHiera`, and
  `default_backends` lives in `pyera.backends`.
- `Hiera.get_key`, `load`, `load_file`, `buildcontext`, `can_resolve` and
  the `resolve*` methods are private (leading underscore); `sources()` no
  longer takes `_load`; the module globals `function`, `interpolate`,
  `rformat` and `LOGGER` are gone.
