# `hyera` — public API header

Header-file-style reference for the `hyera` package: every `__all__` export
with its signature, arguments, contract, and gotchas, so this module can be
consumed without reading its source. Kept current with the public API. For
the project overview, see the shipped `README.md`, or <https://github.com/jose-pr/hyera>.

Install and import as `hyera` (`pip install hyera`, extras
`[cli]`/`[hocon]`); the command is `hyera`. Import every public name from
`hyera` itself, never from a submodule directly — `hyera._*` modules are
private engine internals with no stability contract.

## Engine

- **`Hiera(base_config, backends=None, base_path=None, *, scope=None)`**
  — the main entry point. `base_config`: a file path, a file-like object, a
  pre-parsed `dict` (a Hiera 5 base config: `version`, `defaults`,
  `hierarchy`, `default_hierarchy`), or `None` for Puppet's built-in default
  configuration (`datadir: data`, `data_hash: yaml_data`, a single `Common`
  level at `common.yaml`) rooted at `base_path`. A path is read once, as
  bytes (UTF-8, UTF-8 BOM, or UTF-16 with BOM), and `.base_config` keeps the
  path unchanged (a `str` stays a `str`, a `Path` stays that `Path`); a
  relative path is made absolute at construction, so a later `chdir` cannot
  change what it resolves against. A file-like object is read as given;
  `Hiera` never closes it and `.base_config` keeps that same object. A
  `dict` is deep-copied at construction — `Hiera` never mutates or replaces
  the caller's own dict. `backends`: list of `Backend` classes, defaults to
  `default_backends()`. `base_path`: root that relative `datadir`/paths
  resolve against (defaults to the config file's directory, or `os.getcwd()`
  for a `dict`/file-like/`None` config); also made absolute at construction.
  `scope`: the bound `hyera.Scope` for this instance's lifetime (keyword-only);
  `None` (the default) means `Scope()` — Puppet's own defaults (no facts,
  `$environment` `"production"`, the local `$trusted` hash). Anything other
  than a `Scope`/`None` raises `TypeError("scope must be a hyera.Scope")`.
  `self.scope` is set before the config loads, so a hierarchy path template
  referencing it (`%{trusted.certname}`, `%{environment}`) resolves against
  it from the first, context-free pre-warm onward. A missing or `null`/`false`
  `defaults`/`hierarchy` is filled
  with Puppet's own defaults (`{datadir: data, data_hash: yaml_data}` /
  `[{name: Common, path: common.yaml}]`) rather than raising; a hierarchy
  entry's own `datadir` wins, else `defaults.datadir`, else the literal
  `data`, always resolved next to hiera.yaml (or under `base_path`) — never
  the Hiera 3 absolute `/etc/puppetlabs/...` path. `default_hierarchy` is
  accepted in a single (non-layered) config, and its entries are schema-
  validated exactly like `hierarchy`'s. Each entry uses its own function
  key (`data_hash`/`lookup_key`/`data_dig`/`hiera3_backend`/
  `v4_data_hash`), falling back to `defaults` only when the entry names
  none (`defaults` is never merged into an entry wholesale); `lookup_key`
  and `data_dig` entries raise `ConfigError` for now ("not supported yet"
  for a registered function name, "Unable to find" for an unregistered
  one) — a level that used a `lookup_key` function under `data_hash:
  yaml_data` defaults used to silently read its file as plain YAML,
  returning eyaml ciphertext as the value. Raises `ConfigError` for anything
  about `hiera.yaml` — missing, unreadable, a directory, unparsable,
  non-mapping (naming the Hiera 3 fallback this runtime does not support
  yet), an unsupported `version` (only a literal Integer `5` is accepted; a
  missing `version` or an explicit `3` reads as "hiera.yaml version 3 is
  not supported yet"; `4` reads as "cannot be used in the global layer";
  anything else as "This runtime does not support hiera.yaml version N"),
  or any violation of Puppet's hiera.yaml version 5 schema (an unrecognized
  key anywhere, a missing/duplicate/non-string `name`, more than one
  function or location key, a malformed `options` entry, and the like) —
  with Puppet's own message and, where known, `.path` and `.line` — and
  `BackendError` (`.path` names it) for a data file that cannot be read or
  parsed. Context-free hierarchy levels are loaded by the constructor, so a
  `BackendError` can come from `Hiera(...)` itself, not only from a lookup.
  - **`.get(key, default=None, merge=None, throw=False)`**
    — resolve `key` against the instance's bound scope. `key` must be a
    `str`; anything else raises `TypeError`. A dotted `key` follows Puppet's
    sub-key grammar (see the dotted reference gotcha below) and can itself
    raise `HieraLookupError` (a malformed key, or a type mismatch during the
    walk) — **even when a `default` was given**; only a genuine miss falls
    back to it. `merge`: one of Puppet's strategy names (`"first"`/
    `"default"`/`"unique"`/`"hash"`/`"deep"`) or a dict `{"strategy":
    "deep", "knockout_prefix": ..., "sort_merged_arrays": ...,
    "merge_hash_arrays": ...}`. Omitted → the data's `lookup_options` key
    decides, else first-match-wins. Invalid input (an unknown strategy, a
    strategy hash with no `strategy` key, an unrecognized or mistyped
    option, a `hash`/`unique` merge of a value the strategy rejects) raises
    `hyera.MergeError`. `throw=True` raises `KeyNotFoundError`
    (a `KeyError`) instead of returning `default` on a miss. Falls back to
    `default_hierarchy` when the main hierarchy misses.
  - **`.has(key) -> bool`** — `True` iff `.get(key, throw=True)` would not
    raise `KeyNotFoundError`, against the same bound scope. A non-`str`
    `key` still raises `TypeError`; a dotted `key`'s own
    `HieraLookupError`/`InterpolationError` propagates too — only a genuine
    miss becomes `False`.
  - **`.scoped(*, variables=None, facts=None, trusted=None,
    server_facts=None, environment=None, strict=None, node_name=None) ->
    ScopedHiera`** — `ScopedHiera(self, self.scope.derive(...))`: a view
    bound to a scope derived from this instance's own (see `Scope.derive`).
  - **`.sources() -> list`** — resolve+load the ordered candidate source
    paths for the bound scope (cached per scope value; a fresh `Hiera`
    instance if the on-disk tree may have changed).
  - **`.format(text) -> str`** — resolve `%{var}` references in an
    arbitrary string against the bound scope. A dotted reference follows
    the same grammar and can raise the same way.
  - Gotcha: a single `Hiera` instance caches parsed file contents
    (`.cache`), resolved source-path lists (`._source_cache`) and the merged
    `lookup_options` mapping (`._lookup_options_cache`), all keyed per
    `Scope` value — it does not notice on-disk changes after first load for
    a given scope.
  - Gotcha: a path-configured `Hiera` holds no open file, so the config file
    can be replaced or removed on disk while the instance lives (it keeps
    what it read at construction). `Hiera` and `ScopedHiera` survive
    `copy.deepcopy` and `pickle` (a spawn-start process pool can receive
    one; a relative config path stays relative to the receiving process's
    working directory), which copies the parsed-data cache too,
    sops-decrypted values included (`Scope`'s own warning-dedup state is
    NOT carried over verbatim — its internal lock cannot be pickled, so a
    copy starts with the same dedup keys but a fresh, unlocked mutex).
    Concurrent `.get()` calls on one instance from multiple threads are safe
    on GIL builds, where they only mutate that instance's own caches
    (untested on free-threaded builds).
