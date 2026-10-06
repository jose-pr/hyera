# `hyera` — public API header

Header-file-style reference for the `hyera` package: every `__all__` export
with its exact signature, arguments, contract, and gotchas, so this module
can be consumed without reading its source. Kept current with the public
API by tests. For the project overview, see the shipped `README.md`, or
<https://github.com/jose-pr/hyera>.

Install as `hyera` (`pip install hyera`); extras: `pip install "hyera[cli]"`
(the console script, `duho`), `pip install "hyera[hocon]"` (`HOCONBackend`,
`pyhocon`), `pip install "hyera[eyaml]"` (`EyamlBackend`, `cryptography`).
The `dev`/`docs` extras are contributor-only tooling extras, not needed
to use the library. Import as `import hyera`; import every public name from
`hyera` itself, or from the public modules `hyera.types`, `hyera.backends`
and `hyera.cli` — `hyera._*` modules are private engine internals with no
stability contract. The console script is
`hyera` (`[project.scripts]`); `python -m hyera` runs the same entry point.
`hyera.__version__` (a plain `__version__` string) is the package version. Fully typed (`py.typed`;
`pyright --verifytypes hyera` scores 100%). Every lookup-shaped call
(`lookup`/`__call__`/`__getitem__`/`dig`/`get`/`getvar`) returns `Any`,
since Hiera data is dynamic.

## Lookup

