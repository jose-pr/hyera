# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The `default` merge strategy (first match, as in Puppet).
- The `reverse_deep` and `unconstrained_deep` merge strategies, which
  Puppet accepts for Hiera 3 data. `unconstrained_deep` also takes
  deep_merge's `keep_array_duplicates`, `overwrite_arrays`,
  `unpack_arrays`, `extend_existing_arrays`, `merge_nil_values` and
  `preserve_unmergeables`.

### Changed

- Interpolation in data values follows Puppet's rules: one left-to-right
  pass (`%{literal('%')}{x}` yields the literal `%{x}`, and a substituted
  value is never scanned again); whitespace inside `%{ }` is ignored; hash
  keys are interpolated; a variable whose value contains `%{...}` is
  interpolated; `%{lookup()}`, `%{hiera()}` and `%{scope()}` always produce
  a string; a missing key in `%{lookup()}`/`%{hiera()}`/`%{alias()}` gives
  `""`; `%{scope('x')}` treats an undefined `x` exactly like `%{x}` under
  the scope's `strict`; an unknown method or a malformed method call is an
  error. Data that used a stand-alone `%{lookup('k')}` to copy a list or
  hash must use `%{alias('k')}`.
- `Hiera.format(text)` interpolates exactly as data values are interpolated:
  all five methods are supported, whitespace inside `%{ }` is ignored, other
  braces are left alone, a missing variable follows the scope's `strict`
  instead of raising `KeyError`, and a non-string argument raises
  `TypeError`. A text that is exactly one `%{alias('k')}` returns `k`'s
  value.
- Each YAML-anchored node in a value is interpolated once, and the returned
  value shares it wherever the file reuses the anchor; mutating one
  occurrence in place changes every occurrence. A value a merge actually
  combines with another is always copied per position first, so the merge's
  in-place semantics never corrupt a position that happens to share a node
  with it.
- Non-string values interpolated into strings, hierarchy paths and
  `format()` render as Puppet renders them: floats in Ruby's form
  (`1.0e+20`), arrays as `["a", "b"]`, hashes as `{"k"=>"v"}`, and
  `Sensitive` as `Sensitive [value redacted]`.
- Merges now follow Puppet 8: deep merges put lower-priority array elements
  first and drop duplicates on both sides; merged hashes list lower-priority
  keys first; `unique` flattens nested arrays; duplicates are found with
  Ruby equality, so `1`, `1.0` and `true` stay distinct; `knockout_prefix`
  is a regular expression that removes array elements and blanks strings
  during each merge step and never removes hash keys; `merge_hash_arrays`
  merges lists of any length; `sort_merged_arrays` sorts only merged
  arrays.
- Invalid merge input raises `hyera.MergeError`: an unknown strategy, a
  merge hash without `strategy`, an unknown or mistyped option, a `hash`
  merge of a non-hash, a `unique` merge of a hash, and an array
  `sort_merged_arrays` cannot order.
- Hierarchy locations use Puppet's `%{...}` rules: whitespace, quoted
  segments, empty `%{}`, literal braces, and re-interpolated values.
- An undefined variable in a `path`, `paths`, `glob`, `globs` or
  `mapped_paths` location becomes `''` and logs a warning. Under
  `strict="off"` it logs nothing. The location is probed, not skipped.
- An undefined variable in `datadir` follows `strict` and raises under
  `"error"`.
- Method syntax (`%{lookup(...)}` etc.) in a location or `datadir` raises
  `ConfigError`.
- The `mapped_paths` collection is a scope reference (dotted, `::`). A Hash
  yields `["key", "value"]` pairs. `true`, `false`, numbers raise
  `ConfigError`, and the item is a local variable (`%{::x}` reads the top
  scope). Configs relying on the old Hash-values iteration must list the
  values.
- A `path`/`paths`/mapped location that is a directory raises
  `BackendError` ("Is a directory") instead of loading every file inside
  it, and a glob drops directory matches. List the files, or use a glob.