- **`ScopedHiera(hiera, scope)`** — a `Hiera` with a bound (derived)
  `Scope`; every method (`.get`/`.has`/`.sources`/`.format`/`.scoped`) has
  `Hiera`'s own signature and uses `self.scope` instead of `hiera.scope`.
  `.scoped(...)` layers: it derives from `self.scope`, not `hiera.scope`, so
  nested `scoped()` calls compose instead of each restarting from the
  instance's own scope. Unknown attributes proxy to the wrapped `Hiera`
  (dunder names, `hiera` and `scope` themselves excepted); instances survive
  `copy`, `copy.deepcopy` and `pickle`.
- **`HieraLevel`** (`NamedTuple`: `backend`, `sources`, `glob`, `mapped`) —
  one hierarchy entry. `.new(conf, backend)` builds one from a hierarchy
  dict (`path`/`paths`/`glob`/`globs`/`mapped_paths`). `.paths(base_path,
  scope)` yields candidate source paths for a bound `Scope`; a source
  referencing an unbound/undefined variable is silently skipped. A glob whose
  directory does not exist yields nothing (matches Puppet), instead of
  raising from the underlying filesystem glob.
- **`Sensitive(value)`** — redacting wrapper produced by `convert_to:
  Sensitive`, mirroring Puppet's `Sensitive` type (`p_sensitive_type.rb`).
  `str()`/`repr()` both show `Sensitive [value redacted]`; `.unwrap()`
  returns the real value. Equality and hashing follow Puppet: two
  `Sensitive` values are equal (and hash equal) exactly when their wrapped
  values are Ruby-`eql?` — `1`, `1.0` and `True` are distinct wrapped
  values, but a list or dict payload compares/hashes by content (in any key
  order for a dict) despite being unhashable in plain Python.

## Scope (`_scope.py`)

Puppet's top scope, as one immutable, hashable value, bound to every
`Hiera`/`ScopedHiera` instance (`Hiera(..., scope=...)`, `.scope`,
`.scoped(...)`). Logger `hyera._scope`.