- **`Hiera(base_config, backends=None, base_path=None, *, scope=None, environmentpath=None, basemodulepath=(), modulepath=None, cache_size=256, revalidate=True, codedir=None)`**
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
  `cache_size`: the bound on each scope-keyed cache (resolved hierarchy
  locations, merged `lookup_options`) — least-recently-used entries dropped
  once a new one would exceed it; `None` never evicts; `0` disables caching
  outright (every lookup rebuilds). Must be an `int` (not a `bool`) or
  `None`, else `TypeError("cache_size must be an int or None, not
  <type>")`; a negative value raises `ValueError("cache_size must be >=
  0")`. Every layer built inside one `Hiera(...)` call shares this bound.
  `revalidate`: whether every lookup re-checks the data files and glob
  listings it uses (Puppet's own re-read-between-compilations, for a
  library with no compilation of its own) — a changed file is re-read, a
  vanished one reads as missing, a directory added to or removed from a
  glob-matched directory is seen by the next lookup; `False` keeps every
  file and listing as first read until `clear_cache()`. Must be a `bool`,
  else `TypeError("revalidate must be a bool, not <type>")`. Every layer
  built inside one `Hiera(...)` call shares it too. `codedir`: Puppet's
  `$codedir`, consulted only by a version 3 hiera.yaml's default per-backend
  datadir (`<codedir>/environments/%{::environment}/hieradata`); `None`
  (the default) means Puppet's own AIO default for the platform
  (`%ALLUSERSPROFILE%\PuppetLabs\code` on Windows, `/etc/puppetlabs/code`
  elsewhere) — never the per-user `~/.puppetlabs/etc/code` default, and
  never discovered from `puppet.conf`.
  `repr(h)` is one line, `Hiera(config='<label>', environment='<name>')`:
  the config's absolute path (or `<dict>`, `<default>`, a stream's name or
  `<stream>`) and the scope's environment, never data or scope values.
  `self.scope` is set before the config loads, so a hierarchy path template
  referencing it (`%{trusted.certname}`, `%{environment}`) resolves against
  it from the first lookup onward. A missing or `null`/`false`
  `defaults`/`hierarchy` is filled with Puppet's own defaults
  (`{datadir: data, data_hash: yaml_data}` / `[{name: Common, path:
  common.yaml}]`) rather than raising; a hierarchy entry's own `datadir`
  wins, else `defaults.datadir`, else the literal `data`, always resolved
  next to hiera.yaml (or under `base_path`) — never the Hiera 3 absolute
  `/etc/puppetlabs/...` path. `default_hierarchy` is accepted only in a
  *module*'s own hiera.yaml (see "Layers" below); the same key in the
  global or an environment config raises `ConfigError`
  ("'default_hierarchy' is only allowed in the module layer"), at
  validation time. Where it is accepted, its entries are schema-validated
  exactly like `hierarchy`'s. Each entry uses its own function
  key (`data_hash`/`lookup_key`/`data_dig`/`hiera3_backend`/
  `v4_data_hash`), falling back to `defaults` only when the entry names
  none (`defaults` is never merged into an entry wholesale). A registered
  `lookup_key`/`data_dig` function is called per key and per location
  through a `hyera.LookupContext` (see Backends below); naming a function
  that does not implement the requested kind raises `ConfigError` with
  Puppet's own arity/parameter-type text, and an unregistered name raises
  "Unable to find ... function named ..." the same way: on the first call
  for a location that exists, never while the config is read. A function
  name matches without regard to case and a leading `::` is dropped. A hierarchy entry with no location key at all calls
  its function once, with no location, instead of contributing nothing.
  Raises `ConfigError` for a missing, unreadable, or invalid `hiera.yaml` —
  a directory, unparsable, an unsupported `version` (an explicit `1`, `2`,
  `6` or other non-3/4/5 value as "This runtime does not support hiera.yaml
  version N"; `4` at the global layer as "cannot be used in the global
  layer"), or any violation of Puppet's schema for the resolved version —
  with a message of hyera's own and, where known, `.path` and `.line`. Data
  files are read by lookups: a malformed or non-hash data file raises
  `BackendError` (`.path` names it) from the first lookup whose scope
  reaches it. Every lookup reads all data files of its scope first, to
  gather `lookup_options`, so looking up any key validates them.
  - **Version 3** (Hiera 1, 2 and 3: a file without `version`, and an
    explicit `version: 3`, are the same dialect to Puppet) is read and
    validated against Puppet's own v3 schema (`backends`, `logger`,
    `merge_behavior`, `deep_merge_options`, `hierarchy`, plus one config
    key per listed backend), reporting **every** mismatch — not just the
    first, unlike the version 5 schema below — each as its own "The Lookup
    Configuration ... has wrong type, ..." line, newline-joined into one
    `ConfigError`. A hiera.yaml that exists but does not parse to a YAML
    hash at all (empty, a list, ...) logs Puppet's own warning and falls
    back to Puppet's Hiera 3 default configuration
    (`backends: [yaml]`, `hierarchy: ['nodes/%{::trusted.certname}',
    'common']`, `merge_behavior: native`), then reads *that* as version 3.
    A schema-valid version 3 config resolves through the real
    backend-major provider build: one data source **per listed backend
    name**, over the whole hierarchy, in `backends:` list order — not one
    source per hierarchy level, the v5 shape. `yaml`/`json`/`hocon` map to
    the same `*_data` functions a v5 config would name; `eyaml` maps to
    `eyaml_lookup_key`; any other name must be a third-party
    `hyera.Backend` registered under it in the `"v3"` registry namespace
    (`NAMES = {"v3": (...)}`, resolved only against `data_hash`), else
    `ConfigError` ("Hiera 3 backend '<name>' is not available") — Puppet,
    with real Hiera 3 installed, would instead skip that backend silently
    (the `v3-ruby-backend-unavailable` deviation, below). A relative
    per-backend `datadir` (default
    `<codedir>/environments/%{::environment}/hieradata`) resolves against
    the process's working directory *at construction*, not hiera.yaml's
    own directory (`Hiera(..., codedir=...)`/`hyera --codedir` set
    `$codedir`; Puppet's own AIO default per platform otherwise, never the
    per-user default or a `puppet.conf` lookup). The per-backend
    `:extension:` (default `.<backend>`, `.conf` for hocon) is appended to
    each declared `path`/`paths` entry, after interpolation, unless it
    already ends with it. `merge_behavior`/`deep_merge_options`/`logger`
    are validated but never applied — only an explicit `merge=` changes
    anything, matching Puppet. A `hiera3_backend` entry in a v5 hierarchy
    (global layer only) follows the same backend-name rule, with the
    entry's declared extension stripped and the backend's own re-appended
    (so `path: common` and `path: common.<name>` both read the same file,
    Puppet's own Hiera-3-appends-again behavior). Every version 3 read
    (valid or not) logs Puppet's deprecation warning ("Use of 'hiera.yaml'
    version 3 is deprecated. It should be converted to version 5") unless
    `scope.strict == "off"`.
  - **Version 4** (`backend:` instead of `data_hash:`, `path`/`paths`
    defaulting to the entry's own `name`, one provider *per entry* — the v5
    shape, unlike v3's backend-major one) is accepted only in the
    environment and module layers; a global-layer version 4 config is still
    read and validated in full first (Puppet builds its provider list as
    part of construction), and only once that succeeds does construction
    raise `ConfigError` ("hiera.yaml version 4 cannot be used in the global
    layer") — a schema-invalid version 4 file at the global layer raises
    its schema error instead, never the layer one. `backend` accepts only
    `yaml`/`json`/`hocon` (mapped to the same `*_data` functions a v5
    config would name); any other name raises `ConfigError` ("No data
    provider is registered for backend '<name>'") — unlike v3, there is no
    third-party-backend fallback for v4. A relative `datadir` (default
    `data`, entry-level or config-level) resolves against the layer's own
    root, joined on literally with **no interpolation at all** (unlike
    every other version, whose `datadir` at least gets strict, method-free
    substitution) — a literal `%` in it is never mistaken for `%{...}`.
  - **Version 5** is the schema described above: an unrecognized key
    anywhere, a missing/duplicate/non-string `name`, more than one function
    or location key, a malformed `options` entry, and the like — reporting
    only the first mismatch.
  - **Layers.** `hiera.yaml` (`base_config`) is the *global* layer. Two more,
    optional, keyword-only layers sit alongside it, exactly as `puppet
    lookup` reads them: an *environment* layer, `<environmentpath>/
    <scope.environment>/hiera.yaml`, and a *module* layer,
    `<modulepath>/<module>/hiera.yaml`, consulted only for a key qualified
    `<module>::...`. `environmentpath`/`basemodulepath`/`modulepath` each
    take Puppet's setting shape: a single path, an iterable of paths, or a
    string of paths joined by `os.pathsep`; every entry is made absolute
    against the current working directory at construction (a relative
    `environmentpath="envs"` therefore depends on `os.getcwd()` at that
    moment, not later). `environmentpath=None` (the default) means **no**
    environment directories at all — every environment name then resolves
    with no environment layer and no error, a deliberate difference from
    Puppet (which always has an `environmentpath`); see "Differences from
    Puppet" below. `basemodulepath` defaults to `()`. `modulepath`,
    when given, *replaces* the whole modulepath (the environment's own
    `modules` directory included) for every environment, exactly like
    Puppet's `--modulepath`; `None` (the default) means Puppet's own
    per-environment construction, `<environment>/modules` (if the
    environment has a root) followed by `basemodulepath`.

    The environment is always `scope.environment` (default `"production"`)
    — there is no separate argument — so `.scoped(environment=...)` reads a
    different environment layer, sharing this instance's caches. With
    `environmentpath` set, an environment name other than `"production"`
    that is not found in any entry raises `ConfigError` ("Could not find a
    directory environment named '<name>' anywhere in the path: <path>. Does
    the directory exist?"); a missing `"production"` directory is not an
    error (Puppet's static default environment). A module is found by an
    exact, case-sensitive directory name (`os.listdir`, never a
    filesystem `.is_dir()` probe), so a `Mymod` directory never matches a
    `mymod::` key even on a case-insensitive filesystem (Windows/macOS). A
    key inside a module's data that is not itself qualified with that
    module's name is dropped, with a warning naming the module, the
    function and the location, once per module file this instance ever
    reads that way; an unqualified key at the top of a lookup (no `::`)
    never reaches the module layer at all. `hiera3_backend` is accepted
    only in the global layer's hiera.yaml; the same key at an environment
    or module root raises `ConfigError` ("'hiera3_backend' is only allowed
    in the global layer"), at load time. A version-3 (or missing-`version`)
    hiera.yaml at an environment or module root is not an error by itself:
    under `scope.strict="error"` it raises ("hiera.yaml version 3 cannot be
    used in an environment"/"...in a module"), otherwise it is silently
    ignored (with a once-per-file warning) and that layer contributes
    nothing — its schema is still fully validated first, though (a
    schema-invalid version 3 config at any layer raises, regardless of
    whether that layer would otherwise ignore a *valid* one). A version-4
    config outside the global layer is read normally (see "Version 4"
    above). `sources()` still reports only the global
    layer's candidate paths — a per-layer view belongs to `explain()`.
    A `%{lookup()}`/`%{hiera()}`/`%{alias()}` reached while interpolating a
    version 3 *global* layer's own data is confined to the global layer
    (never reaching an environment or module, even for an otherwise
    qualified key, and never a module's `default_hierarchy`) unless the
    current environment has a real version 5 hiera.yaml — an absent,
    ignored-version-3, or version 4 environment all count as none.
    A module's own hiera.yaml may declare `default_hierarchy`, consulted
    only for that module's own `<module>::...` keys, and only after the
    global, environment and module main hierarchies all miss (including a
    hit whose dotted dig misses). The caller's `merge=` never applies
    there — the merge comes only from the default hierarchy's own
    `lookup_options`, never merged with the main hierarchy's — while the
    main hierarchy's `convert_to` still applies to the result either way.
    The same key in the global or an environment config raises
    `ConfigError` at validation time, before any lookup runs.
    To look up a single module's own data with no global or environment
    config at all: `Hiera({"version": 5, "hierarchy": []},
    modulepath=["/path/to/modules"])`.
  - **`.lookup(name, value_type=None, merge=None, default_value=<unset>, *, default_values_hash=None, override=None, block=None)`**
    — Puppet's `lookup()`, against the instance's bound scope. Five
    equivalent call forms: `lookup("k")`; `lookup("k", "Integer")`;
    `lookup("k", "Integer", "first", 0)` (every positional argument);
    `lookup({"name": "k", "merge": "first"})` (a single dict in place of
    everything else — `"name"` is required, and every other positional
    argument/option keyword must be omitted); `lookup("k", {"merge":
    "first"})` (name positional, every other option in a dict passed as
    `value_type`). Every option name also works as a keyword (`lookup("k",
    merge="first")`); combining an options dict (either form) with another
    positional argument or an option keyword raises `TypeError` — `block`
    is the one exception, accepted alongside either dict form. `name`: a
    `str`, or a `list` of `str`/tuple-path (below) tried in order. A
    non-empty `tuple` is an exact key path, not parsed at all: element 0
    is the root key, every later element a dig segment (`str` a hash key,
    `int` an array index, never `bool`), each taken verbatim — no dot
    splitting, no quote syntax, no whitespace stripping.
    `h.lookup(("a.b", "c", 0))` resolves exactly as `h.lookup('"a.b".c.0')`
    does — same root key, same `lookup_options`, same merge, same
    sub-lookup errors — including a path a quoted string cannot spell (a
    segment holding both quote kinds); its `KeyNotFoundError`/explain/debug
    text renders the same dotted form, and `override`/`default_values_hash`
    match it through that form too (`("hsi", "a")` finds the entry
    `"hsi.a"`). `h["a", "b"]` still means
    `(name, value_type)`, unaffected: a tuple *subscript* keeps unpacking
    into positional arguments (below), so a path there is
    `h[("a.b", "c"),]`. `value_type`: a Puppet type expression string
    (`"Integer"`, `"Optional[String]"`; see "Types" below); every candidate
    value (an override, a found value, a default) is asserted against it,
    raising `HieraLookupError` on a mismatch with Puppet's own subject text
    ("Found value has wrong type, …", "Default value has wrong type, …",
    "Value found for key '<k>' in override hash has wrong type, …", "… in
    default values hash has wrong type, …", "Value returned from default
    block has wrong type, …"). `merge`: a `hyera.MergeSpec` (see "Types"
    below) — a `hyera.Merge` member (`FIRST`/`UNIQUE`/`HASH`/`DEEP`) or the
    same plain string (`Merge.DEEP == "deep"`) — an explicit `merge=`
    overrides only the merge `lookup_options` would have picked; an
    applicable `convert_to` still runs. `default_value`:
    returned (after `value_type`) when nothing else was found; **omitted
    entirely** means no default at all — passing `None` explicitly is a
    real default that beats a miss (and a found `None` beats even that).
    `default_values_hash`: a dict tried, per name, only after the whole
    hierarchy missed every name. `override`: a dict consulted, per name,
    *before* the hierarchy — a hit here returns immediately, `convert_to`
    included, never touching the hierarchy at all; both `override` and
    `default_values_hash` also feed `%{var}` interpolation inside any value
    looked up during the same call (never a hierarchy location, which
    interpolates against the scope alone). `block`: called with `name`
    exactly as given (a list stays a list) when nothing else was found,
    before `default_value`; its return value is asserted against
    `value_type` too. Precedence, per name in order: `override` → the
    hierarchy (`lookup_options`, `default_hierarchy`, `convert_to` all
    apply) → (next name) → `default_values_hash` (every name again) →
    `block` → `default_value` → `KeyNotFoundError` (also a `KeyError`),
    naming every name that was tried ("… for the name 'x'" for one, "… for
    any of the names [...]" otherwise, including an empty list). A
    non-`str`/non-`list`/non-`tuple` `name`, a malformed tuple path (empty,
    a non-`str` root, or a `bool`/other element past the root), an
    unparsable `value_type`'s call shape, an empty-string `merge`, a
    non-callable `block`, or an unknown/malformed option raises
    `TypeError`.
  - Also reachable as `h(...)` (`.__call__(name, value_type=None, merge=None, default_value=<unset>, *, default_values_hash=None, override=None, block=None)`,
    identical to `.lookup(...)`) and `h[...]` (`.__getitem__(item)`: a
    `tuple` unpacks into `.lookup(*item)`, anything else becomes the sole
    `name` argument — so `h["a", "b"]` is `lookup("a", "b")`, not a
    two-name list). `name in h` (`.__contains__(name)`) is `True` unless
    `.lookup(name)` raises `KeyNotFoundError` — any other error (a
    malformed key, a type mismatch) propagates, same as `.lookup()`.
    `iter(h)` raises `TypeError`: a `Hiera` is not a sequence, even though
    it defines `__getitem__`.
  - **`.dig(*keys, value_type=None, merge=None, default_values_hash=None, override=None)`**
    — Puppet's `dig()`: looks up `keys[0]` via `.lookup()` — it is a lookup
    key, parsed as one (`dig("h.x", "p")` looks up `h.x`), while every later
    key is used exactly as given
    (`merge`/`default_values_hash`/`override` apply to that root lookup,
    exactly as they would to `.lookup()` itself), then digs the rest of
    `keys` out of it Ruby `Hash#dig`/`Array#dig`-style. A miss on
    `keys[0]` gives `None` — unlike `.lookup()`, `.dig()` never raises
    `KeyNotFoundError`. A later key that is not an `int` against a `list`,
    or any key against a value that is not a collection, raises
    `HieraLookupError` naming the path walked so far and the Puppet type
    found ("The given data requires an Integer index at […], got '…'" /
    "The given data does not contain a Collection at […], got '…'"). A
    `list` index follows Ruby's negative/out-of-range rules (negative
    counts from the end; out of range is `None`); a `dict` key matches
    only a key of the identical kind (`True` is never `1`, `1` is never
    `1.0`). `value_type`, when given, asserts the final result with the
    subject "Found value" and is checked before the lookup runs. Needs at
    least one key, the first a `str`, else `TypeError`.
  - **`.get(dotted, default_value=None, block=None, *, value_type=None, merge=None, default_values_hash=None, override=None)`**
    — Puppet's `get()`: `dotted` is a single dotted-navigation *string*
    (`"a.b.0"`). The root segment resolves like `.lookup()` (an `int` root
    can never match a hiera key, so it is treated as a miss without a
    lookup at all); a root miss or a found `None`, or any navigation past
    it landing on `None`, returns `default_value` — never raises for that
    reason. A walk error (the same two `HieraLookupError`s `.dig()` raises)
    reaches `block(error)` when a block is given, else raises. `dotted`
    must be a non-empty `str` (there is no whole-data value to return),
    else `HieraLookupError("Syntax error in dotted-navigation string")`,
    the same error a malformed one raises; a non-`str` `dotted`, a
    `block` that is not callable and a `value_type` that is not a type
    spec raise `TypeError` ("get(): block must be callable") before
    anything is resolved. `value_type` asserts the final result with the
    subject that says where it came from ("Found value", "Default value"
    or "Value returned from block").
  - **`.getvar(dotted, default_value=None, block=None)`** — Puppet's
    `getvar()`: `.get()`'s own navigation, over a scope variable's value
    instead of a looked-up one. `dotted` must start with a valid
    (optionally `::`-qualified) Puppet variable name immediately followed
    by `.` or the string's end, else `HieraLookupError("'getvar' The given
    string does not start with a valid variable name")`. An undefined
    variable returns `default_value` regardless of the bound scope's
    `strict` — never raises for that alone. A list/dict result is a copy,
    never the scope's own object. A non-`str` `dotted` or a `block` that
    is not callable raises `TypeError`.
  - **`.explain(name, value_type=None, merge=None, default_value=<unset>, *, default_values_hash=None, override=None, block=None, explain_options=False)`**
    — what `puppet lookup --explain`/`--explain-options` shows: takes
    exactly `.lookup()`'s own signature and dispatcher (the same five call
    forms, the same keyword spellings). `explain_options=True` mirrors
    `--explain-options`: only how `lookup_options` was assembled for `name`
    (and its own module, if qualified) is reported; combined with an
    otherwise-normal call it is Puppet's `--explain --explain-options`,
    byte-identical to `explain_options=False`. Always returns an
    `ExplainResult`:
    - **`ExplainResult(explainer, error=None)`** — never constructed by a
      caller (the engine builds one for every `.explain()` call).
      `.text() -> str` is the indented report (every hierarchy entry and
      path consulted, `Path not found`, `No such key`, `Found key`, merges
      and their results, interpolations and sub-keys, the `lookup_options`
      search, `default_hierarchy`; also `str(result)`); `.to_hash() ->
      dict` is the same tree, projected with Puppet's own keys (`branches`,
      `type`, `key`, `value`, `event`, `name`, `path`, `original_path`,
      ...) — a fresh `copy.deepcopy` on every call, so mutating the result
      never reaches a later `.explain()`/`.lookup()`. `.error ->
      Optional[HieraError]` is the error the lookup ended with, or `None`.
    An error `puppet lookup --explain` prints as its own last line
    is reported the same way here (`.error` set, `.text()` ends with its
    message) instead of raising: a miss (`KeyNotFoundError`), an invalid
    `lookup_options` value, a failed `convert_to`, an interpolation syntax
    error, a sub-key navigated into a non-hash, or a `HieraLookupError`
    raised from *environment or module* data (not the *global* layer's
    own). Everything else raises exactly as `.lookup()` does: a `--type`
    mismatch, a `HieraLookupError` left unhandled by the *global* layer's
    own data (Puppet's own boundary — the same recursive-lookup error is
    reported from environment data and escapes from global data), a
    `BackendError` (an unreadable or unparsable data file, from any
    layer), and `ConfigError` always (a `hiera.yaml` problem is read
    before the lookup starts, so there is never one to report mid-lookup,
    even where `puppet lookup --explain` prints some as its own last
    line). Explaining bypasses the instance's own `lookup_options` cache
    (a fresh, call-scoped one is used instead), so the `lookup_options`
    search always shows, even right after an ordinary `.lookup()` already
    cached everything; it never bypasses or corrupts any other cache, and
    changes nothing an ordinary `.lookup()`/`.sources()` afterwards sees.
  - **`.scoped(*, variables=None, facts=None, trusted=None, server_facts=None, environment=None, strict=None, node_name=None)`**
    — a *view*: builds a new `Hiera` (sharing this instance's config,
    backends and caches — already keyed on the scope value, so sharing
    them is safe) with `self.scope.derive(...)` bound in place of
    `self.scope`. Every method — `.lookup`/`()`/`[]`/`in`, `.sources()`,
    `.format()` — then reads the view's own scope. `.scoped(...)` layers:
    calling it again on a view derives from *that* view's scope, not the
    original instance's, so nested calls compose. The original instance's
    own scope, and any other existing view, are never affected.
  - **`.sources()`** — resolve+load the ordered candidate source paths for
    the bound scope (cached per scope value). Under `revalidate=True` a
    file or glob match added or removed since the last call is seen by the
    next one; with `revalidate=False` the listing is kept until
    `clear_cache()`.
  - **`.format(text)`** — interpolates `text` exactly as a data
    value is interpolated (Puppet's `Context#interpolate`): all five
    methods, whitespace inside `%{ }` ignored, literal braces untouched,
    undefined variables per the scope's `strict`. Returns a `str`, except
    that a `text` that is exactly one `%{alias('k')}` returns `k`'s value.
    Raises `TypeError` for a non-`str` `text`.
  - **`.clear_cache()`** — drops every cached location,
    `lookup_options` mapping, glob listing and parsed data file; the next
    lookup re-reads whatever it needs from disk. Safe to call while other
    threads are looking things up on this instance or a `.scoped(...)` view
    of it (they share every cache). There are no public cache attributes to
    inspect or clear individually.
  - Coming from `hiera()`/`hiera_array()`/`hiera_hash()`/`hiera_include()`
    (Puppet's legacy functions always force a merge, ignoring
    `lookup_options`): `hiera(k[, d])` → `h.lookup(k, None, "first"[, d])`;
    `hiera_array(k)` → `h.lookup(k, None, "unique")`; `hiera_hash(k)` →
    `h.lookup(k, None, "hash")`; `hiera_include(k)` has no equivalent (it
    applies classes to a catalog, which hyera has no notion of) — none of
    the four are implemented as methods; use `.lookup()` directly.
  - Gotcha: parsed data files are cached per `(path, backend.strict, options)`
    for the instance's life — a YAML file's own non-hash validation is
    `strict`-sensitive, so the same file can be cached independently under
    different `strict` values — and unbounded, like Puppet's own
    per-environment file cache: its size follows the data tree, not the
    number of scopes seen, and only `clear_cache()` empties it. Resolved
    hierarchy locations are cached per the values of the variables their
    own interpolation reads (as Puppet's `scope_interpolations_stable?` —
    for example `%{trusted.certname}`, `%{facts.os.family}`, or a
    `mapped_paths` collection), not per the whole scope: two scopes that
    differ only in an unreferenced fact or variable (a volatile timestamp,
    an unrelated top-scope value) share one cached entry, while `True`, `1`
    and `1.0` never do. The merged `lookup_options` mapping is cached per
    set of locations plus the variables its own interpolation reads, and
    never cached at all when that interpolation makes a sub-lookup
    (`%{lookup(...)}` inside a `merge:` spec, say) — a sub-lookup can reach
    data the location set alone does not account for. Each of the two
    scope-keyed caches is bounded by `cache_size` (least-recently-used
    entries dropped). With `revalidate=True` (the default) every lookup
    still re-checks: each candidate location is re-probed once, a data file
    whose inode, modification time or size changed is re-read, a location
    that starts or stops existing is seen, and a glob level is re-listed
    only when a directory it walked or tested a name in (absent ones
    included) has itself changed — one probe per
    candidate and one re-list per changed directory, never a full re-walk
    of every glob on every lookup. With `revalidate=False`, files and
    listings stay exactly as first read for a given set of referenced
    values until `clear_cache()` — no on-disk change is seen at all.
    Neither mode re-reads `hiera.yaml` itself; construct a new `Hiera` for
    that. A `.scoped(...)` view shares every cache with the instance it was
    derived from (and with every other view of the same instance), by
    design (see `.scoped` above) — never copy them expecting isolation.
  - Gotcha: a path-configured `Hiera` holds no open file, so the config file
    can be replaced or removed on disk while the instance lives (it keeps
    what it read at construction). `Hiera` (a `.scoped(...)` view included)
    survives `copy.deepcopy` and `pickle` (a spawn-start process pool can
    receive one; a relative config path stays relative to the receiving
    process's working directory): all derived state does NOT survive a
    copy (`copy.copy` included) — locations, `lookup_options`, parsed files,
    glob listings, module data, function providers with their kept results
    and `LookupContext` caches, and the lock they share. Each copy starts
    empty and independent of the original, so the next lookup re-reads every
    data file (re-decrypting sops plaintext along with it) and rebuilds
    whatever else it needs (`Scope`'s own warning-dedup state is NOT carried
    over verbatim either — its internal lock cannot be pickled, so a copy
    starts with the same dedup keys but a fresh, unlocked mutex).
    `clear_cache()` on an instance or on any of its views reaches all of
    them, function providers included.
    Concurrent `.lookup()` calls on one instance (or its views) from
    multiple threads are safe on GIL builds, where they only mutate the
    shared caches under one lock per instance (untested on free-threaded
    builds).
- **`hyera.FunctionKind`** (`DATA_HASH`, `LOOKUP_KEY`, `DATA_DIG`) — which
  Puppet Hiera 5 provider hook a level's backend implements; see
  `HieraLevel.kind`/`.new` below.
- **`HieraLevel`** (`NamedTuple`: `name`, `backend`, `datadir`,
  `location_key`, `locations`, `kind`, `options`, `extension`,
  `datadir_base`, `datadir_literal`, `lenient_locations`) — one hierarchy
  entry, stored exactly as written in hiera.yaml (`locations`/`options` are
  never interpolated or normalized here). `locations` is always a tuple, a
  one-element one for a singular key. Hashable, `options` included.
  - **`.new(conf, backend, kind='data_hash', *, extension=None, datadir_base=None, datadir_literal=False, lenient_locations=True)`**
    builds one from a hierarchy dict (`location_key` is the first of
    `path`/`paths`/`glob`/`globs`/`uri`/`uris`/`mapped_paths` present, or
    `None`; `locations` is that key's raw value(s) as a tuple — the one
    string of a singular key, the declared strings of a plural one, or
    `(collection_var, item_var, template)` for `mapped_paths`; `datadir`
    defaults to `"data"`; `kind` is a `FunctionKind`
    member (`DATA_HASH`/`LOOKUP_KEY`/`DATA_DIG`) or the same plain string;
    the built `HieraLevel.kind` field itself always reads back a plain
    `str`. `options` is the entry's
    own `options`, else `defaults`'s, uninterpolated; `extension` is a
    version-3-only appended suffix; `datadir_base`/`datadir_literal` are
    version-specific `datadir`-resolution flags — see "Version 3"/"Version
    4" above; `lenient_locations` is `False` for a version 3/4 level, whose
    undefined variable in a location fails under `strict="error"`).
  - **`.paths(base_path, scope) -> list[str]`** resolves candidate source
    *file* paths (plain `str`, with the host's separator) for a bound
    `Scope`, through the same `%{...}` engine as
    data values (`allow_methods=False`): an undefined variable interpolates
    as `''` plus the scope's `strict`-mode warning and the resulting path is
    still probed, **never** a skipped level; `datadir` interpolates
    separately, under the scope's `strict` (raises under `"error"`, unlike a
    location itself, which is lenient only at version 5); method-call syntax
    (`%{lookup(...)}` etc.) raises `ConfigError` in any of these positions.
    A location-less entry, or one using `uri`/`uris`, contributes no paths
    here (`[]`) — a `uri` is never a filesystem path.
  A `mapped_paths` collection is a scope reference (dotted, `::`-qualified)
  — `None`/`""`/an empty Array/Hash contributes no paths, a `String`
  becomes a one-element list, an Array is used as-is, a Hash contributes
  its `[key, value]` pairs; a Boolean/Integer/Float collection raises
  `ConfigError`. Each item binds as one local-scope variable layer
  (`%{item}` reads it; `%{::item}` still reaches a top-scope variable of
  the same name, bypassing the local layer). A `path`/`paths`/mapped
  location that names a directory raises `BackendError` ("Is a directory")
  when loaded, instead of reading its files; a glob match that is a
  directory is dropped instead. A `glob`/`globs` location matches through
  hyera's own Ruby `Dir.glob` port, never `pathlib_next.Path.glob`: `{a,b}`
  brace alternation (nested, in written order, duplicates kept); a
  dotfile matches only an explicit leading `.` in the pattern, never a
  bare `*`/`?`/`[...]`; `**/` never descends through a symlink or a
  Windows junction, and a trailing `**` is plain `*`; `\` escapes a
  metacharacter in the pattern text; each directory's entries sort in
  byte order and every wildcard is case-sensitive, on every OS, while a literal
  segment is an existence check that follows the filesystem's case rule (spelled as
  the pattern spells it); a missing
  or unreadable directory contributes nothing; `datadir`'s own glob
  metacharacters are live for a glob level (a literal directory for a
  `path`/`paths`/mapped one).

## Scope and facts

Puppet's top scope, as one immutable, hashable value, bound to every
`Hiera` instance, views included (`Hiera(..., scope=...)`, `.scope`,
`.scoped(...)`). Logger `hyera._scope.scope`.

- **`hyera.Strict`** (`OFF`, `WARNING`, `ERROR`) — strictness for an
  undefined variable; every `strict=` argument below takes a `Strict`
  member or the same plain string.

- **`Scope(*, variables=None, facts=None, trusted=None, server_facts=None, environment=None, strict='warning', node_name=None)`**
  — every argument keyword-only. `variables`/`facts`/`server_facts`/`trusted`
  are each `None` or a mapping with `str` keys; `variables`/`server_facts`/
  `trusted` values must additionally be Puppet Data (`None`, `bool`, `int`,
  `float`, `str`, a list/tuple — stored as a list — or a `str`-keyed dict of
  Data, recursively) or construction raises `TypeError("Unsupported data
  type: '<type name>'")`; every input is deep-copied. `strict` accepts a
  `hyera.Strict` member (`OFF`/`WARNING`/`ERROR`) or the same plain string
  (`Strict.ERROR == "error"`; same for `.derive(strict=...)` and
  `Hiera.scoped(strict=...)`) — anything else raises `ValueError`.
  `.strict` (read-only property) always reads back a plain `str`.
  `environment` must
  be `None` or a non-empty `str`; `node_name` must be `None` or a `str` —
  otherwise `TypeError`/`ValueError`.

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
  - **`.lookup(name)`** — no strict side effects (Puppet's
    `catch(:undefined_variable)` form); returns the bound value (even
    `None`), or `Scope.UNDEFINED`. A leading `::` is stripped; a remaining
    `::` marks the name qualified, but the local layers and top table are
    still checked for a literal match either way (there are no class
    scopes, so a qualified name is undefined unless a variable is
    literally named that way). Only for an unqualified miss:
    `"caller_module_name"` returns `None`, and a bare non-negative integer
    name (`^(?:0|[1-9][0-9]*)$`) returns `None`; anything else unmatched is
    `Scope.UNDEFINED`. A non-`str` `name` raises `TypeError`.
  - **`.exist(name)`** — `True` iff bound in a local layer or the
    top table, or `name == "caller_module_name"`; a still-qualified name
    (after stripping one leading `::`) or a numeric name is always `False`.
  - **`.lookupvar(name, *, lenient=False)`** — `.lookup(name)`,
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
  - **`.with_local_scope(variables)`** — a child sharing this
    scope's table and warning state, adding one local variable layer
    (`variables`'s keys/values checked the same way as the constructor's
    `variables`). This scope is unchanged; layers are functional, not
    push/pop, since one `Hiera` can serve many concurrent lookups.
  - **`.derive(*, variables=None, facts=None, trusted=None, server_facts=None, environment=None, strict=None, node_name=None)`**
    — a new root scope, fully rebuilt from this scope's own
    constructor inputs: `variables`/`facts`/`server_facts` shallow-update
    the parent's (new values win, so an unrelated fact never goes stale);
    the rest replace the parent's when given (`environment`/`strict`
    default to *this scope's already-resolved* value, not to their own
    defaults). Local layers are not carried; the warning state is shared.
  - **`Scope.UNDEFINED`** — the lookup-miss sentinel (the same object as
    the `<unset>` sentinel documented under "Types" below).
  - **Value semantics** — immutable and hashable; `==`/`hash` compare a
    type-tagged rendering of the top table, local layers, `strict` and
    `node_name` (so `True`, `1` and `1.0` are distinct, unlike plain Python
    equality). Do not mutate a value returned by `.lookup`/`.lookupvar`: it
    is this scope's own copy, shared by every caller. `repr()` shows
    `environment`, `strict` and variable/fact counts, never values.

Two sources for `Scope(facts=...)`: a Puppet-compatible `--facts` file, and
bare `facter`. Neither sanitizes its result — pass it to `Scope`, which does.

- **`load_facts(path)`** — `path` is a `str` or `os.PathLike`; every
  message uses `str(path)` as given (`puppet lookup --facts` rules,
  `application/lookup.rb:349-371`). The parser is chosen by extension:
  `.json` → JSON; `.yml`/`.yaml` → YAML; anything else tries JSON, then
  YAML, and a failure of *either* there just means "no result" (not an
  error). For a `.json`/`.yml`/`.yaml` file specifically, a parse failure
  **does** raise. JSON is parsed with Ruby `json`-gem rules: UTF-8 only (a
  BOM fails), `NaN`/`Infinity`/`-Infinity` rejected. YAML goes through
  hyera's own Psych-compatible loader (decoded `utf-8-sig` first, so a BOM
  is stripped, unlike a hiera.yaml/data file) — but
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
- **`facts_from_facter(*, timeout=30)`** — runs a bare
  `facter -j` (no queries, no `--show-legacy`: a queried `facter -j a b`
  returns flat dotted keys `$facts` cannot navigate) and returns its JSON
  output as a `dict`. Does **not** add `clientcert`/`clientversion`/
  `clientnoop` — those come from Puppet's agent, not facter; pass
  `clientcert` yourself (e.g. via `Scope(variables={"clientcert": ...})`)
  if `$trusted.certname` should be set. Runs through the same private
  runner as `SopsBackend`: `timeout` (default 30s) bounds it, and on expiry
  facter and its child processes are killed and `BackendTimeoutError` is
  raised; its stdin is the null device. A missing `facter` binary, a
  `facter` that `PATH` resolves relative to the current directory, a
  non-zero exit (the last 2,000 characters of stderr), or output that isn't
  a JSON object raise `BackendError` (a `.bat` facter at an absolute path
  is allowed). `__context__` never carries the (possibly partial) output.

## Types

- **`MergeSpec`** — the type of every public `merge=` argument: a
  `hyera.Merge` member or strategy name (`"first"`/`"unique"`/`"hash"`/
  `"deep"`/`"default"`/`"reverse_deep"`/`"unconstrained_deep"` — the last
  three are real strategies but have no `Merge` member, since Puppet itself
  never exposes them as a choice), a `{"strategy": ..., ...}`
  mapping with Puppet's deep-merge options (`knockout_prefix`,
  `sort_merged_arrays`, `merge_hash_arrays`, and, for the two extra Hiera-3
  deep variants, `keep_array_duplicates`, `overwrite_arrays`,
  `unpack_arrays`, `extend_existing_arrays`, `merge_nil_values`,
  `preserve_unmergeables`), or `None` for the level's own `lookup_options`
  default (an unrecognized name/shape raises `hyera.MergeError`). See
  "Merges" under Gotchas for the exact semantics of each strategy.
- **`hyera.Merge`** (`FIRST`, `UNIQUE`, `HASH`, `DEEP`) — Puppet's own four
  public strategy names (`MergeStrategy.strategy_keys()`), as a `str`-mixin
  `Enum`: `Merge.DEEP == "deep"`, `str(Merge.DEEP) == "deep"`, and every
  `merge=` argument above takes a member exactly as it takes the string.
- **`Sensitive(value)`** — redacting wrapper produced by `convert_to:
  Sensitive`, mirroring Puppet's `Sensitive` type (`p_sensitive_type.rb`).
  `str()`/`repr()` both show `Sensitive [value redacted]`; `.unwrap()`
  returns the real value. Equality and hashing follow Puppet: two
  `Sensitive` values are equal (and hash equal) exactly when their wrapped
  values are Ruby-`eql?` — `1`, `1.0` and `True` are distinct wrapped
  values, but a list or dict payload compares/hashes by content (in any key
  order for a dict) despite being unhashable in plain Python.
  `Sensitive[T]` (the Puppet *type*, as opposed to calling `Sensitive(x)`
  for a value) is documented under "`hyera.types`" below.
- **`value_type`/`convert_to` type expressions** — a Puppet type
  expression string (`"Integer"`, `"Array[String]"`,
  `"Optional[Integer[0,10]]"`), or (`value_type` only — `convert_to` always
  comes from data, so stays string-only) the equivalent object from
  `hyera.types`, below, parsed against a subset of Puppet's type
  system: full semantics for `Any`/`Data`/`Undef`/`Boolean`/`Integer`/
  `Float`/`Numeric`/`String`/`Enum`/`Pattern`/`Array`/`Hash`/`Tuple`/
  `Struct`/`Optional`/`NotUndef`/`Variant`/`Scalar`/`Collection`; named-only
  (accepted in a type expression, matched by class, but not a `convert_to`
  target) for the rest of Puppet's built-ins. `convert_to` (Puppet's
  `new()`) is implemented only for types whose values are plain data
  (`Integer`/`Float`/`Numeric`/`String`/`Boolean`/`Array`/`Hash`/
  `Sensitive`/`Tuple`/`Struct`/`Optional`/`NotUndef`); converting to
  `SemVer`, `SemVerRange`, `Timespan`, `Timestamp`, `Regexp`, `Binary`,
  `URI`, `Type` or `Object` always raises `HieraLookupError("hyera does not
  support new() for the Puppet type '<T>'")` — the
  `convert-to-unsupported-type` deviation, below. See "convert_to" under
  Gotchas for the two message forms a failed conversion raises.
- **The `<unset>` sentinel** — `Hiera.lookup`'s `default_value` (and
  `.explain`'s) defaults to a private sentinel object that renders as
  `<unset>` in `help()`/`inspect.signature()` output and keeps its identity
  through `copy`/`pickle`; it is never itself returned from a lookup (it
  only marks "no default was given"). The same object is `Scope.UNDEFINED`.

## `hyera.types`

Public Puppet type objects, one class per Puppet type name: `Any`, `Undef`,
`NotUndef`, `Optional`, `Scalar`, `ScalarData`, `Numeric`, `Integer`,
`Float`, `String`, `Boolean`, `Regexp`, `Pattern`, `Enum`, `Collection`,
`Array`, `Hash`, `Tuple`, `Struct`, `Variant`, `Data`, `RichData`, plus
`Sensitive` (the same object as `hyera.Sensitive`). Not re-exported from
top-level `hyera` (`hyera.types.Integer`, not `hyera.Integer`) except
`Sensitive`, already public there. Every name means one of three things,
by how it is used:

- **Bare** (`Integer`) — the unparameterized type: `isinstance(5,
  Integer)` is `True`.
- **Subscripted** (`Integer[1, 10]`, `Optional[String]`, `Struct[{"a":
  Integer}]`) — a parameterized type, built exactly as
  `value_type`/`convert_to` text would parse the equivalent Puppet
  expression (`Integer[1, 10] == <value_type string "Integer[1, 10]">`,
  including `str()`/`repr()` and every error message). A nested argument is
  itself a type (another of these classes, a type object, or a Puppet
  type-expression `str`, e.g. `Array["Integer"]`) for `Array`/`Hash`/
  `Tuple`/`Variant`/`Sensitive`'s element/key/value/branch/contained
  positions; a literal `str`/`bool`/`int`/`float`/`None` for a
  value-position argument (`Integer`/`Float`/`String`/`Collection`'s size
  or range bounds, `Boolean`'s fixed value, `Enum`'s members, `Pattern`'s
  regex sources alongside a compiled `re.Pattern`, and `Optional`/
  `NotUndef`'s own contained argument when it is a plain `str`, which stays
  a literal match the way Puppet's own grammar treats a bareword/quoted
  string there). `Struct[{...}]` takes exactly one `dict`; a key is a plain
  `str` (optional exactly when its value type accepts undef, as in Puppet),
  `Optional["k"]` (always optional) or `NotUndef["k"]` (always required),
  the last two built from this same module and used directly as the dict
  key. A `Tuple`'s trailing size arguments are a minimum (`Tuple[String,
  1]` is one or more) or a minimum and maximum, and its last type repeats;
  a bare `Tuple` is any array. `Enum`'s trailing `True` makes it
  case-insensitive; bare `Enum` and bare `Pattern` accept any `str`; bare
  `Optional` accepts only `None`; `ScalarData` is the four scalars only.
  A `Pattern`/
  `Regexp` source containing a literal `/` has no escape that survives the
  text round-trip these classes build on; pass the pre-built type object
  instead (not reachable from this module) if that ever matters.
  `Sensitive[T]` (a type) is defined directly on `hyera.Sensitive` itself,
  the same object as this module's own `Sensitive`; like Puppet's, it keeps
  only `T`'s generalized type (`Sensitive[Integer[1, 3]]` is
  `Sensitive[Integer]`). Subscripting builds through the same builders as the
  text form, so a nested argument keeps every parameter
  (`Optional[Integer[1, 3]]` renders and matches as written). A type object
  is immutable: assigning to one of its attributes raises
  `AttributeError`, so a cached or shared object cannot be altered through
  any reference; it compares equal to other type objects of the same value
  and unequal to anything else.
- **Called** (`Integer("42")`) — Puppet's `new()`, returning a plain value
  (`int` for `Integer`, `str` for `String`, `list` for `Array`, `dict` for
  `Hash`, ... — `Sensitive("x")` is the one exception, staying its existing
  value wrapper, unaffected by anything here), never an instance of the
  class itself; a type with no Puppet `new()` raises the same
  `HieraLookupError` `convert_to` already does.

Every bare or subscripted form answers `isinstance` the Puppet way
(`isinstance(5, Integer[1, 10])`, `isinstance(None, Optional[String])`,
`isinstance(Sensitive("x"), Sensitive[String])`) without subclassing a
builtin (`bool`/`NoneType` cannot be subclassed, and a builtin subclass
breaks `yaml.safe_dump`): every class here answers `isinstance` through its
own type object's `instance()`, never through actual subclassing, and this
module's own metaclass is never the type of a type object -- internal code
keeps dispatching on the private classes in `hyera._types.types` directly,
unaffected by anything here. A type checker sees the exact builtin for a
call result once the caller's own code narrows it (e.g. an `int`-annotated
variable assigned from `Integer(x)`), but not from the call expression
alone: no single return type can describe every class from the one shared
metaclass `__call__`, so `reveal_type(Integer("42"))` on its own is `Any`.

Two further pyright limitations, neither a `hyera` defect (both are
typeshed/pyright modeling an existing Python mechanism for a narrower idiom
than this module uses it for; `tests/typing/consumer_types.py`'s own
docstring has the full detail): `isinstance(value, Integer[1, 10])` (a
*subscripted* type as the second argument) type-checks as an error, because
typeshed's `isinstance` overloads only accept an actual `type`/`UnionType`/
tuple, never an arbitrary `__instancecheck__`-only object, even though it
is correct and tested at runtime -- pass the **bare** class there instead
whenever the static check matters. `reveal_type(Sensitive[String])` always
reads `type[Sensitive]`, never the real `SensitiveType` object it returns
at runtime, because pyright hard-codes `ClassName[args]` through
`__class_getitem__` (`Sensitive`'s own mechanism, unlike every other name
here's dedicated metaclass) to the `Generic`/`NamedTuple` idiom's
`type[ClassName]`, regardless of the method's declared return type.

- **`TypeSpec`** — the type of a `value_type` argument's object form,
  accepted everywhere a type is taken (`lookup`/`dig`/`get`/`explain`/
  `__call__`/`__getitem__`, and a nested type argument in a subscript): a
  type object, a bare `hyera.types` class, or (as always) a Puppet
  type-expression `str`. `lookup`/`explain`'s own `value_type` parameter
  additionally takes a `dict` there (form 5's options hash, unrelated to
  the type itself); `dig`/`get` never do, since they have no options-hash
  form.

## Backends

A self-registering registry: every format or provider
is a `Backend` subclass, found by name rather than passed around directly.

- **`Backend.KINDS = ("function", "v3", "format", "render")`** — four
  separate name namespaces. `function` is the Hiera 5 `data_hash`/
  `lookup_key`/`data_dig` value in a hierarchy level (Puppet function
  names only); `v3` is empty for the built-ins (the v3/v4 config
  reader maps its own names; only a third-party Hiera-3 backend
  registers here); `format` is a plain serialization name; `render` is a
  CLI output-format name (see "CLI" below).
  Registered names, by kind:
  - `function`: `yaml_data`, `json_data`, `hocon_data`, `sops_data`,
    `sops`, `eyaml_lookup_key`, `sops_<yaml|json|ini|dotenv>` (a
    `NamePattern`, matching e.g. `sops_json`).
  - `v3`: none built in.
  - `format`: `yaml`, `json`, `hocon`, `dotenv`.
  - `render`: `s`, `json`, `yaml`.
- **`Backend.NAMES: dict`** — `{kind: (name | NamePattern, ...)}`, declared
  on the defining class only (never inherited/merged); read once at
  subclass-definition time. **`NamePattern(display, regex)`** registers by
  regex instead of an exact string; `regex.fullmatch(name)`'s named groups
  become constructor keywords (used by the `sops_<format>` pattern).
  **`Backend.EXTENSIONS: tuple`** — file extensions (with the dot) this
  format answers to, used by `.for_path`.
- **`Backend(conf=None, *, strict=None)`** — `.conf`. `strict` takes a
  `hyera.Strict` member or the same plain string. `.strict`
  (read-only property, always a plain `str`) is the constructor's `strict=`
  when given, else the
  bound lookup scope's `strict` (`"warning"` with no scope) — read at call
  time, never cached, since one backend instance is shared across scopes. `.name`
  defaults to the class's first registered name; `Backend.new` sets it to
  whatever name was actually asked for.
- **`hyera.BackendKind`** (`FUNCTION`, `V3`, `FORMAT`, `RENDER`) — the four
  `kind=` namespaces below (same set as `Backend.KINDS`); every `kind=`
  argument takes a `BackendKind` member or the same plain string.
- **Lookup** — `.find(name, kind='function')` (exact
  names win, then patterns in registration order; `None` if unregistered);
  `.get(name, kind='function')` (raises `BackendError` for an unknown
  name, and via `.check_available()` for a registered-but-unusable one,
  e.g. missing `pyhocon`); `.new(name, conf=None, *, kind='function', strict=None)`
  (instantiates, passing any `NamePattern` captures as keywords);
  `.names(kind='function')` (exact names, then pattern displays, as a
  `list[str]`); `.for_path(path)` (`format`-kind class with the
  longest case-sensitive `EXTENSIONS` suffix match, or `None`);
  `.implements(op)` (derived from method overrides, never declared twice).
- **Serialization (json-module shaped)** — `.loads(text)`/`.dumps(obj, **kw)`
  (subclasses implement; base raises `NotImplementedError`);
  `.load(source)` (path-like or a file object: reads the bytes, decodes
  strict UTF-8 — `context.rb:53` — then calls `.loads`; a decode error or a
  `.loads` `BackendError` becomes `BackendError("Unable to parse (<path>):
  <problem>", path=...)`, raised outside the `except` block so no
  `__cause__`/`__context__` holds the original); `.dump(obj, fp, **kw)`
  writes `.dumps(...)`.
- **Hiera 5 provider hooks** — `.data_hash(path, options)` (base:
  `._require_path_only(path, options)` then `._as_data_hash(self.load(path),
  path)`, raising `ConfigError("'<name>' one of 'path', 'paths' 'glob',
  'globs' or 'mapped_paths' must be declared in hiera.yaml when using this
  data_hash function")` when `path is None` or `options` carries anything
  besides `path`); `.lookup_key(key, options, context)` /
  `.data_dig(key_segments, options, context)` raise `NotImplementedError`
  in the base; a backend that overrides either is called per key and per
  location with a `hyera.LookupContext` as `context` (below). Neither
  hook's return value is interpolated by the engine — call
  `context.interpolate(value)` yourself; signal a miss with
  `context.not_found()`, never a sentinel return value. A hook's return
  value must be Puppet data (`None`, `bool`, `int`, `float`, `str`, and
  `list`/`dict` of those; a hook may also return a
  tuple, read as a list at any depth); any other type (`date`, `Decimal`,
  `bytes`, `set`, ...) raises `BackendError` naming the function, the
  location and the type. A `data_hash` hook returning such a value under a
  key raises `HieraLookupError` naming the key. An exception a
  `lookup_key`/`data_dig` hook raises itself propagates unchanged.
  `options` carries
  `path` (a `str`) or `uri` (interpolated and normalized like Ruby's
  `URI#to_s`, never fetched) for a located entry, or neither for a
  location-less one — the same mapping a `data_hash` hook receives.
- **`LookupContext(function_context, invocation)`** (in `hyera` and
  `hyera.backends`; Puppet's public `Context`,
  `pops/lookup/context.rb:126-206`) — the `context` argument a
  `lookup_key`/`data_dig` hook receives, built by the engine (never
  constructed directly by a caller), one per hierarchy entry and
  location, scoped to
  the `Hiera`/`h.scoped(...)` view the lookup runs against (never shared
  with another view, same as the provider itself):
  - `.interpolate(value)` — `%{...}` interpolation with method calls
    allowed, against the current lookup's scope.
  - `.not_found()` — raises internally (a `BaseException` subclass, so a
    backend's own `except Exception:` cannot swallow it); never returns.
  - `.explain(producer)` — add `producer`'s text (a zero-argument
    callable) to this lookup's `explain()` report.
  - `.cache(key, value)` / `.cache_all(mapping)` / `.cache_has_key(key)` /
    `.cached_value(key)` (`None` when absent) / `.cached_entries()` (an
    iterator of `(key, value)` pairs) — the hook's own per-location store,
    private to this hierarchy entry, living for the bound view's lifetime.
    The engine never reads or writes it: it is not where results live, and
    a hook is called for every key whatever it stored.
  - The engine keeps each `lookup_key`/`data_dig` result separately, and a
    copy (never the kept object) leaves it each time, so a caller mutating a
    returned list/dict never corrupts it. A result is kept until a file the
    hook read through `.cached_file_data()` for it changes (inode,
    modification time or size); the hook is then called again. A result
    whose call read no file is kept for the top-level lookup that produced
    it, so the next lookup calls the hook again. With `revalidate=False`
    every result is kept until `clear_cache()`. A miss
    (`context.not_found()`) is never kept — a later call for the same key
    calls the hook again.
  - `.cached_file_data(path, parse=None)` — reads and, if `parse` is given,
    parses `path` once, revalidated by `(inode, mtime_ns, size)` on every
    call (not by content); shared by every hierarchy entry on the same
    `Hiera` instance (like the `data_hash` file cache), never per-location.
    Reading a file through it is what lets the engine keep the call's
    result across lookups.
  - `.environment_name` (the scope's `environment`, `"production"` when
    unset) / `.module_name` (the module whose `hiera.yaml` names the
    hook; `None` in the global and environment layers).
- **`default_backends()`** — the distinct classes registered in the
  `function` namespace, in definition order: `[YAMLBackend, JSONBackend,
  HOCONBackend, SopsBackend, EyamlBackend]`. `Hiera(backends=...)` takes
  this same kind of list as an allow-list; a `data_hash`/`lookup_key` name
  whose registered class is not in it is refused exactly like an unknown
  name.
- **`YAMLBackend`** — `NAMES = {"function": ("yaml_data",), "format":
  ("yaml",)}`, `EXTENSIONS = (".yaml", ".yml")`. (The `render`-kind `yaml`
  name belongs to a separate, private `_render.YAMLRender` class.)
  `.loads(text)` parses via hyera's own Psych-compatible loader into a
  plain `dict`/`list`, unadapted — dotted-key access is a function over
  that data, not a container method; raises `BackendError` on a YAML
  error, one line, Psych's shape (`<problem> <context> at line L column
  C`, 1-based; either part may be absent) — never a source snippet or the
  underlying value, with no exception chain. The non-Hash rule (ports
  `yaml_data.rb:27-35`): a `dict` (with any Ruby-symbol key normalized to
  its plain-string name) passes through; `None`/`False` always
  warn-and-empty (`{}`, even under `strict="error"`); any other non-dict
  value raises `BackendError` under `strict="error"`, else
  warns-and-empties. `.dumps(obj, **kw)` is `yaml.safe_dump(sort_keys=False,
  allow_unicode=True, default_flow_style=False)`.
  Numbers, booleans, `null`, symbols and sexagesimal values resolve the
  Ruby (Psych `ScalarScanner`) way, not PyYAML's own Python-flavored
  implicit resolvers. A leading BOM (U+FEFF) becomes a single space, not a
  strip (see the BOM gotcha, below). A YAML date/timestamp-shaped scalar
  raises `BackendError("Tried to load unspecified class: Date"/"...:
  Time")` — like Puppet, there is no lenient mode. A Ruby symbol
  (`hyera.backends.RubySymbol(name)`: immutable, `.name` read-only, equal
  only to another `RubySymbol` of that name, never to a `str`)
  represents a `:symbol`/`!ruby/sym(bol)` scalar; only its use as a **key**
  is normalized to a plain string automatically (a symbol *value* survives
  as `RubySymbol` and is not a valid Puppet lookup value — see the
  RichData gotcha). An unknown tag is tokenized/listed/dict-built like an
  untagged node of the same kind (not a parse error); `!ruby/object`/
  `!ruby/regexp`/etc. (other than `!ruby/sym(bol)`/`!ruby/string`) raises
  the same disallowed-class `BackendError`, naming the class from the tag
  text. `!!set` is always disallowed; `!!omap` builds a `dict` from its
  pairs. A duplicate mapping key: the last one wins; an unhashable key (a
  list/dict from a complex `? ... : ...` key) is frozen into a hashable
  tuple, recursively.
- **`JSONBackend`** — `NAMES = {"function": ("json_data",), "format":
  ("json",)}`, `EXTENSIONS = (".json",)`. (The `render`-kind `json` name
  belongs to a separate, private `_render.JSONRender` class.) `.loads(text)`
  parses the way Ruby's `json` gem (MultiJson's `JsonGem` adapter, Puppet's
  own JSON parser) does, not plain `json.loads`: `/* ... */` and `// ...`
  comments outside string literals are accepted (stripped to spaces before
  parsing, so error line/column still line up); `NaN`/`Infinity`/
  `-Infinity` are rejected (`unexpected token '<name>'`); an unescaped lone
  (unpaired) surrogate code point anywhere in a string, key or value, is
  rejected (`incomplete surrogate pair`) — Python's own decoder accepts
  both by default. Raises `BackendError` on any of these, one line: either
  `<msg> at line L column C` (`json.JSONDecodeError`'s own fields) or the
  comment/NaN/surrogate message above, no exception chain. `.dumps(obj, **kw)`
  is `json.dumps(ensure_ascii=False)`.
- **`HOCONBackend(conf=None, *, strict=None, hocon_includes=None)`** —
  `NAMES = {"function": ("hocon_data",), "format": ("hocon",)}`,
  `EXTENSIONS = (".conf",)`. Always registered: a missing/broken `pyhocon`
  fails at `.check_available()` (backend/level construction, so a
  `hocon_data` hierarchy level fails to build) *and* in `.loads`, both
  naming the `hocon` extra, rather than silently vanishing from
  `default_backends()`. `has_hocon()` — `True` iff `pyhocon`
  imports without error; any import-time exception (not just
  `ImportError`) is caught and logged at debug. `.loads(text)` parses through a
  *private copy* of the `pyhocon.config_parser` module, so a duration (`10s`,
  `5 minutes`) or size string (`10MB`) stays literal text — matching real
  Ruby hocon, which has no duration/size type at all — instead of becoming
  a `datetime.timedelta`; the *shared* `pyhocon` module (what a third party
  importing `pyhocon` directly sees) is never touched; the private copy
  alone gets two deprecation shims (its `codecs`/`logger` names, so
  pyhocon's own deprecated calls never raise under this project's
  `filterwarnings = ["error"]`). A root value that is not an
  object (e.g. a top-level `[1, 2]`) raises `BackendError("... has type
  LIST rather than object at file root")`. `.loads` returns plain
  `dict`/`list`. Invalid UTF-8 (handled by the base `.load`), and any other
  pyhocon parse failure, raise `BackendError` with a one-line message
  (`str(e)`, whitespace-collapsed), no exception chain.

  `hocon_includes` (hyera's own extension, not Puppet vocabulary):
  `None` (the constructor default) reads `conf.get("hocon_includes", True)`;
  an explicit `True`/`False` overrides `conf`. From hiera.yaml, set it with
  `options: {hocon_includes: false}` on a `hocon_data` entry or in
  `defaults: {options: ...}` (a bare `hocon_includes:` key is not valid
  hiera.yaml): `.data_hash(path, options)` takes it from `options`, raises
  `ConfigError` when it is not a Boolean, and still raises the usual
  "one of 'path' ..." `ConfigError` for any other option (Puppet 8.10
  refuses every `options` key on `hocon_data`; this one is hyera's
  extension). It selects which of two scanners `.loads` runs before pyhocon ever parses
  the text:
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
    keyword, or a bare `include` with nothing valid after it — raises
    `BackendError`, matching Puppet's own parse/method errors (Ruby hocon
    implements none of them). One accepted divergence: Puppet's `include
    file("*.conf")` never globs (contributes nothing); pyhocon's own
    resolution does and includes every match.
  - **`False` (opt-in restriction, the pre-fidelity behaviour):** every
    form but a plain quoted include raises, `include file(...)` included.

  `${VAR}` substitutions fall back to environment variables, as in
  Puppet, in either mode. As a fail-closed backstop, pyhocon's own
  include-resolving methods also raise for the duration of `.loads()`, for
  whichever forms the active mode does not intend to resolve for real
  (`url`/`package` always; `file` too when `hocon_includes` is `False`),
  so an undiscovered gap in the text scanner still cannot read a file or
  reach the network; this backstop wraps hyera's private copy only, and is
  installed the first time a document is parsed (importing `hyera` imports
  no `pyhocon`). Each file reached through `include file(...)` goes through
  the same scanner before it is parsed, so these rules hold at every depth.
  A quoted key (`"ntp::servers" = ...`) loads without its quote characters,
  `null` inside a concatenation (`k = null x`) is the text `null`, and a
  `\uXXXX` escape in a quoted string is decoded.
- **`SopsBackend(conf=None, *, strict=None, format=None, timeout=None)`** — `NAMES = {"function": ("sops_data", "sops", NamePattern("sops_<yaml|json|ini|dotenv>", ...))}`.
  Not a `YAMLBackend` subclass; `format` is set by the `NamePattern`
  capture, else inferred.
  `.data_hash(path, options)` infers the format from the file's extension
  with **sops's own rule**, case-sensitive (`cmd/sops/formats/formats.go`):
  `.yaml`/`.yml` → yaml, `.json` → json, `.env` → dotenv, `.ini` → ini,
  anything else → `ConfigError` (sops would read it as binary, which is
  not a data hash). It shells out to `sops -d` (hardened for unattended
  use: `timeout` seconds bound the subprocess, which runs with stdin on
  the null device in its own process group, killed whole on expiry with
  `BackendTimeoutError`; `timeout` is the constructor keyword, else
  `hyera.backends.SOPS_TIMEOUT` (default `30`) read at each call (a
  hierarchy entry cannot carry it: `hiera.yaml` rejects unknown keys); a
  missing `sops` binary or non-zero exit raises `BackendError` with the
  last 2,000 characters of stderr rather than hanging or raising a raw
  `OSError`; the error carries `.path`, so a lookup reports it as is; a
  `sops.bat`/`sops.cmd` shim is refused), then parses the
  decrypted bytes with a `format`-namespace backend (`Backend.new(<out>,
  kind="format")`) — YAML keeps `yaml_data`'s non-Hash rule; JSON/dotenv
  get the engine's generic Hash check instead.
  **`ini` is the one exception to "output type = input type":** sops's own
  INI *writer* is ambiguous, so `ini` is always decrypted as
  `--output-type=json` and parsed with `JSONBackend` instead; there is no
  `IniBackend`. sops's JSON view of an ini-format file is exactly
  `{"DEFAULT": {...}, <section>: {...}, ...}`. `data_hash: sops_ini` still
  forces sops's `--input-type` to `ini` but the output/parse side is
  always `json`, the same as inferred-`ini`.
  A decrypted file that fails to parse raises a one-line, chain-free
  `BackendError("Unable to parse (<path>): <problem>", path=...)` — never
  the decrypted plaintext; a `UnicodeDecodeError` is reported as `invalid
  UTF-8 at byte offset <n>` (never the stock codec message's offending
  byte value). Three `_psych` error messages that would otherwise
  quote the offending scalar or an attacker-suppliable class name verbatim
  have that part replaced with `<redacted>` on the sops decrypt path only
  (plain `yaml_data` keeps Puppet's full text).
  **Gotcha:** sops re-emits YAML through its own Go YAML writer, which
  changes shape on decrypt — a date-shaped scalar becomes a full ISO
  timestamp (still disallowed, just with a different message source);
  tags are stripped; `!!binary`/`!!null` round-trip to plain text/`null`;
  octal ints are re-emitted as decimal; a `:symbol` scalar/key survives as
  plain `:name` text (still parses to a `RubySymbol`).
- **`EyamlBackend`** — `NAMES = {"function": ("eyaml_lookup_key",)}`, a
  `lookup_key` provider, Puppet's own name for hiera-eyaml. Requires the
  optional `eyaml` extra (`cryptography`); `.check_available()`
  raises `BackendError` naming the extra when it is missing, at level
  build via `Backend.new`, the same shape as the `hocon_data`/missing-
  `pyhocon` message. **PKCS7 only** — the private key alone is needed (no
  certificate); GPG or any other hiera-eyaml encryptor plugin raises
  hiera-eyaml's own `LoadError` text, unwrapped, since only the `eyaml`
  extra's PKCS7 path is supported. Options: `pkcs7_private_key` (a path
  **relative to the process's current working directory**, not
  `base_path` or the data file's own directory — matches hiera-eyaml
  itself), `pkcs7_private_key_env_var` (wins over the plain path, with a
  logged warning if both are set), `pkcs7_b64_private_key_env_var` (wins
  over the plain path silently, base64-decoded with Ruby's lenient
  `Base64.decode64` rules); `pkcs7_public_key*` options are accepted
  (Puppet's own schema has them) but never read. `.lookup_key(key, options, context)`:
  the raw `.eyaml` file's own non-Hash rule matches `yaml_data`'s (reads
  `self.strict` fresh on every read, never cached). A decrypted value is
  interpolated (methods allowed) exactly like a `data_hash` result — a
  Hash's **keys** are interpolated but never decrypted, only its values
  recurse; every other type (int/float/bool/None) passes through
  unchanged. Decrypt failures raise `BackendError("hiera-eyaml backend
  error decrypting <token> when looking up <key> in <path>. Error was
  <message>")`; a missing `path`/`uri` location raises `ConfigError` like
  any other `lookup_key` function.
- **`DotenvBackend`** — `format`-namespace only (`dotenv`; no Puppet
  `data_hash` equivalent, reachable only through `SopsBackend`). Parses
  **exactly the shape sops's own writer emits**, not a general dotenv
  dialect. `.loads(text)`: blank lines and `#`-prefixed lines are skipped;
  the first `=` splits key/value; a literal two-character `\n` in the
  value becomes a real newline; a line with no `=` raises
  `BackendError("invalid dotenv line <n>")` — never the line's own text.
  There is no `IniBackend`: see `SopsBackend` above for why `ini` is
  parsed as JSON instead.
- **`hyera.RenderAs`** (`S`, `JSON`, `YAML`) — a `puppet lookup
  --render-as` output format; see "Rendering" below.
- **Rendering** — three `render`-kind-only, private `Backend` subclasses
  (`hyera._output.render`), found the same way (`Backend.new(fmt, kind="render")`,
  `fmt` a `hyera.RenderAs` member — `S`/`JSON`/`YAML` — or the same plain
  string): Puppet's `puppet lookup --render-as` output. `hyera.RenderAs` has
  no place in the CLI's own `--render-as` flag, which stays a plain string
  (see "CLI" below). Each implements only
  `dumps(obj) -> str`; none of the Hiera 5 provider hooks or `loads`
  apply. `s` renders Ruby `to_s` (Ruby 3.2 AIO hash form `{"k"=>v}`,
  `Sensitive [value redacted]`) — the same renderer a bare `%{var}`/
  function-call result uses. `json` renders compact, insertion-ordered
  JSON (`ensure_ascii=False, allow_nan=False, separators=(",", ":")`); a
  non-finite `float` raises `ValueError` (`NaN not
  allowed in JSON`, etc.); anything else not representable as Puppet data
  raises `TypeError("<type name> is not a Puppet data value")`. `yaml`
  renders text that reads back as the value rendered, under PyYAML and
  Psych alike (`explicit_start=True, default_flow_style=False,
  sort_keys=False, allow_unicode=True`; a string Psych's scalar scanner
  would read as another type is quoted, and no alias is written; an
  integer of any length renders in full); every render ends with exactly one trailing `\n`.
- Env: `sops` runs with the process environment, so its own `SOPS_*` and
  key-source variables apply. `SOPS_TIMEOUT` is a module attribute, not an
  env var — set it directly (`hyera.backends.SOPS_TIMEOUT = 60`) to change
  the default sops timeout; it is in `hyera.backends.__all__`. See "Environment" below for the fixed-name variables.

## Errors

`HieraError(*args, path=None)` (base; `.path` names the file concerned, or
`None`) →

- **`ConfigError(*args, path=None, line=None)`** (also a `ValueError`) —
  anything about `hiera.yaml`: missing, unreadable, unparsable, non-mapping, an
  unsupported `version`, or wrong shape. A read/shape problem's message
  names the origin directly; an unparsable file's is `(<path>): <problem>
  at line L column C` (Psych's shape, one line). `.line` (in addition to
  the inherited `.path`) names the 1-based line in `.path` a problem was
  found at, when known (`None` for a dict config, or when no line
  applies); a message that includes a line also ends with Puppet's own
  `(file: F, line: N)` suffix.
- **`BackendError`** (also a `ValueError`) — a data file could not be read
  or parsed. `.path` names it; an unparsable file's message is `Unable to parse (<path>):
  <problem> at line L column C`, one line. Raised from the first lookup
  whose scope reaches the bad file (never from `Hiera(...)` itself — see
  "Lookup" above).
- **`BackendTimeoutError`** — a `BackendError` that is also a builtin
  `TimeoutError`: `sops` or `facter` did not finish within its time limit
  and was killed with its child processes. Message: `<program> timed out
  after <n>s`.
- **`HieraLookupError`** — Puppet's `LookupError`: a failure while resolving
  a key, including a `convert_to` whose type cannot be parsed or whose
  conversion/result-type assertion fails (see "Types" above and the
  `convert_to` Gotcha below for the two message forms). →
  - **`InterpolationError`** (also a `ValueError`) — a `%{...}`
    interpolation or function call could not be resolved.
  - **`MergeError`** (also a `ValueError`) — an unknown or invalid merge
    strategy.
  - **`KeyNotFoundError(name)`** (also a `KeyError`) — `.lookup()`'s miss (no
    default given), with Puppet's message ("Function lookup() did not find
    a value for the name '<key>'", or the "any of the names [...]" plural
    form). `.name` holds the key(s) tried.

Every class above is importable directly from `hyera` (e.g.
`hyera.BackendError is hyera.backends.BackendError`, both paths work since
`hyera.__init__` re-exports it too).

## CLI

`hyera.cli` exports `main` always; `Lookup` only when the `cli` extra
(`duho`) is installed.

- **`main(argv=None)`** — the `hyera` console-script entry point;
  builds and dispatches the `Lookup` duho command, which sets
  up `-v/-q/--loglevel` logging and returns the process exit code. When the
  `cli` extra (`duho`) is not installed, `main` always exists but prints
  `hyera: the command-line interface needs the cli extra: pip install
  "hyera[cli]"` to stderr and returns 2, instead of raising
  `ModuleNotFoundError`; `Lookup` itself is not defined in that case.
- **`Lookup`** — the `duho.Cli` command class, accepting `puppet lookup`'s
  own flag set. `.__call__()` runs the parsed command and returns the exit
  code. Fields, grouped:
  - *lookup*: `keys` (positional, zero or more — the first one found wins),
    `merge` (`--merge first|unique|hash|deep`; any other value exits 2,
    validated by hand rather than via argparse choices),
    `knock_out_prefix`/`sort_merged_arrays`/`merge_hash_arrays`
    (`--knock-out-prefix`/`--sort-merged-arrays`/`--merge-hash-arrays`,
    only meaningful with `--merge deep`; any of the three without it exits
    2), `value_type` (`--type`, a Puppet type string;
    parsed once, up front, so a syntax error exits 2 even when the key
    would otherwise just miss), `default` (`--default`), `explain`/
    `explain_options` (`--explain`/`--explain-options`).
  - *facts and scope*: `facts` (`--facts FILE`, parsed by `load_facts`;
    **mandatory** — an absent or empty `--facts` exits 2 with "No facts
    available for target node: `<--node or the local fqdn>`"), `node`
    (`--node NAME`, `Scope(node_name=...)` only — seeds no fact), `scope`
    (`--scope`/`-s NAME=VALUE`, repeatable; VALUE is YAML, an empty VALUE
    is `None`, a dotted NAME nests a nested hash, the CLI's one flag with
    no `puppet lookup` counterpart).
  - *settings*: `hiera_config` (`--hiera_config PATH`; default `./hiera.yaml`
    if it exists, else `Hiera(None, base_path=os.getcwd())` — Puppet's
    built-in default configuration; a *named* missing file is still a
    `ConfigError`), `environment`, `environmentpath`, `modulepath`,
    `basemodulepath` (each split on `os.pathsep` and absolutized by the
    CLI, then passed straight through to `Hiera(...)`'s own keywords — the
    library owns discovery and the missing-environment error, so CLI and
    API can never disagree), `codedir`, `strict` (`--strict off|warning|
    error`, default `None` meaning `"warning"`; checked by `Scope`, not by
    argparse, so a bad value is one `ERROR` line like any other).
  - *output*: `render_as` (`--render-as FORMAT`, default `None` meaning
    `"yaml"`, or `"s"` while explaining; case-insensitive; an unrecognized
    format exits 2 with `Unknown rendering format '<f>'` before any lookup
    runs). Plain strings only, by design: `--merge`/`--strict`/`--render-as`
    never take a `Merge`/`Strict`/`RenderAs` member *name* the way duho's
    own `Enum` CLI support would (it resolves by member name — `DEEP`,
    `ERROR` — not by value, which would silently stop accepting Puppet's
    own lowercase flag values). Output goes through the `s`/`json`/`yaml` render
    backends ("Rendering" above), the same shapes `puppet
    lookup --render-as` prints; a `Sensitive`
    value redacts in every format, including `yaml` (Puppet's own YAML
    leaks the plaintext). A non-finite float under `--render-as json` exits
    2. Output is written
    as UTF-8 bytes with LF line endings, regardless of the console or
    locale encoding, with a trailing newline
    added only if the rendered text lacks one (Ruby `puts` semantics); a
    a write that fails with any `OSError` (a reader that closed the pipe, a
    full device) is silenced and reported as exit 2 with nothing on stderr.
    `main()` never reconfigures `sys.stdout`/`sys.stderr`.
  - `debug` (`--debug`/`-d`) — equivalent to `-vv`; see "Logging" below.

  Not accepted: `--config`/`-c` (the flag is `--hiera_config`), `--deep`
  (`--merge deep`), `--knockout-prefix` (`--knock-out-prefix`),
  `--compile`/`-c`, `--trusted` (Puppet's own is a no-op in 8.10), and the
  `array`/`set` `--merge` aliases.

  **Argument order, matching `puppet lookup`'s own `main`:** the deep-only
  guard, then `--merge` validation, then the no-keys check (`--explain-options`
  alone with no key becomes the key `"__global__"`; otherwise "No keys were
  given to lookup."), then the render format, then scope/facts, then
  `--hiera_config`/layers/`--type` and the lookup or `--explain` itself.
  `main()` first runs every argv token through
  `_puppet_argv`, which joins a long value option with its following token
  (`--opt value` -> `--opt=value`) so a value that itself looks like an
  option (`--knock-out-prefix --`, `--default -x`) reaches argparse the way
  Puppet's own parser would consume it; a value that is exactly `"--"`
  (two-token or `--opt=--`, also in an MCP tool call) is kept as the value.
  Tokens after a bare `--` (not itself following a value option) go to
  duho's own passthrough, treated as more keys — Puppet's own
  "everything after this is a key" convention — and never reach
  `--render-as`/any other flag.

  Exit codes: `0` found (or `--default`/`--explain` printed), `1` the key
  was not found (a `KeyNotFoundError` and nothing else — nothing is
  printed, as `puppet lookup` prints nothing for a miss), `2` every other
  error — a usage problem, an unknown (or empty) render format, an
  unreadable facts file, a `ConfigError`/`BackendError` (a
  `BackendTimeoutError` included), an unrenderable value, or stdout that
  cannot be written, a closed pipe included (both silent: nothing on
  stderr); `main()` returns `130` on Ctrl-C, with no traceback.
  `--explain`/`--explain-options` exit 0 even on a miss or most lookup
  failures (Puppet's own explain report documents the failure as its own
  last line instead); only a configuration/data problem building the
  `Hiera` instance itself exits 2 under `--explain` too. A `2` from a
  lookup or render failure logs exactly one `hyera`-logger ERROR line:
  `Lookup of key 'K' failed: …` for a lookup failure (construction
  included, comma-joining every key tried), `Cannot render the value of
  key 'K': …` if printing the found/default/explained value itself fails.
  The traceback is omitted unless `-v`, `-d`/`--debug` or
  `DUHO_TRACEBACK=1` is set.
  `$server_facts` carries `serverversion` (`8.10.0`, the `puppet lookup` release
  hyera is measured against) and `environment` only — no host-identity keys.
  Facts come only from `--facts`; this CLI never runs facter or reads
  stored facts, so an unattended lookup has no hidden subprocess and gives
  the same answer on every host.
- **Logging** (`-v`/`-q`/`--loglevel` change the `hyera` logger):
  the verbosity scheme follows Puppet's own from a `WARNING` base —
  no flag: warning; `-v`: info; `-vv` or `-d`/`--debug`: debug; `-vvv`:
  trace; `-q`: error; `-qq`: critical. A plain miss (no `--default`) logs
  at DEBUG (Puppet's own "did not find a value for the name…" text), so it
  is silent by default and under `-v`, matching Puppet exactly; only
  `-vv`/`-d`/`--loglevel hyera:DEBUG` shows it. `--help` is plain text.

## Environment

Every environment variable `hyera`'s own code, or a dependency it invokes
in a way that changes visible behavior, reads by a fixed name:

- **`ALLUSERSPROFILE`** — read only on the platform-default-`codedir`
  fallback (no explicit `Hiera(codedir=...)`/`--codedir`), Windows only:
  Puppet's AIO default `codedir` is `%ALLUSERSPROFILE%\PuppetLabs\code`,
  falling back to the literal `C:\ProgramData` if unset.
- **`HYERA_MCP`** — `HYERA_MCP=stdio` runs the `hyera` command as an MCP
  server over stdin/stdout (via `duho`), exposing one tool, `hyera`
  (`Lookup`'s program name, not its class name), whose arguments are the
  CLI fields (`keys`, `hiera_config`, `facts`, `scope`, ...); a
  `tools/call` returns what the command would print, and
  `initialize`'s `serverInfo.name` is `"hyera"` too. Any other
  `HYERA_MCP` value exits `2` with `unsupported MCP transport`. The
  trigger variable name itself is always `HYERA_MCP`, regardless of
  `sys.argv[0]` (so `python -m hyera.cli` or embedding `Lookup` in a
  differently-named script never changes it). The trigger is read before
  the arguments, so an MCP session never performs a command-line lookup.
- **`AGENT_HELP`** / **`AGENTS_HELP`** — either truthy makes `--help` print
  duho's JSON agent-help document instead of usage text.
- **`DUHO_TRACEBACK`** — `DUHO_TRACEBACK=1` adds a traceback to a `2`-exit
  CLI error (same effect as `-v`/`-d`).

Not a fixed-name environment variable, but env-adjacent: `sops` (invoked
by `SopsBackend`) runs with the process environment, so its own `SOPS_*`
and any key-source variables it reads apply; `SOPS_TIMEOUT` is a
`hyera.backends` module attribute, not an env var. `EyamlBackend`'s
`pkcs7_private_key_env_var`/`pkcs7_b64_private_key_env_var` hiera.yaml
options name an environment variable *dynamically* (the variable's own
name is data, not a fixed hyera name) to read the eyaml private key from.

## Differences from Puppet

- **difference** `missing-config-raises` — A missing hiera.yaml raises
  `ConfigError` from `Hiera(path)` or `--hiera_config`, instead of Puppet's
  built-in-default fallback.
- **difference** `config-dir-not-interpolated` — The directory holding
  hiera.yaml is used literally, with no `%{...}` interpolation and no
  glob-metacharacter escaping.
- **difference** `config-not-revalidated` — A changed hiera.yaml is not
  re-read by an existing `Hiera`; construct a new one to pick it up.
- **difference** `environmentpath-none-means-no-layer` — With
  `environmentpath=None` (the default), every environment name resolves
  with no environment layer and no error, instead of Puppet's
  always-configured `environmentpath` raising for an unknown one.
- **deviation** `v3-ruby-backend-unavailable` — Ruby Hiera 3 backends
  cannot run in Python, so an unregistered Hiera 3 backend name raises
  `ConfigError` where Puppet with Hiera 3 installed skips it.
- **difference** `codedir-aio-default` — `codedir` defaults to Puppet's
  AIO system location for the platform, never the per-user default or a
  value discovered from `puppet.conf`.
- **deviation** `sops-backend` — hyera keeps a `sops`/`sops_data`
  `data_hash` backend that Puppet does not have.
- **difference** `hocon-include-glob` — `hocon_data`'s `include
  file("*.conf")` globs and includes every match by default; Puppet's own
  `hocon_data` never expands such a glob.
- **deviation** `hocon-pyhocon-parser` — `hocon_data` is parsed by
  pyhocon, so `+=`, capitalised booleans, boolean concatenation,
  leading-zero and whole-float numbers, object-then-scalar and quoted-path
  keys, the empty-string key, a byte-order mark, object key order, a
  backslash-slash escape and the two forms hyera accepts and Puppet
  rejects (`[1,, 2]`, `+1`) differ from Puppet's hocon gem.
- **difference** `eyaml-pkcs7-only` — `eyaml_lookup_key` supports only the
  PKCS7 encryptor; other hiera-eyaml encryptors raise the same error
  Puppet gives without their plugin.
- **deviation** `integer-dotted-navigation-error-text` — navigating a
  dotted sub-key into an Integer keeps hyera's own "Data Provider type
  mismatch" text; Puppet crashes with a raw Ruby `NoMethodError` instead
  of a designed message.
- **deviation** `knockout-prefix-not-python-regex` — a `knockout_prefix`
  that Python's `re` module cannot compile raises an error; Ruby accepts
  it with a warning ("regular expression has redundant nested repeat
  operator").
- **deviation** `convert-to-unsupported-type` — hyera implements Puppet's
  `new()` only for types whose values are plain data; converting to
  SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI, Type or
  Object raises.
- **difference** `strict-default-warning` — Undefined variables default to
  `strict="warning"`, not Puppet 8's `strict="error"`.
- **difference** `glob-case-sensitive-byte-order` — Glob wildcards are
  case-sensitive and results sort by byte order on every OS, unlike Ruby
  on Windows.
- **difference** `render-yaml-sensitive-redacted` — `--render-as yaml`
  prints `Sensitive` values redacted, where Puppet prints the plaintext.
- **difference** `render-yaml-equivalent-not-identical` — `--render-as yaml`
  reads back as the value looked up, but its quoting need not match
  Puppet's text; no anchors or aliases are written, and `--explain
  --render-as yaml` writes the tree with plain string keys.
- **difference** `aio-hash-rendering` — `--render-as s` prints hashes in
  Ruby 3.2's AIO form (`{"a"=>1}`), as Puppet 8's own packages do.
- **difference** `scope-flag-sets-node-parameters` — `--scope
  NAME=VALUE` sets node parameters, which `puppet lookup` takes from the
  node classifier instead.
- **difference** `facts-from-file-only` — Facts come only from
  `--facts`/`Scope(facts=...)`; `puppet lookup` also reads local facter or
  PuppetDB-stored facts.
- **difference** `server-facts-minimal` — `$server_facts` holds only
  `serverversion` and `environment`, where a real Puppet server populates
  more.
- **difference** `environment-conf-compile-trusted-unsupported` —
  `environment.conf` is not read; `--compile` and `--trusted` are not
  supported.
- **difference** `python-equal-hash-keys` — A YAML mapping whose keys
  collide only in Python (`1`/`1.0`/`true`) raises `BackendError` naming
  them, and `--merge deep` joins such keys from different levels; Ruby
  keeps them apart.
- **difference** `non-utf8-data` — Data files and `eyaml` plaintext are
  read as strict UTF-8: a `json_data` file with one bad byte fails every
  lookup that reaches it, non-UTF-8 `eyaml` plaintext raises, and a
  `!!binary` value that is not UTF-8 cannot be rendered as `s` or `json`.
- **difference** `nesting-bound` — YAML and JSON documents (data, facts,
  `--scope` values) nested more than 500 levels deep raise `BackendError`
  ("nested too deeply").
- **difference** `ruby-regex-constructs` — A `Pattern`/`Regexp` type or
  `lookup_options` key using `\p{..}`, `\P{..}`, `\R`, `\X`, `\G`, `\K`,
  `\g<..>`, `&&`, a nested `[...]`, `\D \W \S \H` inside `[...]`, a nested
  repeat such as `a**`, or (Python 3.9 and 3.10) a possessive quantifier or
  atomic group raises `HieraLookupError` naming the construct; POSIX bracket
  classes are ASCII-only; no match-time bound exists, as in Ruby, so a
  pattern with nested quantifiers can take exponential time on a long
  subject.
- **difference** `string-format-subset` — `String` formats cover one
  directive per value: a type-map format, the `#` indenting flag on an
  Array or Hash, and a precision on `%a`/`%A` raise `HieraLookupError`, as
  does converting a Binary, Timestamp, URI or Object value.
- **difference** `interpolation-chain-depth` — A chain of `%{lookup()}`/
  `%{alias()}` interpolations resolves to about 80 hops (Puppet: 100); a
  longer one, or a value nested past Python's recursion limit, raises
  `InterpolationError` naming the keys.
- **difference** `interpolation-key-shapes` — `%{::::x}` is an undefined
  variable (Puppet prints the fact), and a hash key that interpolates to an
  Array raises `InterpolationError` (Puppet keeps the Array as the key).
- **difference** `error-exit-status` — Every error exits 2 where `puppet
  lookup` exits 1 (a miss exits 1 in both), and hyera logs one `ERROR` line
  where Puppet prints `Error: Could not run:` and the message.
- **difference** `error-message-text` — Error message text is hyera's own
  where it differs: the unknown-function error ends with `known: ...`, a
  global-layer version 4 refusal has no `(file: ...)` suffix, and
  `--explain` of a version 3 config with a relative `:datadir:` shows
  absolute paths where Puppet shows them as written.
- **difference** `schema-error-line-suffix` — Schema errors carry
  `(line: N)`, which Puppet never prints, and report only the first
  mismatch where Puppet lists all of them.
- **difference** `puppet-crashes-hyera-answers` — `puppet lookup --type
  Data k` (any type alias), an Integer key in `lookup_options` or module
  data, and `Float.new("0")` crash Puppet 8.10; hyera returns a value,
  ignores the key, and returns `0.0`.
- **difference** `environment-trailing-slash` — `--environment
  production/` is accepted by Puppet and rejected by hyera as an unknown
  environment.
- **difference** `dir-glob-ruby-quirks` — A brace group directly after
  `**/` is matched per directory in sorted order by Ruby (hyera expands it
  first, in written order, so such a `glob` level's files can be searched
  in a different order); Ruby keeps a doubled `/` in a result; a brace
  that expands to an empty pattern also returns the base directory in
  Ruby, which Puppet discards.
- **difference** `glob-case-folded-spelling` — On a case-insensitive
  filesystem a literal glob segment matched by case folding is returned in
  the pattern's spelling; Ruby returns the on-disk spelling.

Not supported:

- The legacy Puppet functions `hiera()`/`hiera_array()`/`hiera_hash()`/
  `hiera_include()` as Python methods — see "Coming from `hiera()`" under
  Lookup for the equivalent `.lookup()` calls. The `%{hiera('x')}`
  interpolation function stays supported.
- Running Ruby Hiera 3 backends or `hiera3_backend` Ruby code: a
  third-party Python `Backend` may register under the name; anything else
  raises `ConfigError` (`v3-ruby-backend-unavailable`, above).
- Catalog-compilation context: class-local variable scopes beyond an
  explicit `variables=` layer, `calling_class`/`calling_module`, and
  `--compile`.
- `--render-as binary|msgpack|console|flat|rich_data_json`, `-V`, underscore
  spellings of a hyphenated flag, abbreviated options, Ruby's JSON float
  text and an empty `--environment`.
- The GPG eyaml encryption scheme — detected and reported as an
  unsupported plugin; only PKCS7 is implemented.
- Type aliases (`Stdlib::*`, user-defined) in a type expression, and
  `new()` for `Timespan`/`Timestamp`/`SemVer` in `convert_to` — an
  explicit "unsupported type" error (see "Types" above).
- Discovering `environmentpath`/`modulepath`/`codedir` from `puppet.conf`:
  they are explicit constructor and CLI arguments only.

## Gotchas

- Debug trace: with the `hyera` logger at `DEBUG` (and a handler attached),
  each `lookup`/`dig`/`get`/`explain` call logs one record on the
  `hyera._output.explain` logger: `Lookup of '<key>'` followed by the same report
  `explain()` returns, each line indented two spaces. Each
  `%{lookup()}`/`%{hiera()}`/`%{alias()}` encountered while resolving a
  value logs its own record first, in the order it actually runs. With
  `DEBUG` off (the default), nothing is recorded and no explain tree is
  built at all — checked once per top-level call, not once per hook.
- The engine interpolates a `data_hash` value (methods allowed) but never a
  `lookup_key`/`data_dig` result — a backend that wants interpolation calls
  `context.interpolate(value)` itself. `lookup_key`/`data_dig` providers,
  their kept results and their `LookupContext` caches are per scope-binding
  object (`Hiera`/`h.scoped(...)` view), never shared with another view; `cached_file_data`
  is per `Hiera` instance, shared by every hierarchy entry. A backend's
  `context.not_found()` raises a `BaseException` subclass, not `Exception`
  — a backend wrapping its own logic in `except Exception:` does not
  accidentally swallow it.
- A self- or mutually-referencing interpolation (`%{lookup('a')}` inside
  `a`; a variable whose value refers to itself; a chain `a` -> `b` -> `a`)
  raises `InterpolationError` "Recursive lookup detected in [a, b]" (the
  keys/scope-references visited, in the order first reached) instead of
  Python's own `RecursionError`. A chain that is not cyclic but runs past
  the interpreter's recursion limit (about 80 `%{lookup()}`/`%{alias()}`
  hops on Python 3.9 and 3.14; Puppet resolves 100), or a value or type
  expression nested beyond it, raises `InterpolationError` too, naming the
  keys being resolved; every public entry point (`lookup`, `explain`, `get`,
  `dig`, `getvar`, `format`) converts it. hyera never changes the
  interpreter's recursion limit.
- A value that reuses a YAML anchor (`&x`/`*x`) shares that node wherever it
  appears in one lookup's returned value, exactly as the parsed file does:
  mutating one occurrence in place changes every occurrence that shares it.
  A value that a merge actually combines with another is always copied per
  position first, so the merge's own in-place semantics never reach a
  shared node and corrupt an unrelated position.
- Interpolation is a single left-to-right pass over each `%{...}` occurrence
  in the original text: text inserted in its place is never re-scanned, only
  a method's own resolved result is interpolated again (so a variable whose
  *value* itself contains `%{...}` is interpolated, but text a call just
  produced is not scanned a second time for new `%{...}` occurrences).
  Whitespace inside `%{ }` is ignored (`%{ x }` is `%{x}`), and hash keys are
  interpolated the same as values.
- A `%{hiera(...)}`/`%{lookup(...)}`/`%{scope(...)}` call embedded inside a
  larger string must resolve to a scalar; interpolating a non-scalar
  (list/dict) into a string raises `InterpolationError`. Only a **whole**
  `%{alias(...)}` (the entire value, not embedded in more text) keeps the
  resolved value's native, possibly non-scalar type; an alias embedded in a
  larger string is a different error (below).
- An interpolated non-string renders as Puppet renders it: `true`/`false`,
  `""` for null, `1.0e+20`, `["a", "b"]`, `{"k"=>"v"}` (the form Puppet 8's
  packaged Ruby 3.2 prints), `Sensitive [value redacted]`.
- `%{lookup(...)}`/`%{hiera(...)}`/`%{alias(...)}` of a missing key resolve
  to `""` (Puppet's own rule) rather than raising — same as a missing
  `%{var}`/`%{scope(...)}` reference, so the two are symmetric. Each runs a
  **full lookup** of its own target key: that key's own `lookup_options`
  (merge and `convert_to`), a `default_hierarchy` fallback on a main miss,
  the enclosing `.lookup()` call's `override`/`default_values_hash` (still
  consulted for the sub-lookup's own key) — everything a top-level
  `.lookup()` would do — never only the hierarchy currently being walked.
  The one thing it never inherits is the *caller's* `merge=`/`lookup_options`
  accumulator: a sub-lookup always resolves with `merge=None` (first-match,
  or its own `lookup_options`' merge), so nesting `%{lookup(...)}` inside a
  `merge="deep"` lookup never pulls the caller's strategy into the nested
  one. `lookup_options` and `lookup_options.<x>` are reserved and never
  resolve, from any caller (interpolation, `.lookup()`, a nested
  `%{lookup(...)}`) alike.
- `%{x}` and `%{scope('x')}` follow the same rule for an undefined root: it
  is governed by the bound `Scope`'s `strict` (`"off"`/`"warning"`/`"error"`,
  default `"warning"`) exactly as `Scope.lookupvar` documents, while a
  *missing nested segment* off an otherwise-defined value always resolves to
  `""` regardless of `strict`.
- Errors: an unknown interpolation method (`%{nosuch('x')}`), a misplaced
  `%{alias(...)}` (embedded in a larger string instead of being the whole
  value) and a non-hashable interpolated hash key all raise
  `InterpolationError`. A malformed `%{...}` expression (an unbalanced quote,
  a stray dot) raises `HieraLookupError` "Syntax error in string: `<text>`".
  Indexing a dotted reference into a scalar (or any value that is not the
  collection type the next segment needs) raises the same type-mismatch
  `HieraLookupError` a dotted lookup key raises.
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
- A `lookup_options` key is a **regex only when it starts with `^`**
  (Hiera 5's rule); anything else is matched literally, so a key containing
  `.` cannot shadow-match unrelated keys. Patterns use Ruby syntax
  (`(?<name>…)`, `\A`, `\z`, `\h`/`\H`, POSIX bracket classes, inline
  `(?i)`/`(?m)`/`(?x)` flags and a lookbehind all work; `\w \d \s` are ASCII;
  a construct with no Python translation raises `HieraLookupError` naming it,
  see the `ruby-regex-constructs` difference), match by
  **searching** from the start of the key (`^app::` matches `app::ports`,
  not only `app::`), and are tried in the merged order — lower-priority
  levels' patterns first. An exact key match always wins over a pattern.
  An invalid pattern, or a `lookup_options` value that is not a hash, raises
  `HieraLookupError` — the whole lookup fails, not just the one key. An
  entry that is a string applies no options and blocks a matching pattern
  from being tried at all (same key, both an exact and a pattern entry); any
  other non-hash, non-string entry (a list, an integer, a boolean, ...)
  raises `HieraLookupError` too. `lookup_options` is matched by a key's
  *root* only, never a dotted key as written, and is fetched **always** —
  even under an explicit `merge=` — because an explicit `merge=` replaces
  only the *merge* `lookup_options` would have picked; its `convert_to`
  still runs on the result either way.
- With layers configured, `lookup_options` entries declared in the global,
  environment and module data all apply to the same key, gathered and HASH-
  merged in that priority order — global wins over environment, which wins
  over module, per key (an environment or module with no entry for a key
  leaves the higher layer's entry untouched, rather than blanking it). A
  module's own `lookup_options` keys and `^`-prefixed patterns must be
  qualified with that module's own name (`<module>::...`), else
  `HieraLookupError` ("all lookup_options keys must start with module name
  '<module>'" / "...patterns must match a key starting with module name
  '<module>'"), raised whenever that module's options are read at all — not
  only for the one offending key. A module's own options apply to that
  module's keys found in *any* layer (global, environment or module data
  alike), not only its own.
- A found root value that is not Puppet RichData — a hash keyed by anything
  other than a `String`/numeric (a boolean, `~`, or a nested collection), or
  a Ruby symbol (`hyera.backends.RubySymbol`) anywhere in the structure —
  raises `HieraLookupError` naming the key, the `data_hash` function and the
  file, the moment that value is found (before interpolation or a merge), so
  a bad value at one key never breaks a lookup of any other key in the same
  file.
- A **dotted reference** (`%{trusted.certname}`, `%{facts.os.family}`) and a
  **dotted lookup key** (`h.lookup("a.b.0")`) both follow Puppet's own
  sub-key grammar, in hierarchy paths, `datadir`,
  `mapped_paths` templates, values, `.format()`, `%{scope('a.b')}` and
  `.lookup()` alike: a segment may be single- or double-quoted (quotes keep any
  embedded `.` literal and are trimmed off; whitespace around an unquoted
  segment or the dots themselves is trimmed too, e.g. `%{ a . b }`), and a
  segment made only of optionally-signed digits indexes a list (out of
  range, or a negative index, is a miss, never Python's wraparound). A
  dotted **lookup key** specifically resolves its *root* segment (`a`)
  first — through the full provider/level/location merge, with the root's
  own `lookup_options` applied — and digs the remaining segments (`.b.0`)
  out of that single merged value exactly once; a level whose root value
  lacks the dug segment is a miss, it never falls through to dig a
  different level's value instead. A root key set to `~` is *found*, with
  the value `None`: it stops a first-found lookup at that level (never
  falls through to a lower one) and beats a `default=`, same as any other
  found value — only an absent root key, or a segment a dig cannot reach
  (an out-of-range index, a key a dict lacks, a `None` met mid-dig), is a
  miss.
  A Puppet variable name cannot contain `.`, so there is **no flat-key
  fallback**: `%{a.b}` always means "navigate `.b` into the value of `a`",
  never a literal scope/data key named `"a.b"` — quote the whole
  reference (`%{'a.b'}`, `h.lookup("'a.b'")`) to reach that key instead.
  A malformed key (an empty/unbalanced quoted segment, a stray leading,
  trailing or doubled `.`) raises `HieraLookupError` with Puppet's "Syntax
  error in key/string" text, and so does navigating into (or with) the
  wrong type — a non-collection value with a further segment, or a
  non-string/non-`Integer[...]`-shaped root variable name (that one raises
  `InterpolationError` instead) — **even when a `default_value=` was
  given**, or through `in`: only a genuine miss (an absent key, a `None`
  value walked no further, or an out-of-range/nonexistent segment) is
  silent.
  A malformed reference in a hierarchy path raises `HieraLookupError`
  "Syntax error in string" too, and so does a well-formed one that hits a
  type mismatch, same as in a value.
- References resolve against the bound `Scope`, so a value or path
  reference means the same thing everywhere: `%{environment}`/
  `%{trusted...}` are always defined (`Scope`'s own defaults, never a
  level skip); a defined `False`/`0`/`""`/`[]`/`{}` variable or fact is
  never dropped (kept, unlike a genuinely unbound one); a bare embedded
  `False`/`True` renders `false`/`true` and `None` renders `` (the empty
  string) — a *standalone* `%{scope(...)}`/`%{hiera(...)}`/`%{lookup(...)}`
  call still stringifies a scalar result the same way (only `%{alias(...)}`
  preserves a non-scalar result's native type when it is the entire value).
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
  between the two loaders. Both venvs and every published
  wheel ship libyaml, so this is a real fallback path (a source build
  without it, or `PyYAML` built `--no-libyaml`), not a hidden dead branch;
  it is tested and documented, not worked around.
- A YAML **complex key** (`? [a, b]\n: 1`, or a Hash key) parses to a
  hashable tuple (recursively frozen), and a **symbol value** (`:foo`,
  `!ruby/symbol x`) parses to a `RubySymbol` — both load without error, but
  neither is a valid Puppet lookup *value*: looking up a key whose root
  value holds one raises `HieraLookupError`, as in Puppet.
- **`convert_to` is Puppet's `new()`.** A `str` first element of the
  `convert_to` spec is parsed as a Puppet type expression first; a parse
  failure raises `HieraLookupError("Invalid data type in lookup_options for
  key '<key>' could not parse '<source>', error: '<msg>")` (Puppet's own
  format string, with its unbalanced closing quote, verbatim). A parseable
  type whose conversion or result-type assertion fails raises
  `HieraLookupError("The convert_to lookup_option for key '<key>' raised
  error: <msg>")` instead — both with the underlying error chained as
  `__cause__`. See "Types" above for the types `new()` never supports.
  `new()` checks its arguments as Puppet does: `Integer`'s optional `radix`
  and `abs` (or the `{from, radix, abs}` hash), `Float`/`Numeric`'s `abs`
  (or `{from, abs}`), the argument count per type, and number strings
  against Puppet's anchored patterns (`'12\n'`, `'inf'`, `'1_000'` are
  refused). A `String` format is exactly one `%<flags><width>.<prec><char>`
  directive following Puppet's per-type tables (negative `%x` is `..f01`,
  a Hash prints `{'a' => 1}`); anything else is the "not a valid format" or
  "Illegal format '<c>' specified for value of <Type> type" error.
- `HOCONBackend`'s `include` handling matches Puppet's own `hocon_data`
  by default (see "Backends" above): `include file(...)` really reads the
  named file (cwd-relative or absolute), and a directive in value position
  (`msg = please include "x"`) is kept as literal text. The stricter
  pre-fidelity behaviour (raise on both) is kept as the
  `hocon_includes=False` opt-in. One measured, accepted divergence:
  `include file("*.conf")` globs under pyhocon's own resolution where
  Puppet's never does.