- `HieraLevel`'s fields are now `name, backend, datadir, location_key,
  locations`, and `.paths(base_path, scope)` returns a list.
- Glob hierarchy levels (`glob:`/`globs:`) now match through hyera's own
  Ruby `Dir.glob` port instead of `pathlib_next.Path.glob`: `{a,b}` brace
  alternation (nested, in written order, duplicates kept); `**` never
  follows a symlink or a Windows junction, and a trailing `**` is plain
  `*`; a dotfile matches only an explicit leading `.`, and `\` escapes a
  metacharacter; results sort in byte order and every wildcard is
  case-sensitive, on every OS; an unreadable directory is skipped; glob
  metacharacters in `datadir` apply to glob levels.
- `lookup_options` patterns now match as in Puppet: a search from the start
  of the key (`^app::` matches `app::ports`), Ruby regex syntax, and
  lower-priority levels' patterns tried first. An invalid pattern, a
  `lookup_options` value that is not a hash, and an entry that is neither a
  hash nor a string now raise `HieraLookupError` instead of being skipped.

### Removed

- `hyera.Merge` and `hyera.make_merge`: pass a strategy name or a
  `{"strategy": ...}` hash as `merge=`.
- `merge=list`/`set`/`dict`: use `"unique"` or `"hash"`.
- The `merge_deep=` argument of `get()`: pass `merge="deep"`.
- `sort_merged_arrays` with `unique`, `merge` as an alias of `strategy` in a
  merge hash, and lenient sorting of arrays that cannot be ordered.
- `Backend.datadir`: the entry's `datadir` is `HieraLevel.datadir`.

### Fixed

- A self- or mutually-referencing interpolation (`%{lookup('a')}` inside
  `a`, or a variable whose value refers to itself) raises
  `InterpolationError` "Recursive lookup detected in [a, b]" instead of
  Python's own `RecursionError`.
- Glob results no longer depend on the installed `pathlib_next` patch
  (dotfile matching, a trailing `**`), no longer loop or read outside a
  hierarchy's own tree through a symlink or Windows junction loop, and
  sort the same way on every OS.

## [0.0.0a0] - 2026-09-29

### Added

- Documentation site at <https://jose-pr.github.io/hyera/>: a getting-started
  page, an API reference generated from the docstrings of `hyera`,
  `hyera.backends` and `hyera.cli`, and this changelog. The package metadata
  links it as `Documentation`; the `docs` extra installs its build tools.
- `HYERA_MCP=stdio hyera` serves the command over MCP (stdio): one tool,
  `hyera`, taking the command-line fields as arguments and returning what
  the command prints. Any other `HYERA_MCP` value exits `2`.
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
- `convert_to` takes a Puppet type string (`Integer`, `Optional[Integer]`)
  or `[Type, *args]` (`[Integer, 16]`, `[String, '%x']`) and converts with
  Puppet's `new()`: Integer, Float, Numeric, String, Boolean, Array, Hash,
  Tuple, Struct, Optional, NotUndef and Sensitive (a redacting
  `hyera.Sensitive`). An invalid type or a failed conversion raises
  `hyera.HieraLookupError`.
- `HOCONBackend` (`hocon_data`/`hocon`) via the optional `pyhocon` dependency
  (`pip install hyera[hocon]`); registered automatically when importable.
- CLI merge surface: `--merge first|unique|hash|deep` (with `array`/`set`
  aliases) and `--knockout-prefix`.
- `src/` package layout, `pyproject.toml`, and PyPI-ready metadata. The
  distribution, import package and console script are all `hyera`.
- Glob hierarchy levels (`glob:` / `globs:`), expanded via `pathlib_next` and
  resolved in sorted (deterministic) order.
- Command-line interface `hyera KEY` (built on `duho`), with
  `--config`, repeatable `--scope key=value`, `--merge`, `--deep`,
  `--output raw|json|yaml`, and `--default`. Exit codes: `0` found (or
  `--default` printed), `1` missing, `2` any other error. Installed as the
  `hyera` console script and runnable via `python -m hyera`.
- Typed exception hierarchy: `HieraError` (base, `.path` names the file
  concerned) → `ConfigError` (invalid/missing `hiera.yaml`), `BackendError`
  (a data file could not be read or parsed, `.path` names it), and
  `HieraLookupError` (a failure while resolving a key) → `InterpolationError`,
  `MergeError`, and `KeyNotFoundError` (also a `KeyError`; `.get(...,
  throw=True)`'s miss). All exported from `hyera`.
- Test suite (pytest) covering lookup, interpolation, merge, glob, backends,
  and the CLI; green on Python 3.9 and 3.14.
- `Hiera(None, base_path=...)`: Puppet's built-in default configuration
  (`data/common.yaml` under `base_path`), used when there is no hiera.yaml
  to point at.
- `ConfigError.path` and `ConfigError.line` name the file (and, where
  known, the line) a configuration problem was found at.
- `sops_data` decrypts YAML, JSON, INI and dotenv files, choosing the
  format from the file extension the same way the `sops` CLI itself does
  (`.yaml`/`.yml`/`.json`/`.env`/`.ini`, case-sensitive; any other
  extension is a clear error naming the file instead of a raw or
  misleading failure). `DotenvBackend` parses sops's own dotenv output
  shape; it has no Puppet `data_hash` equivalent, so it is reachable only
  through `sops_data`. An INI file is decrypted through sops's own JSON
  view instead of a dedicated ini parser (see Security). `sops` is
  another name for `sops_data`; `sops_yaml`/`sops_json`/`sops_ini`/
  `sops_dotenv` force that format regardless of the file's own extension.
- `Scope`: Puppet's top scope for lookups. `variables` are node parameters;
  facts become top-scope variables without overriding them, and `$facts`;
  `server_facts` merge under both; `$environment` defaults to `production`;
  `$trusted` defaults to Puppet's local hash (certname from the `clientcert`
  variable or fact); `strict` is `off`, `warning` (default) or `error`.
- `LICENSES/puppet-Apache-2.0.txt` and `LICENSES/psych-MIT.txt`: credit
  [Puppet](https://github.com/puppetlabs/puppet) and
  [Psych](https://github.com/ruby/psych), the Apache-2.0 and MIT projects
  several modules port translated code from, alongside phiera; `NOTICE` lists
  each ported file.
- `load_facts(path)` reads a facts file with `puppet lookup --facts` rules
  (JSON for `.json`, YAML for `.yaml`/`.yml`, otherwise JSON then YAML; the
  result must be a mapping; YAML dates, times and `:symbols` are rejected;
  `hostname`/`domain`/`fqdn`/`clientcert` all or none). `facts_from_facter(timeout=30)`
  runs `facter -j` and returns its facts. Both raise `BackendError`.

### Changed

- `convert_to` raises `hyera.HieraLookupError` with Puppet's message when
  its type cannot be parsed or converted to, instead of returning the
  value unchanged; fix the data or catch the error.
- `hyera.Sensitive` prints as `Sensitive [value redacted]` and equals
  another `Sensitive` that wraps an equal value.
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
  sdist. The API reference `hyera/AGENTS.md` still ships in both.
- Building requires `hatchling>=1.27`, so the package metadata declares
  `License-Expression: MIT AND Apache-2.0` and lists `LICENSE`, `NOTICE` and
  `LICENSES/phiera-Apache-2.0.txt` as license files; older hatchling wrote
  only a free-text `License:` field.
- Releases: a `v*` tag must name the version being built, or the release
  stops before anything is published. Pre-release tags (`v1.0.0-rc.1`)
  create a GitHub pre-release and are not uploaded to PyPI, and re-running a
  release skips files already on PyPI.
- The engine is split into private modules; import public names from
  `hyera`. `hyera.core` now defines only `Hiera` and `ScopedHiera`, and
  `default_backends` lives in `hyera.backends`.
- `Hiera.get_key`, `load`, `load_file`, `buildcontext`, `can_resolve` and
  the `resolve*` methods are private (leading underscore); `sources()` no
  longer takes `_load`; the module globals `function`, `interpolate`,
  `rformat` and `LOGGER` are gone.
- `Merge.deep` and `Merge.typ` are removed; read `Merge.strategy`.
- `get(..., throw=True)` raises `KeyNotFoundError`, still a `KeyError`; a
  non-string key raises `TypeError`; an unknown merge strategy raises
  `MergeError` instead of `ValueError` — catch `MergeError` or `HieraError`.
- A data file that cannot be read or parsed raises `BackendError` (`.path`
  names the file) instead of `ConfigError`. A missing, unreadable, a
  directory, unparsable, non-mapping or malformed `hiera.yaml` raises
  `ConfigError` instead of `FileNotFoundError`, `BackendError`,
  `AttributeError`, `TypeError` or `ValueError`. Catch `ConfigError` for
  `hiera.yaml` and `BackendError` for data files.
- Parse errors are one line and name the file, line and column —
  `Unable to parse (<path>): <problem> at line L column C` for data files
  and `(<path>): <problem> at line L column C` for `hiera.yaml`, never a
  multi-line snippet or the underlying value.
- Backends register by subclassing `Backend` and declaring `NAMES` (a
  mapping of namespace — `function`/`v3`/`format`/`render` — to the names
  it answers to in that namespace), and are found by
  name: `Backend.find`/`.get`/`.new`/`.names`/`.for_path`. `load(bytes)`,
  `.read_file` and `YAMLBackend.load_ordered` are gone — use
  `.loads(text)`/`.load(path_or_file)`, which now decode data files as
  strict UTF-8 (as Puppet does) instead of relying on PyYAML's/`json`'s own
  detection. `SopsYAMLBackend` is renamed `SopsBackend` (`sops_data`).
  `default_backends()` always lists `HOCONBackend`; without `pyhocon` a
  `hocon_data` level now fails at construction (and at parse time) naming
  the `hyera[hocon]` extra, instead of `HOCONBackend` silently vanishing
  from the default list. A data file whose top-level value is not a Hash
  now follows Puppet instead of crashing: YAML warns and falls through
  (raising only under `--strict error`); JSON/HOCON/a third-party backend's
  non-Hash result is a `BackendError` naming the backend, the file and the
  value's Puppet type.
- YAML data and `hiera.yaml` now follow Puppet's Psych parser rules instead
  of PyYAML's own: octal/hex/sexagesimal/comma-separated numbers, Ruby's
  case-insensitive `yes`/`no`/`on`/`off` booleans and `null`, `:symbol`
  scalars (`RubySymbol`, in `hyera.backends`) and `!ruby/symbol`/`!ruby/sym`
  values, complex (list/hash) keys as hashable tuples, `<<` merge keys,
  multi-document files (only the first is read), and a leading UTF-8 BOM
  all resolve/parse the way Ruby does. An unrecognized YAML tag is no
  longer a parse error — it is tokenized/listed/dict-built like an
  untagged node of the same kind, matching Puppet. A date/timestamp-shaped
  scalar and `!!set` are refused (`BackendError`, no lenient mode), as
  Puppet's own safe-load refuses them. Symbol keys in `hiera.yaml` itself
  (however written) normalize to plain strings for every config version.
  libyaml (via PyYAML's `CSafeLoader`) is used when available; the one
  known gap in the pure-Python fallback is a tab after `:` in a plain
  scalar.
- JSON data now follows Ruby's `json` gem, not Python's `json` module
  directly: `/* ... */` and `// ...` comments are accepted; `NaN`/
  `Infinity`/`-Infinity` and an unescaped lone surrogate code point in a
  string are rejected (both are accepted by Python's decoder by default).
  HOCON durations (`10s`, `5 minutes`) and size strings (`10MB`) now stay
  literal text, matching real Ruby hocon (which has no such type) — they
  used to become `datetime.timedelta`, which crashed `-o yaml`. A HOCON
  file whose top-level value is not an object (e.g. `[1, 2]`) is now an
  error instead of returning that value directly.
- The `hocon` extra's `pyhocon` floor comment now also records the
  `get_period_expr` reason (present since 0.3.60, already covered by the
  existing `>=0.3.62` floor).
- A context key containing `.` no longer shadows the nested `%{a.b}` walk:
  Puppet has no such flat-key fallback (a variable name cannot contain
  `.`), so `%{a.b}` always means "navigate `.b` into the value of `a`" —
  quote the whole reference (`%{'a.b'}`) to reach a context entry literally
  named `"a.b"` instead. The CLI's `--scope` follows the same rule: a
  dotted `--scope` name (`--scope a.b=v`) now exits `2` rather than storing
  a variable nothing could ever read.
- A hiera.yaml without `version`, or with `version: 3`, raises `ConfigError`
  instead of being read as version 5 -- add `version: 5`.
- `version: 4` raises "hiera.yaml version 4 cannot be used in the global
  layer" instead of being read as version 5.
- Any other unsupported version raises "This runtime does not support
  hiera.yaml version N".
- A `version` that is not an Integer (`"5"`, `5.0`) raises, instead of
  being accepted or silently truncated.
- An empty or non-mapping hiera.yaml raises a `ConfigError` that names the
  Hiera version 3 fallback it does not yet support, instead of being read
  as an empty version 5 config.
- A relative config path, and a relative `base_path`, are made absolute at
  construction instead of resolving against the current working directory
  on every read.
- A hierarchy entry without `datadir` reads from `data/` next to
  hiera.yaml (or under `base_path`); it used to read from
  `/etc/puppetlabs/code/environments/%{environment}/hieradata`.
- A missing `defaults` becomes `{datadir: data, data_hash: yaml_data}`, and
  a missing `hierarchy` becomes `[{name: Common, path: common.yaml}]` (also
  when either is present but empty/`null`), matching Puppet's own built-in
  default configuration; both used to raise `ConfigError`.
- hiera.yaml is validated against Puppet's version 5 schema: a closed key
  set at the top level, in `defaults`, and in each hierarchy/
  `plan_hierarchy`/`default_hierarchy` entry; a required, non-empty,
  unique `name`; exactly one function key (`data_hash`/`lookup_key`/
  `data_dig`/`hiera3_backend`) per entry or in `defaults`; at most one
  location key (`path`/`paths`/`glob`/`globs`/`uri`/`uris`/`mapped_paths`);
  non-empty `paths`/`globs`/`uris`, and exactly three `mapped_paths`; a
  present key must be a non-empty string where one is expected (`~` is an
  error, not treated as absent); Puppet's option-name pattern for
  `options` keys, with `path`/`uri` reserved; and `hiera3_backend: json`/
  `yaml` (or `hocon`, when available) points at the `data_hash` name to
  use instead. Every violation raises `ConfigError` with Puppet's own
  message, and `.path`/`.line` when known. A hiera.yaml with a misspelled
  key, a missing or duplicate `name`, or two location keys on one entry
  used to load (silently misreading the config) and now raises; fix the
  config.
- `context=`/`**kwargs` are gone from `Hiera()`, `.get()`, `.has()`,
  `.sources()`, `.format()` and `.scoped()`; every one of them now binds a
  `hyera.Scope` instead (`Hiera(..., scope=Scope(...))`), and an unknown
  keyword raises `TypeError`. Migration: `Hiera(cfg, context={"role":
  "web"})` → `Hiera(cfg, scope=Scope(variables={"role": "web"}))`;
  `h.get(k, role="web")` → `h.scoped(variables={"role": "web"}).get(k)`.
- `ScopedHiera(hiera, scope)` — its `.get`/`.has`/`.sources`/`.format` use
  the bound scope, and `.scoped(...)` derives from it (nesting composes
  instead of each call restarting from the instance's own scope).
- A `False`/`0`/`""`/`[]`/`{}` variable or fact is kept, no longer dropped:
  `%{flag}` renders `false` (not empty) when `flag` is the boolean `false`,
  and a `virtual/%{is_virtual}.yaml` hierarchy level loads
  `virtual/false.yaml` instead of being skipped. `None`/`null` still
  renders as the empty string. `%{environment}` is always defined
  (`"production"` unless set) and `%{trusted}` defaults to Puppet's local
  hash, in both values and hierarchy paths.

- The non-Puppet `data_hash` names `yaml`, `json`, `hocon` and `yaml.enc`
  (strict Puppet only). Use `yaml_data`, `json_data` and `hocon_data`;
  `sops_data` (see Changed) is the one intentionally kept non-Puppet name.
- `hyera.LookupDict`, `hyera.sym_lookup` and the `hyera.util` module.
  Parsed data and merge results are now plain `dict`/`list` throughout;
  dotted-key navigation is a function over that data
  (`"a.b.0.c"`-style lookups still work exactly the same), not a container
  method. Migration: `LookupDict(...).lookup("a.b.0")` is now
  `Hiera.get("a.b.0")`. Ruby-symbol keys (`:key`) are normalized to `key`
  when data loads, so `sym_lookup` has no replacement — nothing needs one.
- The `data_dir` spelling, which Puppet does not accept — only `datadir`
  is a real Puppet key. A hierarchy or `defaults` entry using `data_dir`
  now raises `ConfigError` ("unrecognized key 'data_dir'"); rename it to
  `datadir`. `Backend` no longer falls back to reading `conf["data_dir"]`.

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
- The `hyera` console script and `python -m hyera` now print `pip install
  "hyera[cli]"` and exit 2 when the `cli` extra is missing, instead of
  crashing with a `ModuleNotFoundError` traceback.
- `ScopedHiera` can be copied, deep-copied and pickled; each previously
  raised `RecursionError`.
- The `HYERA_MCP` trigger env var name (and the served tool's name) no
  longer depends on `sys.argv[0]`. Running the CLI as `python -m
  hyera.cli` (trigger var `CLI_MCP`) or embedding `Lookup` in a
  differently-named script previously changed which environment variable
  launched the MCP server, silently breaking the documented `HYERA_MCP`
  contract.
- The missing-extra install hints (`pip install "hyera[cli]"` and `pip
  install "hyera[hocon]"`) are now double-quoted throughout; the old
  single-quoted form fails when pasted into `cmd.exe`, where single quotes
  are literal.
- A real `include file(...)`/`include url(...)` resolution (now the
  default -- see the HOCON `include` entry under Security) no longer
  crashes on Python 3.14+: pyhocon 0.3.63 itself still calls the
  deprecated `codecs.open()` and `Logger.warn()`, which raise
  `DeprecationWarning` there, turned into a fatal error by this project's
  own `filterwarnings = ["error"]`. Both calls are shimmed in hyera's
  already-private `pyhocon.config_parser` module copy; the shared
  `pyhocon` module, and every other caller of it, are unaffected.
- `Hiera(dict_config)` no longer modifies the caller's dict; the config is
  deep-copied at construction.
- A malformed hiera.yaml shape (a 2-element `mapped_paths`, a `hierarchy`
  that is a Hash or a list of strings, a non-Hash `defaults`, a non-Array
  `data_hash`, a `null` entry, and the like) now raises `ConfigError` with
  Puppet's own message, instead of a raw `ValueError`, `TypeError` or
  `AttributeError`. A string `paths`/`globs`/`uris` value is rejected
  outright instead of being silently split into one source per character.
- A hierarchy entry with `lookup_key`, `data_dig`, `hiera3_backend` or
  `v4_data_hash` no longer falls back to `defaults.data_hash` and reads
  its file as plain YAML; eyaml ciphertext used to be returned as the
  value. An unknown function name now raises Puppet's own "Unable to
  find '<kind>' function named '<name>'"; a known `lookup_key`/`data_dig`
  function raises `ConfigError` ("not supported yet") instead.
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
- Dotted lookup keys and `%{...}` context references now follow Puppet's own
  `split_key`/`sub_lookup` sub-key grammar exactly, instead of a naive
  `str.split(".")`: a segment may be single- or double-quoted (so
  `get("'a.b'.c")`-style keys reach a key that literally contains a dot),
  a negative or out-of-range list index (`lst.-1`) is not found rather
  than wrapping to the last item, an integer segment matches only an integer
  hash key (`h.0` finds `{0: x}`, never `{"0": x}`), and walking further into
  a `null` value (`n.x` where `n` is `~`) is not found rather than raising a
  raw `TypeError`. A genuine type mismatch (`s.x`/`lst.x`/`f.x` walking into
  a scalar, array or float) now raises `HieraLookupError` with Puppet's
  "Data Provider type mismatch" message instead of a raw `TypeError`/
  `ValueError`, and a malformed key (`a..b`, `a.`, `.a`, an unbalanced or
  empty quoted segment) raises `HieraLookupError` with Puppet's "Syntax
  error in key/string" text instead of silently returning the default. Both
  kinds of error are raised **even with a `default=` given**, and through
  `.has()` — only a genuine miss is silent, matching Puppet's own
  `lookup()`, which raises both even with `default_value` set.
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
- The CLI exits `2` with a one-line `Lookup of key 'K' failed: …` message
  for every failure other than a missing key; several failures used to
  print a traceback and exit `1`, the missing-key code (a plain `KeyError`
  from a custom `.get()` override, for example, could be mistaken for a
  miss). `-v` or `DUHO_TRACEBACK=1` adds the traceback.

### Security

- An INI file decrypted by `sops_data` is now parsed from sops's own
  `--output-type=json` view, never ini text: sops's own INI writer emits
  a value containing `"""` plus a newline ambiguously, so a decrypted
  value could be read back as a different key or as an injected section
  (reproduced against real sops 3.13.3: a `db.password` value replaced by
  a later, attacker-supplied one). `sops_ini`/extension-inferred `ini`
  both still tell sops to *read* the file as ini; only the output/parse
  side changed.
- Three more `_yaml_loader` messages a decrypted YAML file can shape
  itself into (`invalid value for Float()`/`Integer()`, and `Tried to
  load unspecified class:` for a `!ruby/object`/`!ruby/hash` tag) no
  longer quote the offending scalar or class name on the `sops_data`
  decrypt path; `yaml_data` (no sops involved) is unchanged. A handful of
  fixed names hyera itself raises for a known YAML shape (`Time`, `Date`,
  an unnamed `!ruby/object`, `!!set`) still show, since none of them ever
  echo text from the document.
- A `sops` decrypt timeout no longer leaves the underlying
  `subprocess.TimeoutExpired` (and its captured partial stdout) reachable
  via the raised `BackendError`'s `__context__`; only `__cause__` was
  addressed by the previous `from None` fix.
- A `UnicodeDecodeError` from a non-UTF-8 decrypted file now reports only
  the byte offset, not the offending byte value or the stock codec
  message's surrounding text.
- Decrypted `sops` plaintext no longer appears in error messages or logs
  when a decrypted file fails to parse.
- The data file passed to `sops` is always an absolute path after a
  literal `--`, so a name starting with `-` can never become a `sops`
  option; the `sops` found on `PATH` is executed by its full resolved
  path, and a `sops.bat`/`sops.cmd` shim is refused.
- HOCON `include` directives resolve exactly as Puppet's own `hocon_data`
  does by default (hyera never does *less* than Puppet by default,
  only as an explicit opt-in): a plain `include "file"` contributes
  nothing, as in Puppet; `include file(...)` really reads the file
  (relative to the process working directory, or absolute), as Puppet's
  `hocon_data` does; a directive in value position (including inside a
  `[...]` array) is kept as literal text, as Puppet keeps it; and
  `url(...)`, `classpath(...)`, `required(...)`, `package(...)`, a
  case-mismatched keyword, or a bare `include` with nothing valid after
  it all raise `BackendError`, matching Puppet's own parse/method errors
  for those forms (Ruby hocon implements none of them). The pre-fidelity
  refusal -- every form other than a plain quoted include raises,
  `include file(...)` included -- is kept as an opt-in:
  `HOCONBackend(hocon_includes=False)`, or a `hocon_includes: false` key
  on the hierarchy entry/`defaults` (hyera's own extension, not Puppet
  vocabulary). One accepted divergence: Puppet's `include file("*.conf")`
  never globs; pyhocon's own resolution does and includes every match.
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
  also treated as value position (kept as literal text by default, raised
  under the `hocon_includes=False` opt-in) rather than being blanked into
  a shorter array. As a fail-closed backstop, pyhocon's own
  include-resolving methods raise for the duration of a HOCON parse for
  every form the active mode does not intend to resolve for real, so even
  an undiscovered scanner gap cannot read a file or reach the network;
  they behave normally for any other use of pyhocon in the same process.
  This backstop now also wraps hyera's own private `pyhocon.config_parser`
  module copy (added by `json_hocon_loaders` for Ruby-hocon-compatible
  duration parsing) -- previously it wrapped only the shared `pyhocon`
  module, which `HOCONBackend` never actually parses through, leaving the
  backstop installed but inert for every real `HOCONBackend` call; found
  and fixed while widening the default's own capability, which makes the
  backstop's guarantee matter more, not less.
