# `pyera` — public API header

Header-file-style reference for the `pyera` package: every `__all__` export
with its signature, arguments, contract, and gotchas, so this module can be
consumed without reading its source. Kept current with the public API. For
the project overview, see the shipped `README.md`, or <https://github.com/jose-pr/pyera>.

Install and import as `pyera` (`pip install pyera`, extras
`[cli]`/`[hocon]`); the command is `pyera`.

## Engine (`core.py`)

- **`Hiera(base_config, backends=None, base_path=None, context=None, **kwargs)`**
  — the main entry point. `base_config`: a file path, a file-like object, or
  a pre-parsed `dict` (a Hiera 5 base config: `version`, `defaults`,
  `hierarchy`, `default_hierarchy`). A path is read once, as bytes (UTF-8,
  UTF-8 BOM, or UTF-16 with BOM), and `.base_config` keeps the path unchanged
  (a `str` stays a `str`, a `Path` stays that `Path`). A file-like object is
  read as given; `Hiera` never closes it and `.base_config` keeps that same
  object. `backends`: list of `Backend` classes, defaults to
  `default_backends()`. `base_path`: root that relative `data_dir`/paths
  resolve against (defaults to the config file's directory, or
  `os.getcwd()` for a dict/file-like config). `context`/`kwargs`: default
  format variables merged into every call's context. Raises `ConfigError` on
  any invalid/missing configuration (bad `version`, missing `hierarchy`,
  unknown `data_hash`, unparsable base file).
  - **`.get(key, default=None, merge=None, merge_deep=False, throw=False, context=None, **kwargs)`**
    — resolve `key`. `merge`: a strategy name (`"first"`/`"unique"`/
    `"hash"`/`"deep"`), a legacy type (`list`/`set`/`dict`), or a dict
    `{"strategy": "deep", "knockout_prefix": ..., "sort_merged_arrays": ...,
    "merge_hash_arrays": ...}`. Omitted → the data's `lookup_options` key
    decides, else first-match-wins. `merge_deep`: legacy flag, promotes a
    `dict`/`"hash"` merge to `"deep"`. `throw=True` raises `KeyError` instead
    of returning `default` on a miss. Falls back to `default_hierarchy` when
    the main hierarchy misses. `context`/`kwargs` layer over the instance's
    default context for this call only.
  - **`.has(key, context=None, **kwargs) -> bool`** — `True` iff
    `.get(key, throw=True, context=context, **kwargs)` would not raise
    `KeyError`. `context`/`kwargs` layer over the instance context exactly
    as in `.get`, and reach hierarchy path resolution as well as
    interpolation.
  - **`.scoped(context=None, **kwargs) -> ScopedHiera`** — bind context
    variables once for reuse.
  - **`.sources(context=None, **kwargs) -> list`** — resolve+load the
    ordered candidate source paths for a context (cached per resolved
    context; a fresh `Hiera` instance if the on-disk tree may have changed).
  - **`.format(text, context=None, **kwargs) -> str`** — resolve `%{var}`
    references in an arbitrary string against the instance context.
  - Gotcha: a single `Hiera` instance caches parsed file contents
    (`.cache`), resolved source-path lists (`._source_cache`) and the merged
    `lookup_options` mapping (`._lookup_options_cache`), all per resolved
    context — it does not notice on-disk changes after first load for a
    given context.
  - Gotcha: a path-configured `Hiera` holds no open file, so the config file
    can be replaced or removed on disk while the instance lives (it keeps
    what it read at construction). It survives `copy.deepcopy` and `pickle`
    (a spawn-start process pool can receive one; a relative config path
    stays relative to the receiving process's working directory), which
    copies the parsed-data cache too, sops-decrypted values included.
    Concurrent `.get()` calls on one instance from multiple threads are safe
    on GIL builds, where they only mutate that instance's own caches
    (untested on free-threaded builds).
- **`ScopedHiera(hiera, context=None)`** — wraps a `Hiera` with a bound
  context; `.get(key, ..., context=None, **kwargs)` and
  `.has(key, context=None, **kwargs)` merge the bound context *under*
  per-call overrides, so a per-call value always wins. Unknown attributes
  proxy to the wrapped `Hiera`.
- **`make_merge(spec) -> Merge | None`** — normalize a `merge=` spec (name,
  legacy type, or options dict) into a `Merge` accumulator, or `None` for
  first-match. Raises `ValueError` on an unrecognized strategy/type.
- **`Merge(strategy, knockout_prefix=None, sort_merged_arrays=False, merge_hash_arrays=False)`**
  — accumulates matches across the hierarchy. `"unique"`: flatten
  scalars+arrays, dedupe, first-seen order (+ optional sort). `"hash"`:
  shallow merge, higher-priority (earlier) level wins per key. `"deep"`:
  recursive merge — hashes recurse, lists concatenate+dedupe (or merge
  element-wise by index with `merge_hash_arrays` when both sides are
  equal-length lists of dicts), a scalar already set by a higher-priority
  level is never clobbered; `knockout_prefix` marks keys/values to remove
  post-merge. `sort_merged_arrays` applies to `"unique"` and `"deep"`; on
  `"deep"` it runs after knockout and sorts lists nested anywhere in the
  result, leaving any list with no total order (mixed types) in merge order.
- **`HieraLevel`** (`NamedTuple`: `backend`, `sources`, `glob`, `mapped`) —
  one hierarchy entry. `.new(conf, backend)` builds one from a hierarchy
  dict (`path`/`paths`/`glob`/`globs`/`mapped_paths`). `.paths(base_path,
  context)` yields candidate source paths for a context; a source
  referencing an absent context var is silently skipped. A glob whose
  directory does not exist yields nothing (matches Puppet), instead of
  raising from the underlying filesystem glob.
- **`Sensitive(value)`** — redacting wrapper produced by `convert_to:
  Sensitive`. `str()`/`repr()` show `Sensitive(<redacted>)`; `.unwrap()`
  returns the real value.
- **`default_backends() -> list[type[Backend]]`** — `[YAMLBackend,
  SopsYAMLBackend, JSONBackend]`, plus `HOCONBackend` if `pyhocon` is
  importable.

## Backends (`backends.py`)

- **`Backend(conf=None)`** — base class; subclasses implement `.load(data)`
  (and optionally override `.read_file(path) -> bytes`). `NAMES: tuple[str,
  ...]` — the `data_hash` value(s) it answers to. `.datadir` reads `conf["datadir"]`
  or `conf["data_dir"]` (Hiera-5 spelling), default `""`.
- **`YAMLBackend`** — `NAMES = ("yaml_data", "yaml")`. Parses with
  `yaml.SafeLoader` (data is untrusted config) into `LookupDict` mappings.
  `.load_ordered(stream, Loader=yaml.SafeLoader, object_pairs_hook=LookupDict)`
  (staticmethod) does the actual parse; raises `BackendError` on a YAML
  error.
- **`SopsYAMLBackend(YAMLBackend)`** — `NAMES = ("yaml.enc", "sops")`.
  Shells out to the `sops` CLI to decrypt before YAML-parsing. Hardened for
  unattended use: `SOPS_TIMEOUT` (module-level, default `30` seconds) bounds
  the subprocess; a missing `sops` binary or non-zero exit raises
  `BackendError` with captured stderr rather than hanging or raising a raw
  `OSError`. The resolved `sops` is invoked as
  `[<abs sops path>, "--input-type=<fmt>", "--output-type=<fmt>", "-d",
  "--", <abs data path>]` — the data path is always absolute and after a
  literal `--`, so a path or scope value starting with `-` can never be
  parsed as a `sops` option; a `sops.bat`/`sops.cmd` shim is refused
  (`cmd.exe` re-parses a batch file's own argument line). A decrypted file
  that fails to parse raises `BackendError` with only a short reason and a
  1-based line/column — never the decrypted plaintext, and with no
  exception chain (`__cause__`/`__context__` are both `None`) to carry it.
- **`JSONBackend`** — `NAMES = ("json_data", "json")`. `json.loads` with
  `object_pairs_hook=LookupDict`; raises `BackendError` on decode failure.
- **`HOCONBackend`** — `NAMES = ("hocon_data", "hocon")`. Requires the
  optional `pyhocon` dependency (`pip install pyera[hocon]`); raises
  `BackendError` naming the extra if it's not installed, or if an installed
  `pyhocon` fails to import for any other reason (e.g. against a too-new
  stdlib). Invalid UTF-8 raises `BackendError` rather than a raw
  `UnicodeDecodeError`. `include` directives are sanitized before pyhocon
  ever parses the text, so pyhocon's own include machinery (file reads
  relative to the process cwd, `http(s)`/`file` URL fetches) never runs: a
  plain `include "..."` contributes nothing, matching Puppet; every other
  form — `file(...)`, `url(...)`, `classpath(...)`, `required(...)`,
  `package(...)`, any other `name(...)`, a case-mismatched keyword
  (`INCLUDE ...`), a bare `include` with nothing valid after it, or an
  `include` directive in value position — raises `BackendError` instead
  (two of these, `file()` and value position, differ from what Puppet
  itself does; see the gotcha below). `${VAR}` substitutions fall back to
  environment variables, as in Puppet.
- **`has_hocon() -> bool`** — `True` iff `pyhocon` imports without error; any
  import-time exception (not just `ImportError`) is caught, logged at
  debug, and returns `False` — an installed but broken `pyhocon` leaves
  `HOCONBackend` unregistered instead of breaking every `Hiera()`.
- Env: `sops` runs with the process environment, so its own `SOPS_*` and
  key-source variables apply. `SOPS_TIMEOUT` is a module attribute, not an
  env var — set it directly (`pyera.backends.SOPS_TIMEOUT = 60`) to change
  the sops timeout.

## Utilities (`util.py`)

- **`LookupDict(dict)`** — supports dotted-path lookup. `.lookup(key)`
  resolves `"a.b.0.c"`, indexing into nested dicts and lists (numeric
  segments index a list); raises `KeyError`/`IndexError` on a miss.
  Intentionally **not hashable** (mutable mapping) — never use one as a
  dict key.
- **`sym_lookup(obj, key, default=None)`** — dict lookup that also tries a
  Ruby-symbol-style `":key"` spelling.

## Exceptions (`exceptions.py`)

`HieraError` (base) → **`ConfigError`** (invalid/missing base config),
**`BackendError`** (a backend failed to load/decode a file), and
**`InterpolationError`** (a `%{...}` interpolation or function call could
not be resolved). All are also re-exported from `pyera.__init__` (except
`BackendError`, which lives on `pyera.backends`/`pyera.BackendError` — both
paths work since `__init__` re-exports it too).

## CLI (`cli.py`)

- **`main(argv=None) -> int`** — the `pyera` console-script entry point;
  builds and dispatches the `Lookup` duho command (`duho.main`), which sets
  up `-v/-q/--loglevel` logging and returns the process exit code. When the
  `cli` extra (`duho`) is not installed, `main` always exists but prints
  `pyera: the command-line interface needs the cli extra: pip install
  'pyera[cli]'` to stderr and returns 2, instead of raising
  `ModuleNotFoundError`; `Lookup` itself is not defined in that case.
- **`Lookup`** — the `duho.Cli` command class (only defined when `duho` is
  installed). Fields: `key` (positional),
  `config` (`--config/-c`, default `"hiera.yaml"`), `scope` (`--scope/-s`,
  repeatable `key=value`), `merge` (`--merge`, choice of
  `first|unique|hash|deep|array|set`, default `None`; `array`/`set` are
  legacy aliases for `unique`), `deep` (`--deep`, promotes `merge=hash` to
  `deep`), `knockout_prefix` (`--knockout-prefix`), `output` (`--output/-o`,
  choice of `raw|json|yaml`, default `"raw"`), `default` (`--default`).
  `-o yaml`/`json` (and raw for a dict/list) redact `Sensitive` values the
  same way raw text already does. Omitting `--merge` lets the data's
  `lookup_options` decide (else first-match-wins); an explicit `--merge`,
  `first` included, always overrides `lookup_options`. Exit codes: `0` key
  found, `1` key missing (and no `--default`), `2` usage/config error (bad
  `--scope`, unreadable/invalid config, or a `HieraError`).

## Gotchas

- A `%{hiera(...)}`/`%{lookup(...)}` call embedded inside a larger string
  must resolve to a scalar; interpolating a non-scalar (list/dict) into a
  string raises `InterpolationError`. A function call standing alone as the
  *entire* value keeps its native (possibly non-scalar) type.
- Nested/inline lookups (function calls resolving other keys) never inherit
  the caller's `merge=` — accumulation happens exactly once, at the
  top-level `get_key` call.
- A missing bare `%{var}` interpolation resolves to `""` (matches Ruby
  Hiera); a missing function-call argument raises `InterpolationError`
  instead — the two failure modes are not symmetric.
- A `lookup_options` key is a **regex only when it starts with `^`**
  (Hiera 5's rule); anything else is matched literally, so a key containing
  `.` cannot shadow-match unrelated keys. An exact key match wins over a
  pattern; an invalid pattern is skipped rather than raising.
- A **dotted reference** (`%{trusted.certname}`, `%{facts.os.family}`) is
  nested *mapping* access into the context, in hierarchy paths, `data_dir`,
  `mapped_paths` templates, values, `.format()`, and `%{scope('a.b')}`
  alike. Numeric segments index lists (`%{roles.0}`). A context key that
  literally contains dots takes precedence over the nested walk. An
  unresolvable reference skips the hierarchy level (in a path) or
  interpolates as `""` (in a value) — it never raises.
- `HOCONBackend`'s include handling differs from Puppet in two deliberate
  places, both erring toward raising rather than silently doing what Puppet
  does: Puppet's `include file(...)` reads the named file (cwd-relative or
  absolute); pyera always raises `BackendError` instead, since reading a
  file a data file names, from wherever the process happens to run, is
  exactly the exposure being closed. Puppet keeps an `include` directive
  written in value position (`msg = please include "x"`) as literal text;
  pyera raises there too. Tracked as a project finding for
  `backend_registry_and_data_loading` to weigh.
