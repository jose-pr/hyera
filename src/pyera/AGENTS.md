# `pyera` — public API header

Header-file-style reference for the `pyera` package: every `__all__` export
with its signature, arguments, contract, and gotchas, so this module can be
consumed without reading its source. Kept current with the public API. For
the project overview, see the shipped `README.md`, or <https://github.com/jose-pr/pyera>.

Install and import as `pyera` (`pip install pyera`, extras
`[cli]`/`[hocon]`); the command is `pyera`. Import every public name from
`pyera` itself, never from a submodule directly — `pyera._*` modules are
private engine internals with no stability contract.

## Engine

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
  format variables merged into every call's context. Raises `ConfigError`
  for anything about `hiera.yaml` — missing, unreadable, a directory,
  unparsable, non-mapping, or wrong-shape (bad `version`, missing
  `hierarchy`, unknown `data_hash`, a malformed hierarchy level) — and
  `BackendError` (`.path` names it) for a data file that cannot be read or
  parsed. Context-free hierarchy levels are loaded by the constructor, so a
  `BackendError` can come from `Hiera(...)` itself, not only from a lookup.
  - **`.get(key, default=None, merge=None, merge_deep=False, throw=False, context=None, **kwargs)`**
    — resolve `key`. `key` must be a `str`; anything else raises `TypeError`.
    `merge`: a strategy name (`"first"`/`"unique"`/`"hash"`/`"deep"`), a
    legacy type (`list`/`set`/`dict`), or a dict `{"strategy": "deep",
    "knockout_prefix": ..., "sort_merged_arrays": ..., "merge_hash_arrays":
    ...}`. Omitted → the data's `lookup_options` key decides, else
    first-match-wins. `merge_deep`: legacy flag, promotes a `dict`/`"hash"`
    merge to `"deep"`. `throw=True` raises `KeyNotFoundError` (a `KeyError`)
    instead of returning `default` on a miss. Falls back to
    `default_hierarchy` when the main hierarchy misses. `context`/`kwargs`
    layer over the instance's default context for this call only.
  - **`.has(key, context=None, **kwargs) -> bool`** — `True` iff
    `.get(key, throw=True, context=context, **kwargs)` would not raise
    `KeyNotFoundError`. `context`/`kwargs` layer over the instance context
    exactly as in `.get`, and reach hierarchy path resolution as well as
    interpolation. A non-`str` `key` still raises `TypeError`.
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
    what it read at construction). `Hiera` and `ScopedHiera` survive
    `copy.deepcopy` and `pickle` (a spawn-start process pool can receive
    one; a relative config path stays relative to the receiving process's
    working directory), which copies the parsed-data cache too,
    sops-decrypted values included.
    Concurrent `.get()` calls on one instance from multiple threads are safe
    on GIL builds, where they only mutate that instance's own caches
    (untested on free-threaded builds).
- **`ScopedHiera(hiera, context=None)`** — wraps a `Hiera` with a bound
  context; `.get(key, ..., context=None, **kwargs)` and
  `.has(key, context=None, **kwargs)` merge the bound context *under*
  per-call overrides, so a per-call value always wins. Unknown attributes
  proxy to the wrapped `Hiera` (dunder names and `hiera` itself excepted);
  instances survive `copy`, `copy.deepcopy` and `pickle`.
- **`make_merge(spec) -> Merge | None`** — normalize a `merge=` spec (name,
  legacy type, or options dict) into a `Merge` accumulator, or `None` for
  first-match. Raises `MergeError` on an unrecognized strategy/type.
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

## Backends (`backends.py`)

A self-registering registry: every format or provider
is a `Backend` subclass, found by name rather than passed around directly.

- **`Backend.KINDS = ("function", "v3", "format", "render")`** — four
  separate name namespaces. `function` is the Hiera 5 `data_hash`/
  `lookup_key`/`data_dig` value in a hierarchy level (Puppet function
  names only); `v3` is empty for the built-ins (the v3/v4 config
  reader maps its own names); `format` is a plain serialization name;
  `render` is a CLI/MCP output-format name.
