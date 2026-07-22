# `hiera` — public API header

Header-file-style reference for the `hiera` package: every `__all__` export
with its signature, arguments, contract, and gotchas, so this module can be
consumed without reading its source. Kept current with the public API. For
the project overview, see the shipped `README.md`, or <https://github.com/jose-pr/hiera>.

## Engine (`phiera.py`)

- **`Hiera(base_config, backends=None, base_path=None, context=None, **kwargs)`**
  — the main entry point. `base_config`: a file path, a file-like object, or
  a pre-parsed `dict` (a Hiera 5 base config: `version`, `defaults`,
  `hierarchy`, `default_hierarchy`). `backends`: list of `Backend` classes,
  defaults to `default_backends()`. `base_path`: root that relative
  `data_dir`/paths resolve against (defaults to the config file's directory,
  or `os.getcwd()` for a dict/file-like config). `context`/`kwargs`: default
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
  - **`.has(key, **kwargs) -> bool`** — `True` iff `.get(key, throw=True,
    **kwargs)` would not raise `KeyError`.
  - **`.scoped(context=None, **kwargs) -> ScopedHiera`** — bind context
    variables once for reuse.
  - **`.sources(context=None, **kwargs) -> list`** — resolve+load the
    ordered candidate source paths for a context (cached per resolved
    context; a fresh `Hiera` instance if the on-disk tree may have changed).
  - **`.format(text, context=None, **kwargs) -> str`** — resolve `%{var}`
    references in an arbitrary string against the instance context.
  - Gotcha: a single `Hiera` instance caches both parsed file contents
    (`.cache`) and resolved source-path lists (`._source_cache`) — it does
    not notice on-disk changes after first load for a given context.
- **`ScopedHiera(hiera, context=None)`** — wraps a `Hiera` with a bound
  context; `.get`/`.has` merge the bound context under per-call overrides.
  Unknown attributes proxy to the wrapped `Hiera`.
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
  post-merge.
- **`HieraLevel`** (`NamedTuple`: `backend`, `sources`, `glob`, `mapped`) —
  one hierarchy entry. `.new(conf, backend)` builds one from a hierarchy
  dict (`path`/`paths`/`glob`/`globs`/`mapped_paths`). `.paths(base_path,
  context)` yields candidate source paths for a context; a source
  referencing an absent context var is silently skipped.
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
  `OSError`.
- **`JSONBackend`** — `NAMES = ("json_data", "json")`. `json.loads` with
  `object_pairs_hook=LookupDict`; raises `BackendError` on decode failure.
- **`HOCONBackend`** — `NAMES = ("hocon_data", "hocon")`. Requires the
  optional `pyhocon` dependency (`pip install hiera[hocon]`); raises
  `BackendError` naming the extra if it's not installed.
- **`has_hocon() -> bool`** — `True` iff `pyhocon` is importable.
- Env: none. `SOPS_TIMEOUT` is a module attribute, not an env var — set it
  directly (`hiera.backends.SOPS_TIMEOUT = 60`) to change the sops timeout.

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
not be resolved). All are also re-exported from `hiera.__init__` (except
`BackendError`, which lives on `hiera.backends`/`hiera.BackendError` — both
paths work since `__init__` re-exports it too).

## CLI (`cli.py`)

- **`main(argv=None) -> int`** — the `hiera` console-script entry point;
  builds and dispatches the `Lookup` duho command (`duho.main`), which sets
  up `-v/-q/--loglevel` logging and returns the process exit code.
- **`Lookup`** — the `duho.Cli` command class. Fields: `key` (positional),
  `config` (`--config/-c`, default `"hiera.yaml"`), `scope` (`--scope/-s`,
  repeatable `key=value`), `merge` (`--merge`, choice of
  `first|unique|hash|deep|array|set`, default `"first"`; `array`/`set` are
  legacy aliases for `unique`), `deep` (`--deep`, promotes `merge=hash` to
  `deep`), `knockout_prefix` (`--knockout-prefix`), `output` (`--output/-o`,
  choice of `raw|json|yaml`, default `"raw"`), `default` (`--default`).
  Exit codes: `0` key found, `1` key missing (and no `--default`), `2`
  usage/config error (bad `--scope`, unreadable/invalid config, or a
  `HieraError`).

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