- **`Scope(*, variables=None, facts=None, trusted=None, server_facts=None,
  environment=None, strict="warning", node_name=None)`** — every argument
  keyword-only. `variables`/`facts`/`server_facts`/`trusted` are each `None`
  or a mapping with `str` keys; `variables`/`server_facts`/`trusted` values
  must additionally be Puppet Data (`None`, `bool`, `int`, `float`, `str`,
  a list/tuple — stored as a list — or a `str`-keyed dict of Data, recursively)
  or construction raises `TypeError("Unsupported data type: '<type
  name>'")`; every input is deep-copied. `strict` must be `"off"`,
  `"warning"` or `"error"`, else `ValueError`. `environment` must be `None`
  or a non-empty `str`; `node_name` must be `None` or a `str` — otherwise
  `TypeError`/`ValueError`.

  Built in Puppet's own precedence order (`compiler.rb`'s
  `set_node_parameters`, `node.rb`, `trusted_information.rb`,
  `node/facts.rb`'s `sanitize_fact`, `parser/scope.rb`):
  1. `variables` become the node parameters, in order.
  2. `$environment` resolves to the explicit argument, else a `str`
     `variables["environment"]`, else `"production"`.
  3. `facts` are *sanitized*, not validated (recursively: dicts and
     lists/tuples recurse — tuples become lists — `bool`/`int`/`float`/`str`
     are kept, `None` becomes `""`, anything else becomes `str(value)`; a
     list/tuple used as a fact *key* raises `TypeError`), then merged into
     the parameters without overriding an existing one of the same name — a
     collision keeps the old value and logs one warning: "The node
     parameter '\<name>' for node '\<node_name>' was already set to
     '\<old>'. It could not be set to '\<new>'." (the `for node '...'`
     clause is omitted when `node_name` is `None`).
  4. `server_facts` (plus `environment` forced in last, so it always wins)
     merge into the parameters the same collision-safe way.
  5. `$trusted` resolves to the explicit `trusted` argument, as given; else
     a `trusted` parameter/fact is "resurrected" as-is only when it is a
     dict holding `authenticated`, `certname` and `extensions` (a `None`/
     `False` `trusted` parameter is left in place, so step 6 below rejects
     it as reserved); else Puppet's local hash, in this key order:
     `authenticated: "local"`, `certname: <the "clientcert" parameter>`,
     `extensions: {}`, `hostname`, `domain` (from `certname.split(".", 1)`
     Ruby-style: no dot → `domain=None`; falsy `certname` → both `None`),
     `external: {}`. A non-`str` `clientcert` raises `TypeError` ("undefined
     method 'split' for an instance of \<Ruby class>", matching what real
     Puppet crashes with).
  6. Every remaining parameter becomes a top-scope variable, **except**: a
     name matching `^[0-9]+$` raises `ValueError("Cannot assign to a
     numeric match result variable '$<name>'")`; a name of `trusted`,
     `facts` or `server_facts` raises `ValueError("Attempt to assign to a
     reserved variable name: '<name>'")`. Then `$environment`, `$trusted`,
     `$server_facts` (the whole merged hash) and `$facts` (the whole
     sanitized facts mapping, unmerged) are stored under those reserved
     names.
  7. `module_name` (`""`), `title` and `name` (both `"main"`, matching
     `puppet lookup`'s evaluated `main` class) are stored last; any of the
     three already present (from a variable/fact) raises
     `ValueError("Cannot reassign variable '$<name>'")`.
  - **`.environment`/`.strict`/`.node_name`** — read-only properties for the
    resolved values above.
  - **`.lookup(name) -> value | Scope.UNDEFINED`** — no strict side effects
    (Puppet's `catch(:undefined_variable)` form). A leading `::` is
    stripped; a remaining `::` marks the name qualified, but the local
    layers and top table are still checked for a literal match either way
    (there are no class scopes, so a qualified name is undefined unless a
    variable is literally named that way). Only for an unqualified miss:
    `"caller_module_name"` returns `None`, and a bare non-negative integer
    name (`^(?:0|[1-9][0-9]*)$`) returns `None`; anything else unmatched is
    `Scope.UNDEFINED`. A non-`str` `name` raises `TypeError`.
  - **`.exist(name) -> bool`** — `True` iff bound in a local layer or the
    top table, or `name == "caller_module_name"`; a still-qualified name
    (after stripping one leading `::`) or a numeric name is always `False`.
  - **`.lookupvar(name, *, lenient=False) -> value`** — `.lookup(name)`,
    with `.strict` applied to `Scope.UNDEFINED`: `"off"` returns `None`
    silently; `"warning"` warns once per name ("Undefined variable
    '\<name>'", `"; class <X> could not be found"` appended for a
    qualified name) and returns `None`; `"error"` raises
    `InterpolationError` with that same text, **unless** `lenient=True`
    (Puppet's `avoid_hiera_interpolation_errors`, for hierarchy locations),
    which instead warns once ("Interpolation failed with '\<name>', but
    compilation continuing...") and returns `None`. Warnings dedupe per
    root `Scope` (shared by every scope `with_local_scope`/`derive`
    produces from it), up to 100 distinct names tracked, matching Puppet's
    own cap.
  - **`.with_local_scope(variables) -> Scope`** — a child sharing this
    scope's table and warning state, adding one local variable layer
    (`variables`'s keys/values checked the same way as the constructor's
    `variables`). This scope is unchanged; layers are functional, not
    push/pop, since one `Hiera` can serve many concurrent lookups.
  - **`.derive(*, variables=None, facts=None, trusted=None,
    server_facts=None, environment=None, strict=None, node_name=None) ->
    Scope`** — a new root scope, fully rebuilt from this scope's own
    constructor inputs: `variables`/`facts`/`server_facts` shallow-update
    the parent's (new values win, so an unrelated fact never goes stale);
    the rest replace the parent's when given (`environment`/`strict`
    default to *this scope's already-resolved* value, not to their own
    defaults). Local layers are not carried; the warning state is shared.
  - **`Scope.UNDEFINED`** — the lookup-miss sentinel (the same object as
    `hyera._navigation._MISSING`).
  - **Value semantics** — immutable and hashable; `==`/`hash` compare a
    type-tagged rendering of the top table, local layers, `strict` and
    `node_name` (so `True`, `1` and `1.0` are distinct, unlike plain Python
    equality). Do not mutate a value returned by `.lookup`/`.lookupvar`: it
    is this scope's own copy, shared by every caller. `repr()` shows
    `environment`, `strict` and variable/fact counts, never values.

## Facts (`_facts.py`)

Two sources for `Scope(facts=...)`: a Puppet-compatible `--facts` file, and
bare `facter`. Neither sanitizes its result — pass it to `Scope`, which does.

- **`load_facts(path) -> dict`** — `path` is a `str` or `os.PathLike`; every
  message uses `str(path)` as given (`puppet lookup --facts` rules,
  `application/lookup.rb:349-371`). The parser is chosen by extension:
  `.json` → JSON; `.yml`/`.yaml` → YAML; anything else tries JSON, then
  YAML, and a failure of *either* there just means "no result" (not an
  error). For a `.json`/`.yml`/`.yaml` file specifically, a parse failure
  **does** raise. JSON is parsed with Ruby `json`-gem rules: UTF-8 only (a
  BOM fails), `NaN`/`Infinity`/`-Infinity` rejected. YAML goes through
  `hyera._yaml_loader.safe_load` (decoded `utf-8-sig` first, so a BOM is
  stripped, unlike a hiera.yaml/data file) — but
  `Puppet::Util::Yaml.safe_load` permits **no** classes at all, unlike the
  data-file loader, which permits `Symbol`: a `RubySymbol` anywhere in the
  parsed result (key or value, from an explicit `!ruby/sym` tag or a plain
  `:name` scalar alike) raises `BackendError("Tried to load unspecified
  class: Symbol")` the same way a Date/Time already does. The result must be
  a `dict` — anything else (a list, a scalar, an empty file, or "no
  result" from the lenient any-extension path) raises `BackendError`
  ("Incorrectly formatted data in `<path>` given via the --facts flag (only
  accepts yaml and json files)"). `hostname`/`domain`/`fqdn`/`clientcert`
  are all-or-nothing: any one present without the other three raises
  `BackendError` ("When overriding any of the hostname,domain,fqdn,clientcert
  facts with `<path>` given via the --facts flag, they must all be
  overridden."). Every error is a `BackendError` with `.path` set, chained
  from its cause where there is one.
- **`facts_from_facter(*, timeout: int = 30) -> dict`** — runs a bare
  `facter -j` (no queries, no `--show-legacy`: a queried `facter -j a b`
  returns flat dotted keys `$facts` cannot navigate) and returns its JSON
  output as a `dict`. Does **not** add `clientcert`/`clientversion`/
  `clientnoop` — those come from Puppet's agent, not facter; pass
  `clientcert` yourself (e.g. via `Scope(variables={"clientcert": ...})`)
  if `$trusted.certname` should be set. Hardened like `SopsBackend`'s own
  subprocess call: `timeout` (default 30s) bounds it; a missing `facter`
  binary, a timeout, a non-zero exit (stderr captured), or output that
  isn't a JSON object all raise `BackendError` — a timeout is recorded
  inside its `except` and raised after, so `__context__` never carries
  the (possibly partial) output, matching the sops runner's own hardening.

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
  `conf["datadir"]` only (Puppet's only spelling; a config with a
  non-Puppet key is rejected before a level's conf ever reaches a
  backend), default `""`. `.strict`
  (read-only property) is the constructor's `strict=` when given, else the
  call-time default (`"warning"` until the lookup scope's `strict`
  setting is threaded through to backends) — read at call time, never
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
  in the base (no built-in implements them yet).
- **`default_backends() -> list[type[Backend]]`** — the distinct classes
  registered in the `function` namespace, in definition order:
  `[YAMLBackend, JSONBackend, HOCONBackend, SopsBackend]`.
  `Hiera(backends=...)` takes this same kind of list as an allow-list; a
  `data_hash` name whose registered class is not in it is refused exactly
  like an unknown name.
- **`YAMLBackend`** — `NAMES = {"function": ("yaml_data",), "format":
  ("yaml",), "render": ("yaml",)}`, `EXTENSIONS = (".yaml", ".yml")`.
  `.loads` is `hyera._yaml_loader.safe_load` (Psych's parsing rules, not
  PyYAML's own) into a plain `dict`/`list`, unadapted -- dotted-key access
  is a function over that data, not a container method; raises
  `BackendError` on a YAML error, one line, Psych's
  shape (`<problem> <context> at line L column C`, 1-based; either part may
  be absent) — never a source snippet or the underlying value, with no
  exception chain. `._as_data_hash` ports `yaml_data.rb:27-35`: a `dict`
  (with any `RubySymbol` key normalized to its plain-string name) passes
  through; `None`/`False` always warn-and-empty (`{}`, even under
  `strict="error"`); any other non-dict value raises `BackendError` under
  `strict="error"`, else warns-and-empties. `.dumps` is
  `yaml.safe_dump(sort_keys=False, allow_unicode=True,
  default_flow_style=False)`.
- **`hyera._yaml_loader`** (private) — ports Psych 5.3.1's `safe_load` +
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
  mode. `RubySymbol(name)` (re-exported from `hyera.backends`, `__slots__`,
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
  ("hocon",)}`, `EXTENSIONS = (".conf",)`. Always registered: a missing/broken `pyhocon`
  fails at `.check_available()` (backend/level construction, so a
  `hocon_data` hierarchy level fails to build) *and* in `.loads`, both
  naming the `hyera[hocon]` extra, rather than silently vanishing from
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
  is never touched, except for two deprecation shims scoped to the private
  copy only: its `codecs`/`logger` names are replaced so pyhocon 0.3.63's
  own `codecs.open()`/`Logger.warn()` calls (both deprecated on Python
  3.14+) never raise under this project's `filterwarnings = ["error"]`
  when a real `include file(...)` resolves. A root value that is not an
  object (e.g. a top-level `[1, 2]`) raises `BackendError("... has type
  LIST rather than object at file root")`. `.loads` returns plain
  `dict`/`list` (`ConfigTree`/`ConfigList` converted recursively). Invalid
  UTF-8 (handled by the base `.load`), and any other pyhocon parse
  failure, raise `BackendError` with a one-line message (`str(e)`,
  whitespace-collapsed), no exception chain.

  `__init__(conf=None, *, strict=None, hocon_includes=None)` —
  `hocon_includes` (hyera's own extension, not Puppet vocabulary):
  `None` (the default) reads `conf.get("hocon_includes", True)`, so a
  hierarchy entry/`defaults` key of the same name reaches it the same way
  `datadir` already does; an explicit `True`/`False` overrides
  `conf`. `self.hocon_includes` (bool) selects which of two scanners
  `.loads` runs before pyhocon ever parses the text:

  - **`True` (default, matches Puppet's own `hocon_data`):** a plain
    `include "..."` contributes nothing; `include file(...)` (relative or
    absolute, also inside a nested object or after another key) is left
    untouched, so pyhocon's own resolution — which already reads the file
    relative to the process cwd or absolute, exactly as Ruby hocon does —
    runs for real (a missing, non-required target silently contributes
    nothing, matching Puppet); a directive in value position (including
    inside a `[...]` array), of any spelling/case, is defanged into an
    ordinary quoted token so the result is the exact literal text Puppet
    keeps; every other key-position form — `url(...)`, `classpath(...)`,
    `required(...)` (whether or not its target exists), `package(...)`,
    any other `name(...)`, a space before the paren, a case-mismatched
    keyword (`INCLUDE ...`, a dotless-i look-alike), or a bare `include`
    with nothing valid after it — raises `BackendError`, matching
    Puppet's own parse/method errors (Ruby hocon implements none of
    them). One accepted divergence: Puppet's `include file("*.conf")`
    never globs (contributes nothing); pyhocon's own resolution does and
    includes every match (`hocon-file-include-globs-where-puppet-does-not`).
  - **`False` (opt-in restriction, the pre-fidelity behaviour):** every form
    but a plain quoted include raises, `include file(...)` included.

  `${VAR}` substitutions fall back to environment variables, as in
  Puppet, in either mode. As a fail-closed backstop, pyhocon's own
  include-resolving methods (`parse_file`/`parse_URL`/
  `resolve_package_path`) also raise for the duration of `.loads()`, for
  whichever forms the active mode does not intend to resolve for real
  (`url`/`package` always; `file` too when `hocon_includes` is `False`),
  so an undiscovered gap in the text scanner still cannot read a file or
  reach the network; they behave normally for any other pyhocon use in
  the same process, before or after. This backstop wraps **both** the
  shared `pyhocon.config_parser` module and hyera's own private copy
  (`_hocon_parser()`'s `mod` — a *different* `ConfigFactory`/
  `ConfigParser` class from the shared module's own, since
  `.loads` always parses through the private copy).
- **`SopsBackend`** — `NAMES = {"function": ("sops_data", "sops",
  NamePattern("sops_<yaml|json|ini|dotenv>", ...))}`. Not a `YAMLBackend`
  subclass; `__init__(conf=None, *, strict=None, format=None)` —
  `format` is set by the `NamePattern` capture, else inferred.
  `.data_hash` infers the format from the file's extension with **sops's
  own rule**, case-sensitive (`cmd/sops/formats/formats.go`, verified
  against the real v3.13.3 binary and source 2026-09-29): `.yaml`/`.yml`
  → yaml, `.json` → json, `.env` → dotenv, `.ini` → ini, anything else →
  `ConfigError` (sops would read it as binary, which is not a data hash)
  — `_SOPS_SUFFIXES`, checked in that order via `str.endswith`. It shells
  out to `sops -d` (hardened for unattended use: `SOPS_TIMEOUT`,
  module-level, default `30` seconds, bounds the subprocess; a missing
  `sops` binary or non-zero exit raises `BackendError` with captured
  stderr rather than hanging or raising a raw `OSError`; invoked as
  `[<abs sops path>, "--input-type=<fmt>", "--output-type=<out>", "-d",
  "--", <abs data path>]` — the data path is always absolute and after a
  literal `--`, so a path or scope value starting with `-` can never be
  parsed as a `sops` option; a `sops.bat`/`sops.cmd` shim is refused,
  `cmd.exe` re-parses a batch file's own argument line; a
  `subprocess.TimeoutExpired` is recorded and its `BackendError` raised
  only after the `except` block, so `__context__` never carries its
  `.stdout` -- security review R4b, 2026-09-29), then parses the
  decrypted bytes with a `format`-namespace backend (`Backend.new(<out>,
  kind="format")`) — YAML keeps `yaml_data`'s non-Hash rule; JSON/dotenv
  get the engine's generic Hash check instead.
  **`ini` is the one exception to "output type = input type" (security
  review S5, 2026-09-29):** sops's own INI *writer* is ambiguous (a
  decrypted value containing `"""` plus a newline can inject a key or
  replace a whole other section, and no ini-text parser can tell those
  bytes apart from a genuine file — reproduced against real sops 3.13.3),
  so `ini` is always decrypted as `--output-type=json` and parsed with
  `JSONBackend` instead; there is no `IniBackend`. sops's JSON view of an
  ini-format file is exactly `{"DEFAULT": {...}, <section>: {...}, ...}`
  (`DEFAULT` always present, even empty; a duplicate `[section]` "last
  wins" the way a duplicate JSON object key does), confirmed against the
  real binary. `data_hash: sops_ini` still forces sops's `--input-type`
  to `ini` (reads the file as INI) but the output/parse side is always
  `json`, the same as inferred-`ini`.
  A decrypted file that fails to parse raises a one-line, chain-free
  `BackendError("Unable to parse (<path>): <problem>", path=...)` — never
  the decrypted plaintext; a `UnicodeDecodeError` is reported as `invalid
  UTF-8 at byte offset <n>` (never the stock codec message's offending
  byte value); the `raw`/`text` locals are `del`eted before that raise.
  **`_sops_redact_yaml_problem` (security review S7, 2026-09-29):** three
  `_yaml_loader` messages quote the offending scalar or an attacker-
  suppliable class name verbatim (`invalid value for Float()/Integer():
  "<data>"`, `Tried to load unspecified class: <data>` for a `!ruby/...`
  tag) — on the sops decrypt path only (plain `yaml_data` keeps Puppet's
  full text), the quoted/named part is replaced with `<redacted>` unless
  the class name is one of the fixed set `_yaml_loader` itself raises
  unconditionally for a known YAML shape (`Time`, `Date`, `Object`,
  `Psych::Set` — never text lifted from the document).
  **Gotcha:** sops re-emits YAML through its own Go YAML writer, which
  changes shape on decrypt — a date-shaped scalar becomes a full ISO
  timestamp (`2024-01-15` → `2024-01-15T00:00:00Z`, still disallowed by
  `_yaml_loader`, just with a different message source); tags are
  stripped (`!foo bar` → `bar`); `!!binary`/`!!null` round-trip to plain
  text/`null`; octal ints are re-emitted as decimal (`0755` → `493`); a
  `:symbol` scalar/key survives as plain `:name` text (still parses to a
  `RubySymbol`/normalizes via `symkeys_to_string` on our side, same as
  any other YAML source).
- **`DotenvBackend`** — `format`-namespace only (`dotenv`; no Puppet
  `data_hash` equivalent, reachable only through `SopsBackend`). Parses
  **exactly the shape sops's own writer emits** (`stores/dotenv/
  store.go`), not a general dotenv dialect — sops's own `--output-type
  json` view is the acceptance oracle it was verified against
  (2026-09-29, real sops 3.13.3). `DotenvBackend.loads(text) -> dict`:
  blank lines and `#`-prefixed lines are skipped; the first `=` splits
  key/value; a literal two-character `\n` in the value becomes a real
  newline; a line with no `=` raises `BackendError("invalid dotenv line
  <n>")` — never the line's own text. There is no `IniBackend`: see
  `SopsBackend` above (S5) for why `ini` is parsed as JSON instead.
- Env: `sops` runs with the process environment, so its own `SOPS_*` and
  key-source variables apply. `SOPS_TIMEOUT` is a module attribute, not an
  env var — set it directly (`hyera.backends.SOPS_TIMEOUT = 60`) to change
  the sops timeout.

## Exceptions (`exceptions.py`)

`HieraError(*args, path=None)` (base; `.path` names the file concerned, or
`None`) →

- **`ConfigError`** — anything about `hiera.yaml`: missing, unreadable,
  unparsable, non-mapping, an unsupported `version`, or wrong shape. A
  read/shape problem's message names the origin directly; an unparsable
  file's is `(<path>): <problem> at line L column C` (Psych's shape, one
  line). `.line` (in addition to the inherited `.path`) names the 1-based
  line in `.path` a problem was found at, when known (`None` for a dict
  config, or when no line applies); a message that includes a line also
  ends with Puppet's own `(file: F, line: N)` suffix.
- **`BackendError`** — a data file could not be read or parsed. `.path`
  names it; an unparsable file's message is `Unable to parse (<path>):
  <problem> at line L column C`, one line. Can be raised from `Hiera(...)`
  itself (context-free levels are loaded by the constructor) as well as
  from a lookup.
- **`HieraLookupError`** — Puppet's `LookupError`: a failure while resolving
  a key, including a `convert_to` whose type cannot be parsed or whose
  conversion/result-type assertion fails (Puppet's `new()`; see `Sensitive`
  and the `convert_to` Gotcha below for the two message forms). →
  - **`InterpolationError`** — a `%{...}` interpolation or function call
    could not be resolved.
  - **`MergeError`** — an unknown or invalid merge strategy.
  - **`KeyNotFoundError`** (also a `KeyError`) — `.get(..., throw=True)`'s
    miss, with Puppet's message ("Function lookup() did not find a value
    for the name '<key>'", or the "any of the names [...]" plural form).
    `.name` holds the key(s) tried.

Every class above is importable directly from `hyera` (e.g. `hyera.BackendError
is hyera.backends.BackendError`, both paths work since `hyera.__init__`
re-exports it too).

## CLI (`cli.py`)

- **`main(argv=None) -> int`** — the `hyera` console-script entry point;
  builds and dispatches the `Lookup` duho command (`duho.main`), which sets
  up `-v/-q/--loglevel` logging and returns the process exit code. When the
  `cli` extra (`duho`) is not installed, `main` always exists but prints
  `hyera: the command-line interface needs the cli extra: pip install
  "hyera[cli]"` to stderr and returns 2, instead of raising
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
  exactly one `hyera`-logger ERROR line: `Lookup of key 'K' failed: …` for
  a lookup failure (construction included), `Cannot render the value of
  key 'K': …` if printing the found/default value itself fails. The
  traceback is omitted unless `-v` or `DUHO_TRACEBACK=1` is set.
- Env: `HYERA_MCP=stdio` runs the command as an MCP server over
  stdin/stdout (duho), exposing one tool, `hyera` (`Lookup`'s
  `_parsername_`, not its class name), whose arguments are the CLI fields
  (`key`, `config`, `scope`, ...); a `tools/call` returns what the command
  would print, and `initialize`'s `serverInfo.name` is `"hyera"` too. Any
  other `HYERA_MCP` value exits `2` with `unsupported MCP transport`. The
  trigger variable name itself is always `HYERA_MCP`, from that same
  `_parsername_`, regardless of `sys.argv[0]` (so `python -m hyera.cli` or
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
- **Merges follow Puppet exactly, including its quirks:** `unique` is
  first-found's higher-priority sibling — it flattens nested arrays and
  wraps a scalar into a one-element list, and only ever dedupes (`uniq`)
  when a SINGLE variant was found; a value found across multiple locations
  of a multi-location level (or multiple levels) is flattened and unioned
  but never separately deduped beyond that union, so duplicates survive
  when the only match came from one multi-location level. Dedup/union
  compare with Ruby `eql?`, not Python `==`: `1`, `1.0` and `True` are three
  distinct values. `hash` puts the lower-priority side's keys first, with
  higher-priority values winning per key. `deep` puts lower-priority array
  elements first (union, not concatenation) and dedupes the same way;
  `knockout_prefix` is a regular expression (spliced in unescaped, so a
  prefix like `.` or `x+` behaves as a pattern) that removes matching array
  elements and blanks matching strings during each merge step — it never
  removes a hash key, only a same-named *value*. A value found only once
  (no second location/level to merge against) is never asserted against
  the strategy's type rules and is returned exactly as found, markers and
  order included. `sort_merged_arrays` raises `MergeError` when Ruby's
  `<=>` cannot order two elements of an array it actually merged (nil vs. a
  number, a Boolean vs. anything, mismatched numeric/string types).
  Invalid `merge=` input (an unknown strategy name, a strategy hash with no
  `strategy` key, an unrecognized or mistyped option) raises
  `hyera.MergeError`, never a bare `ValueError`/`TypeError`.
- A missing bare `%{var}` interpolation resolves to `""` (matches Ruby
  Hiera); a missing function-call argument raises `InterpolationError`
  instead — the two failure modes are not symmetric.
- A `lookup_options` key is a **regex only when it starts with `^`**
  (Hiera 5's rule); anything else is matched literally, so a key containing
  `.` cannot shadow-match unrelated keys. An exact key match wins over a
  pattern; an invalid pattern is skipped rather than raising.
- A **dotted reference** (`%{trusted.certname}`, `%{facts.os.family}`) and a
  **dotted lookup key** (`h.get("a.b.0")`) both follow Puppet's own
  `split_key`/`sub_lookup` sub-key grammar, in hierarchy paths, `datadir`,
  `mapped_paths` templates, values, `.format()`, `%{scope('a.b')}` and
  `.get()` alike: a segment may be single- or double-quoted (quotes keep any
  embedded `.` literal and are trimmed off; whitespace around an unquoted
  segment or the dots themselves is trimmed too, e.g. `%{ a . b }`), and a
  segment made only of optionally-signed digits indexes a list (out of
  range, or a negative index, is a miss, never Python's wraparound).
  A Puppet variable name cannot contain `.`, so there is **no flat-key
  fallback**: `%{a.b}` always means "navigate `.b` into the value of `a`",
  never a literal scope/data key named `"a.b"` — quote the whole
  reference (`%{'a.b'}`, `h.get("'a.b'")`) to reach that key instead.
  A malformed key (an empty/unbalanced quoted segment, a stray leading,
  trailing or doubled `.`) raises `HieraLookupError` with Puppet's "Syntax
  error in key/string" text, and so does navigating into (or with) the
  wrong type — a non-collection value with a further segment, or a
  non-string/non-`Integer[...]`-shaped root variable name (that one raises
  `InterpolationError` instead) — **even when a `default=` was given**, or
  through `.has()`: only a genuine miss (an absent key, a `None` value
  walked no further, or an out-of-range/nonexistent segment) is silent.
  In a hierarchy path specifically, a *malformed* reference still just
  skips the level rather than raising (hierarchy paths do not follow
  Puppet's location rules yet) — but a well-formed one that hits a type
  mismatch raises there too, same as in a value.
- References resolve against the bound `Scope`, so a value or path
  reference means the same thing everywhere: `%{environment}`/
  `%{trusted...}` are always defined (`Scope`'s own defaults, never a
  level skip); a defined `False`/`0`/`""`/`[]`/`{}` variable or fact is
  never dropped (kept, unlike a genuinely unbound one); a bare embedded
  `False`/`True` renders `false`/`true` and `None` renders `` (the empty
  string) — a *standalone* `%{scope(...)}`/`%{hiera(...)}`/`%{lookup(...)}`
  call still stringifies a scalar result the same way (only `%{alias(...)}`
  preserves a non-scalar result's native type when it is the entire value).
- `HOCONBackend`'s `include` handling matches Puppet's own `hocon_data`
  by default (2026-09-29 — see the API section above): `include
  file(...)` really reads the named file (cwd-relative or absolute), and
  a directive in value position (`msg = please include "x"`) is kept as
  literal text, both confirmed against the real oracle (Puppet 8.10.0 /
  Ruby hocon 1.4.0, WSL). The stricter pre-fidelity behaviour (raise on both)
  is kept as the `hocon_includes=False` opt-in. One measured, accepted
  divergence: `include file("*.conf")` globs under pyhocon's own
  resolution where Puppet's never does (see the API section).
  `hocon_include_parity` closed the two findings this reversed
  (`hocon-data-include-file-and-value-position-includes-raise-pu`,
  `hocon-array-value-position-include-raises-vs-puppet-literal`) and
  found, in the same pass, that the fail-closed pyhocon-include guard had
  never actually wrapped the module `HOCONBackend.loads` parses through
  (`_hocon_parser()`'s private copy has its own, distinct
  `ConfigFactory`/`ConfigParser` classes — `is not` the shared module's) —
  a gap dating to `json_hocon_loaders`'s private-copy fix for duration
  parsing, now closed by installing the guard on that copy too.
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
  neither is a valid Puppet lookup *value*, and `hyera` does not reject
  them yet (Puppet's RichData value check is not ported yet); a value keyed
  or shaped this way currently returns successfully instead of erroring
  like Puppet.
- **`None`/`null`/`~` as an actual data value is indistinguishable from "key
  not found"** in the engine's own navigation (`Hiera._get_key` treats
  `cache is None` as "keep looking") — a known limitation, not yet
  fixed; a data file legally
  containing `key: ~` currently makes that key un-lookupable.
- **`convert_to` is Puppet's `new()`.** A `str` first element of the
  `convert_to` spec is parsed as a Puppet type expression first; a parse
  failure raises `HieraLookupError("Invalid data type in lookup_options for
  key '<key>' could not parse '<source>', error: '<msg>")` (Puppet's own
  format string, with its unbalanced closing quote, verbatim). A parseable
  type whose conversion or result-type assertion fails raises
  `HieraLookupError("The convert_to lookup_option for key '<key>' raised
  error: <msg>")` instead — both with the underlying error chained as
  `__cause__`. Converting to SemVer, SemVerRange, Timespan, Timestamp,
  Regexp, Binary, URI, Type or Object always raises the second form with
  "hiera does not support new() for the Puppet type '<T>'" — these are
  types whose values are not plain data, so this subset never implements
  `new()` for them (a deliberate deviation; Puppet itself supports several).