- **`Backend.NAMES: dict`** — `{kind: (name | NamePattern, ...)}`, declared
  on the defining class only (never inherited/merged); read once at
  subclass-definition time. **`NamePattern(display, regex)`** registers by
  regex instead of an exact string; `regex.fullmatch(name)`'s named groups
  become constructor keywords (used by the `sops_<format>` pattern).
  **`Backend.EXTENSIONS: tuple`** — file extensions (with the dot) this
  format answers to, used by `.for_path`.
- **`Backend(conf=None, *, strict=None)`** — `.conf`; `.datadir` reads
  `conf["datadir"]` or `conf["data_dir"]` (Hiera-5 spelling), default `""`
  (`config_loading_and_validation` removes this fallback later). `.strict`
  (read-only property) is the constructor's `strict=` when given, else the
  call-time default (`"warning"` until `interpolation_engine` points
  it at a `Scope.strict`-backed `ContextVar`) — read at call time, never
  cached, since one backend instance is shared across scopes. `.name`
  defaults to the class's first registered name; `Backend.new` sets it to
  whatever name was actually asked for.
- **Lookup** — `Backend.find(name, kind="function") -> type | None` (exact
  names win, then patterns in registration order); `.get(name, kind) ->
  type` (raises `BackendError` for an unknown name, and via
  `.check_available()` for a registered-but-unusable one, e.g. missing
  `pyhocon`); `.new(name, conf=None, *, kind="function", strict=None) ->
  Backend` (instantiates, passing any `NamePattern` captures as keywords);
  `.names(kind="function") -> list[str]` (exact names, then pattern
  displays); `.for_path(path) -> type | None` (`format`-kind class with the
  longest case-sensitive `EXTENSIONS` suffix match); `.implements(op) ->
  bool` (derived from method overrides, never declared twice).
- **Serialization (json-module shaped)** — `.loads(text)`/`.dumps(obj,
  **kw)` (subclasses implement; base raises `NotImplementedError`);
  `.load(source)` (path-like or a file object: reads the bytes, decodes
  strict UTF-8 — `context.rb:53` — then calls `.loads`; a decode error or a
  `.loads` `BackendError` becomes `BackendError("Unable to parse (<path>):
  <problem>", path=...)`, raised outside the `except` block so no
  `__cause__`/`__context__` holds the original); `.dump(obj, fp, **kw)`
  writes `.dumps(...)`.
- **Hiera 5 provider hooks** — `.data_hash(path, options)` (base:
  `._as_data_hash(self.load(path), path)`); `._as_data_hash(parsed, path)`
  adapts a parsed document into hiera data (base: identity; `YAMLBackend`
  overrides it for the non-Hash rule). `.lookup_key(key, options, context)`
  / `.data_dig(key_segments, options, context)` raise `NotImplementedError`
  in the base (no built-in implements them yet;
  `function_providers_and_eyaml` adds one).
- **`default_backends() -> list[type[Backend]]`** — the distinct classes
  registered in the `function` namespace, in definition order:
  `[YAMLBackend, JSONBackend, HOCONBackend, SopsBackend]`.
  `Hiera(backends=...)` takes this same kind of list as an allow-list; a
  `data_hash` name whose registered class is not in it is refused exactly
  like an unknown name.
- **`YAMLBackend`** — `NAMES = {"function": ("yaml_data",), "format":
  ("yaml",), "render": ("yaml",)}`, `EXTENSIONS = (".yaml", ".yml")`.
  `.loads` is `pyera._yaml_loader.safe_load` (Psych's parsing rules, not
  PyYAML's own) into a plain `dict`/`list` (no `LookupDict` here; the
  engine adapts); raises `BackendError` on a YAML error, one line, Psych's
  shape (`<problem> <context> at line L column C`, 1-based; either part may
  be absent) — never a source snippet or the underlying value, with no
  exception chain. `._as_data_hash` ports `yaml_data.rb:27-35`: a `dict`
  (with any `RubySymbol` key normalized to its plain-string name) passes
  through; `None`/`False` always warn-and-empty (`{}`, even under
  `strict="error"`); any other non-dict value raises `BackendError` under
  `strict="error"`, else warns-and-empties. `.dumps` is
  `yaml.safe_dump(sort_keys=False, allow_unicode=True,
  default_flow_style=False)`.
- **`pyera._yaml_loader`** (private) — ports Psych 5.3.1's `safe_load` +
  `ScalarScanner#tokenize` on top of PyYAML (`CSafeLoader`/libyaml when
  available, else the pure `SafeLoader`; both are wired identically, so
  results only differ on one known gap — see the gotcha below).
  `safe_load(text) -> object`: a leading BOM (U+FEFF) is replaced with a
  single space (reproduces every probed Psych BOM outcome without scanner
  changes — it does *not* strip the BOM, unlike some other Ruby file-read
  paths; see the gotcha below); only the first YAML document is read; a
  `None` result (empty/comment-only/`~` document) becomes `False`
  (`util/yaml.rb:28-41`). Numbers, booleans, `null`, symbols and
  sexagesimal values are resolved by `_tokenize`, a line-by-line port of
  `scalar_scanner.rb`, not PyYAML's own (Python-flavored) implicit
  resolvers — a custom `resolve()` override retags every implicit plain
  scalar with a private tag before PyYAML's own bool/int/float/null
  resolvers ever see it (the literal `<<` merge-key scalar is the one
  exception, so `flatten_mapping` keeps recognizing it). A YAML
  date/timestamp-shaped scalar raises `BackendError("Tried to load
  unspecified class: Date"/"...: Time")` — like Puppet, there is no lenient
  mode. `RubySymbol(name)` (re-exported from `pyera.backends`, `__slots__`,
  not a `str` subclass) represents a Ruby `:symbol`; `symkeys_to_string(obj)`
  recursively turns `RubySymbol` **keys** (not values) into their plain
  string names — used for both data files and `hiera.yaml`. An unknown tag
  is tokenized (scalar), listed (sequence) or dict-built (mapping) like an
  untagged node of the same kind, matching `to_ruby.rb`'s default case — it
  is *not* a parse error, unlike plain PyYAML. A `!ruby/object`/`!ruby/regexp`/
  etc. (other than `!ruby/sym(bol)`/`!ruby/string`) raises the same
  disallowed-class `BackendError`, naming the class from the tag text.
  `!!set`/`!!omap` follow Psych (`!!set` is always disallowed —
  `Psych::Set` is never a permitted class; `!!omap` builds a `dict` from
  its pairs). A duplicate mapping key: the last one wins, matching
  `construct_mapping`'s own behavior; an unhashable key (a list/dict from a
  complex `? ... : ...` key) is frozen into a hashable tuple, recursively.
- **`JSONBackend`** — `NAMES = {"function": ("json_data",), "format":
  ("json",), "render": ("json",)}`, `EXTENSIONS = (".json",)`. `.loads`
  parses the way Ruby's `json` gem (MultiJson's `JsonGem` adapter, Puppet's
  own JSON parser) does, not plain `json.loads`: `/* ... */` and `// ...`
  comments outside string literals are accepted (stripped to spaces before
  parsing, so error line/column still line up); `NaN`/`Infinity`/
  `-Infinity` are rejected (`unexpected token '<name>'`); an unescaped lone
  (unpaired) surrogate code point anywhere in a string, key or value, is
  rejected (`incomplete surrogate pair`) — Python's own decoder accepts
  both by default. Raises `BackendError` on any of these, one line: either
  `<msg> at line L column C` (`json.JSONDecodeError`'s own fields) or the
  comment/NaN/surrogate message above, no exception chain. `.dumps` is
  `json.dumps(ensure_ascii=False)`.
- **`HOCONBackend`** — `NAMES = {"function": ("hocon_data",), "format":
  ("hocon",)}`, `EXTENSIONS = (".conf",)`. Always registered (Design Q5 of
  `backend_registry_and_data_loading/registry`): a missing/broken `pyhocon`
  fails at `.check_available()` (backend/level construction, so a
  `hocon_data` hierarchy level fails to build) *and* in `.loads`, both
  naming the `pyera[hocon]` extra, rather than silently vanishing from
  `default_backends()`. `has_hocon() -> bool` — `True` iff `pyhocon`
  imports without error; any import-time exception (not just
  `ImportError`) is caught and logged at debug. `.loads` parses through a
  *private copy* of the `pyhocon.config_parser` module (`_hocon_parser()`,
  built once under a lock and cached; the copy's `get_period_expr` is
  replaced with a grammar that never matches), so a duration (`10s`,
  `5 minutes`) or size string (`10MB`) stays literal text — matching real
  Ruby hocon, which has no duration/size type at all — instead of becoming
  a `datetime.timedelta` (which used to crash `-o yaml`); the *shared*
  `pyhocon` module (what a third party importing `pyhocon` directly sees)
  is never touched. A root value that is not an object (e.g. a top-level
  `[1, 2]`) raises `BackendError("... has type LIST rather than object at
  file root")`. `.loads` returns plain `dict`/`list` (`ConfigTree`/
  `ConfigList` converted recursively). Invalid UTF-8 (handled by the base
  `.load`), and any other pyhocon parse failure, raise `BackendError` with
  a one-line message (`str(e)`, whitespace-collapsed), no exception chain.
  `include` directives are
  sanitized before pyhocon ever parses the text, so pyhocon's own include
  machinery (file reads relative to the process cwd, `http(s)`/`file` URL
  fetches) never runs: a plain `include "..."` contributes nothing,
  matching Puppet; every other form — `file(...)`, `url(...)`,
  `classpath(...)`, `required(...)`, `package(...)`, any other `name(...)`,
  a case-mismatched keyword (`INCLUDE ...`), a bare `include` with nothing
  valid after it, or an `include` directive in value position — raises
  `BackendError` instead (two of these, `file()` and value position,
  differ from what Puppet itself does; see the gotcha below). `${VAR}`
  substitutions fall back to environment variables, as in Puppet. As a
  fail-closed backstop, pyhocon's own include-resolving methods
  (`parse_file`/`parse_URL`/`resolve_package_path`) also raise for the
  duration of `.loads()`, so an undiscovered gap in the text scanner still
  cannot read a file or reach the network; they behave normally for any
  other pyhocon use in the same process, before or after.
- **`SopsBackend`** — `NAMES = {"function": ("sops_data",)}` (a `sops`
  alias and a `sops_<yaml|json|ini|dotenv>` `NamePattern` are added by a
  a later commit). Not a `YAMLBackend` subclass; `__init__(conf=None,
  *, strict=None, format=None)` — `format` is set by the `NamePattern`
  capture, else inferred. `.data_hash` infers the format from the file's
  extension with **sops's own rule**, case-sensitive (`cmd/sops/formats/
  formats.go`, verified against the real v3.13.3 binary and source
  2026-09-29): `.yaml`/`.yml` → yaml, `.json` → json, `.env` → dotenv,
  `.ini` → ini, anything else → `ConfigError` (sops would read it as
  binary, which is not a data hash) — `_SOPS_SUFFIXES`, checked in that
  order via `str.endswith`. It shells out to `sops -d` (hardened for
  unattended use: `SOPS_TIMEOUT`, module-level, default `30` seconds,
  bounds the subprocess; a missing `sops` binary or non-zero exit raises
  `BackendError` with captured stderr rather than hanging or raising a raw
  `OSError`; invoked as `[<abs sops path>, "--input-type=<fmt>",
  "--output-type=<fmt>", "-d", "--", <abs data path>]` — the data path is
  always absolute and after a literal `--`, so a path or scope value
  starting with `-` can never be parsed as a `sops` option; a
  `sops.bat`/`sops.cmd` shim is refused, `cmd.exe` re-parses a batch file's
  own argument line), then parses the decrypted bytes with the inferred
  format's own registered `format`-namespace backend (`Backend.new(fmt,
  kind="format")`) — YAML keeps `yaml_data`'s non-Hash rule; JSON/INI/
  dotenv get the engine's generic Hash check instead. A decrypted file
  that fails to parse raises a one-line, chain-free
  `BackendError("Unable to parse (<path>): <problem>", path=...)` — never
  the decrypted plaintext. **Gotcha:** sops re-emits YAML through its own
  Go YAML writer, which changes shape on decrypt — a date-shaped scalar
  becomes a full ISO timestamp (`2024-01-15` → `2024-01-15T00:00:00Z`,
  still disallowed by `_yaml_loader`, just with a different message
  source); tags are stripped (`!foo bar` → `bar`); `!!binary`/`!!null`
  round-trip to plain text/`null`; octal ints are re-emitted as decimal
  (`0755` → `493`); a `:symbol` scalar/key survives as plain `:name` text
  (still parses to a `RubySymbol`/normalizes via `symkeys_to_string` on our
  side, same as any other YAML source).
- **`IniBackend`**/**`DotenvBackend`** — `format`-namespace only (`ini`/
  `dotenv`; no Puppet `data_hash` equivalent, reachable only through
  `SopsBackend`). Each parses **exactly the shape sops's own writer
  emits** (`stores/ini/store.go`/`stores/dotenv/store.go`), not a general
  INI/dotenv dialect — sops's own `--output-type json` view is the
  acceptance oracle both were verified against (2026-09-29, real sops
  3.13.3). `IniBackend.loads(text) -> {"DEFAULT": {...}, <section>:
  {...}, ...}`: lines before any header go to `DEFAULT` (always present,
  even empty); a repeated `[section]` header overwrites the previous one
  (matching "last wins" when sops's own duplicate-section JSON is parsed);
  a backtick-wrapped key/value (`` `k` ``) or a `"..."`-wrapped value is
  unwrapped; a `"""..."""` value may span multiple lines; a line with
  neither a header nor an `=` raises `BackendError("invalid ini line
  <n>")` — never the line's own text. `DotenvBackend.loads(text) -> dict`:
  blank lines and `#`-prefixed lines are skipped; the first `=` splits
  key/value; a literal two-character `\n` in the value becomes a real
  newline; a line with no `=` raises `BackendError("invalid dotenv line
  <n>")` — same no-content-in-the-message rule.
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

`HieraError(*args, path=None)` (base; `.path` names the file concerned, or
`None`) →

- **`ConfigError`** — anything about `hiera.yaml`: missing, unreadable,
  unparsable, non-mapping, or wrong shape. A read/shape problem's message
  names the origin directly; an unparsable file's is `(<path>): <problem>
  at line L column C` (Psych's shape, one line).
- **`BackendError`** — a data file could not be read or parsed. `.path`
  names it; an unparsable file's message is `Unable to parse (<path>):
  <problem> at line L column C`, one line. Can be raised from `Hiera(...)`
  itself (context-free levels are loaded by the constructor) as well as
  from a lookup.
- **`HieraLookupError`** — Puppet's `LookupError`: a failure while resolving
  a key. →
  - **`InterpolationError`** — a `%{...}` interpolation or function call
    could not be resolved.
  - **`MergeError`** — an unknown or invalid merge strategy.
  - **`KeyNotFoundError`** (also a `KeyError`) — `.get(..., throw=True)`'s
    miss, with Puppet's message ("Function lookup() did not find a value
    for the name '<key>'", or the "any of the names [...]" plural form).
    `.name` holds the key(s) tried.

Every class above is importable directly from `pyera` (e.g. `pyera.BackendError
is pyera.backends.BackendError`, both paths work since `pyera.__init__`
re-exports it too).

## CLI (`cli.py`)

- **`main(argv=None) -> int`** — the `pyera` console-script entry point;
  builds and dispatches the `Lookup` duho command (`duho.main`), which sets
  up `-v/-q/--loglevel` logging and returns the process exit code. When the
  `cli` extra (`duho`) is not installed, `main` always exists but prints
  `pyera: the command-line interface needs the cli extra: pip install
  "pyera[cli]"` to stderr and returns 2, instead of raising
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
  `first` included, always overrides `lookup_options`. Exit codes: `0`
  found (or `--default` printed), `1` the key was not found (a
  `KeyNotFoundError` and nothing else), `2` any other error. A `2` logs
  exactly one `pyera`-logger ERROR line: `Lookup of key 'K' failed: …` for
  a lookup failure (construction included), `Cannot render the value of
  key 'K': …` if printing the found/default value itself fails. The
  traceback is omitted unless `-v` or `DUHO_TRACEBACK=1` is set.
- Env: `PYERA_MCP=stdio` runs the command as an MCP server over
  stdin/stdout (duho), exposing one tool, `pyera` (`Lookup`'s
  `_parsername_`, not its class name), whose arguments are the CLI fields
  (`key`, `config`, `scope`, ...); a `tools/call` returns what the command
  would print, and `initialize`'s `serverInfo.name` is `"pyera"` too. Any
  other `PYERA_MCP` value exits `2` with `unsupported MCP transport`. The
  trigger variable name itself is always `PYERA_MCP`, from that same
  `_parsername_`, regardless of `sys.argv[0]` (so `python -m pyera.cli` or
  embedding `Lookup` in a differently-named script never changes it). The
  trigger is read before the arguments, so an MCP session never performs a
  command-line lookup. A
  truthy `AGENT_HELP` or `AGENTS_HELP` makes
  `--help` print duho's JSON agent-help document instead of usage text.

## Gotchas

- A `%{hiera(...)}`/`%{lookup(...)}` call embedded inside a larger string
  must resolve to a scalar; interpolating a non-scalar (list/dict) into a
  string raises `InterpolationError`. A function call standing alone as the
  *entire* value keeps its native (possibly non-scalar) type.
- Nested/inline lookups (function calls resolving other keys) never inherit
  the caller's `merge=` — accumulation happens exactly once per lookup, at
  the top level.
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
- **A BOM behaves differently in a data file than in `hiera.yaml` vs. how
  it might look at first** — actually the *same* either way, and that is
  itself the gotcha: `puppet lookup` reads hiera.yaml via `HieraConfig` ->
  `cached_file_data` -> `Puppet::Util::Yaml.safe_load(content, ...)`
  directly on the file's raw content, *not* through
  `Puppet::Util::Yaml.safe_load_file`'s BOM-*stripping* file read. A
  leading BOM therefore reaches the YAML parser exactly the same way for
  both — kept, then swapped for a single space by `safe_load`'s BOM
  handling. One real consequence: a BOM'd `hiera.yaml` (or data file) whose
  top-level mapping has more than one key on separate lines silently keeps
  only the *first* key (the swapped space shifts that one line's column,
  so a same-column second key one line down no longer matches) — a real
  Puppet limitation, reproduced faithfully rather than "fixed". A
  flow-style (`{...}`) or single-key mapping has no such problem.
- **libyaml (the C loader) accepts a tab after `:` in a plain scalar
  (`plain:\tp`); the pure-Python loader does not** — the one behavioral gap
  between `_C_LOADER` and `_PURE_LOADER`. Both venvs and every published
  wheel ship libyaml, so this is a real fallback path (a source build
  without it, or `PyYAML` built `--no-libyaml`), not a hidden dead branch;
  it is tested and documented, not worked around.
- A YAML **complex key** (`? [a, b]\n: 1`, or a Hash key) parses to a
  hashable tuple (recursively frozen), and a **symbol value** (`:foo`,
  `!ruby/symbol x`) parses to a `RubySymbol` — both load without error, but
  neither is a valid Puppet lookup *value*, and `pyera` does not reject
  them yet (`lookup_pipeline_and_api`'s RichData check does); a value keyed
  or shaped this way currently returns successfully instead of erroring
  like Puppet.
- **`None`/`null`/`~` as an actual data value is indistinguishable from "key
  not found"** in the engine's own navigation (`Hiera._get_key` treats
  `cache is None` as "keep looking") — a pre-existing limitation, not
  something this plan's YAML work introduced or fixes; a data file legally
  containing `key: ~` currently makes that key un-lookupable.
