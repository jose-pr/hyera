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

- **`Hiera(base_config, backends=None, base_path=None, *, scope=None,
  environmentpath=None, basemodulepath=(), modulepath=None,
  cache_size=256, revalidate=True)`**
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
  built inside one `Hiera(...)` call shares it too.
  `self.scope` is set before the config loads, so a hierarchy path template
  referencing it (`%{trusted.certname}`, `%{environment}`) resolves against
  it from the first, context-free pre-warm onward. A missing or `null`/`false`
  `defaults`/`hierarchy` is filled
  with Puppet's own defaults (`{datadir: data, data_hash: yaml_data}` /
  `[{name: Common, path: common.yaml}]`) rather than raising; a hierarchy
  entry's own `datadir` wins, else `defaults.datadir`, else the literal
  `data`, always resolved next to hiera.yaml (or under `base_path`) — never
  the Hiera 3 absolute `/etc/puppetlabs/...` path. `default_hierarchy` is
  accepted only in a *module*'s own hiera.yaml (see "Layers" below); the
  same key in the global or an environment config raises `ConfigError`
  ("'default_hierarchy' is only allowed in the module layer"), at
  validation time. Where it is accepted, its entries are schema-validated
  exactly like `hierarchy`'s. Each entry uses its own function
  key (`data_hash`/`lookup_key`/`data_dig`/`hiera3_backend`/
  `v4_data_hash`), falling back to `defaults` only when the entry names
  none (`defaults` is never merged into an entry wholesale). A registered
  `lookup_key`/`data_dig` function is called per key and per location
  through a `hyera.LookupContext` (see Backends below); naming a function
  that does not implement the requested kind raises `ConfigError` with
  Puppet's own arity/parameter-type text ("Unable to find" for an
  unregistered name). A hierarchy entry with no location key at all calls
  its function once, with no location, instead of contributing nothing.
  Raises `ConfigError` for anything about `hiera.yaml` — missing,
  unreadable, a directory, unparsable, an unsupported `version` (an
  explicit `1`, `2`, `6` or other non-3/4/5 value as "This runtime does not
  support hiera.yaml version N"; `4` at the global layer as "cannot be used
  in the global layer"), or any violation of Puppet's schema for the
  resolved version — with Puppet's own message and, where known, `.path`
  and `.line` — and `BackendError` (`.path` names it) for a data file that
  cannot be read or parsed. Context-free hierarchy levels are loaded by the
  constructor, so a `BackendError` can come from `Hiera(...)` itself, not
  only from a lookup.
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
    (the `v3-ruby-backend-unavailable` conformance deviation). A relative
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
    Puppet" in the README. `basemodulepath` defaults to `()`. `modulepath`,
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
  - **`.lookup(name, value_type=None, merge=None, default_value=<unset>, *,
    default_values_hash=None, override=None, block=None)`** — Puppet's
    `lookup()`, against the instance's bound scope. Five equivalent call
    forms: `lookup("k")`; `lookup("k", "Integer")`; `lookup("k", "Integer",
    "first", 0)` (every positional argument); `lookup({"name": "k", "merge":
    "first"})` (a single dict in place of everything else — `"name"` is
    required, and every other positional argument/option keyword must be
    omitted); `lookup("k", {"merge": "first"})` (name positional, every
    other option in a dict passed as `value_type`). Every option name also
    works as a keyword (`lookup("k", merge="first")`); combining an options
    dict (either form) with another positional argument or an option
    keyword raises `TypeError` — `block` is the one exception, accepted
    alongside either dict form. `name`: a `str`, or a `list` of `str` tried
    in order (a `tuple` raises `TypeError`: `h["a", "b"]` must mean `(name,
    value_type)`, so a name list has to be a `list`). `value_type`: a Puppet
    type expression string (`"Integer"`, `"Optional[String]"`); every
    candidate value (an override, a found value, a default) is asserted
    against it, raising `HieraLookupError` on a mismatch with Puppet's own
    subject text ("Found value has wrong type, …", "Default value has wrong
    type, …", "Value found for key '<k>' in override hash has wrong type,
    …", "… in default values hash has wrong type, …", "Value returned from
    default block has wrong type, …"). `merge`: as `lookup_options`'
    `merge` (see below) — an explicit `merge=` overrides only the merge
    `lookup_options` would have picked; an applicable `convert_to` still
    runs. `default_value`: returned (after `value_type`) when nothing else
    was found; **omitted entirely** means no default at all — passing
    `None` explicitly is a real default that beats a miss (and a found
    `None` beats even that). `default_values_hash`: a dict tried, per name,
    only after the whole hierarchy missed every name. `override`: a dict
    consulted, per name, *before* the hierarchy — a hit here returns
    immediately, `convert_to` included, never touching the hierarchy at
    all; both `override` and `default_values_hash` also feed `%{var}`
    interpolation inside any value looked up during the same call (never a
    hierarchy location, which interpolates against the scope alone).
    `block`: called with `name` exactly as given (a list stays a list) when
    nothing else was found, before `default_value`; its return value is
    asserted against `value_type` too. Precedence, per name in order:
    `override` → the hierarchy (`lookup_options`, `default_hierarchy`,
    `convert_to` all apply) → (next name) → `default_values_hash` (every
    name again) → `block` → `default_value` → `KeyNotFoundError` (also a
    `KeyError`), naming every name that was tried ("… for the name 'x'" for
    one, "… for any of the names [...]" otherwise, including an empty
    list). A non-`str`/non-`list` `name`, a `tuple`, an unparsable
    `value_type`'s call shape, an empty-string `merge`, a non-callable
    `block`, or an unknown/malformed option raises `TypeError`. Also
    reachable as `h(...)` (`__call__`, identical to `.lookup(...)`) and
    `h[...]` (`__getitem__`: a `tuple` unpacks into `.lookup(*item)`,
    anything else becomes the sole `name` argument — so `h["a", "b"]` is
    `lookup("a", "b")`, not a two-name list). `name in h` (`__contains__`)
    is `True` unless `.lookup(name)` raises `KeyNotFoundError` — any other
    error (a malformed key, a type mismatch) propagates, same as
    `.lookup()`. `iter(h)` raises `TypeError` (`__iter__ = None`): a `Hiera`
    is not a sequence, even though it defines `__getitem__`.
  - **`.dig(*keys, value_type=None, merge=None, default_values_hash=None,
    override=None) -> Any`** — Puppet's `dig()`: looks up `keys[0]` via
    `.lookup()` (`merge`/`default_values_hash`/`override` apply to that
    root lookup, exactly as they would to `.lookup()` itself), then digs
    the rest of `keys` out of it Ruby `Hash#dig`/`Array#dig`-style. A miss
    on `keys[0]` gives `None` — unlike `.lookup()`, `.dig()` never raises
    `KeyNotFoundError`. A later key that is not an `int` against a `list`,
    or any key against a value that is not a collection, raises
    `HieraLookupError` naming the path walked so far and the Puppet type
    found ("The given data requires an Integer index at […], got '…'" /
    "The given data does not contain a Collection at […], got '…'"). A
    `list` index follows Ruby's negative/out-of-range rules (negative
    counts from the end; out of range is `None`); a `dict` key matches
    only a key of the identical kind (`True` is never `1`, `1` is never
    `1.0`). `value_type`, when given, asserts the final result with the
    subject "Found value". Needs at least one key, the first a `str`, else
    `TypeError`.
  - **`.get(dotted, default_value=None, block=None, *, value_type=None,
    merge=None, default_values_hash=None, override=None) -> Any`** —
    Puppet's `get()`: `dotted` is a single dotted-navigation *string*
    (`"a.b.0"`), **not** the removed old `.get(key, ...)`'s plain key. The
    root segment resolves like `.lookup()` (an `int` root can never match a
    hiera key, so it is treated as a miss without a lookup at all); a root
    miss or a found `None`, or any navigation past it landing on `None`,
    returns `default_value` — never raises for that reason. A walk error
    (the same two `HieraLookupError`s `.dig()` raises) reaches `block(error)`
    when a block is given, else raises. `dotted` must be a non-empty `str`
    (there is no whole-data value to return), else
    `HieraLookupError("Syntax error in dotted-navigation string")`, the
    same error a malformed one raises; a non-`str` `dotted` raises
    `TypeError` instead. `value_type` asserts the final result with the
    subject that says where it came from ("Found value", "Default value"
    or "Value returned from block").
  - **`.getvar(dotted, default_value=None, block=None) -> Any`** — Puppet's
    `getvar()`: `.get()`'s own navigation, over a scope variable's value
    instead of a looked-up one. `dotted` must start with a valid
    (optionally `::`-qualified) Puppet variable name immediately followed
    by `.` or the string's end, else `HieraLookupError("'getvar' The given
    string does not start with a valid variable name")`. An undefined
    variable returns `default_value` regardless of the bound scope's
    `strict` — never raises for that alone.
  - **`.explain(name, value_type=None, merge=None, default_value=<unset>, *,
    default_values_hash=None, override=None, block=None,
    explain_options=False) -> ExplainResult`** — what `puppet lookup
    --explain`/`--explain-options` shows: takes exactly `.lookup()`'s own
    signature and dispatcher (the same five call forms, the same keyword
    spellings). `explain_options=True` mirrors `--explain-options`: only
    how `lookup_options` was assembled for `name` (and its own module, if
    qualified) is reported; combined with an otherwise-normal call it is
    Puppet's `--explain --explain-options`, byte-identical to
    `explain_options=False`. Always returns an `ExplainResult`:
    `.text()` is the indented report (every hierarchy entry and path
    consulted, `Path not found`, `No such key`, `Found key`, merges and
    their results, interpolations and sub-keys, the `lookup_options`
    search, `default_hierarchy`); `.to_hash()` is the same tree, projected
    with Puppet's own keys (`branches`, `type`, `key`, `value`, `event`,
    `name`, `path`, `original_path`, ...) — a fresh `copy.deepcopy` on
    every call, so mutating the result never reaches a later `.explain()`/
    `.lookup()`. `.error` is the `HieraError` the lookup ended with, or
    `None`. An error `puppet lookup --explain` prints as its own last line
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
  - **`.scoped(*, variables=None, facts=None, trusted=None,
    server_facts=None, environment=None, strict=None, node_name=None) ->
    Hiera`** — a *view*: `self._view(self.scope.derive(...))` builds a new
    `Hiera` (via `object.__new__` plus a `__dict__` copy, not a proxy) that
    shares this instance's config, backends and all three caches (`.cache`,
    `._source_cache`, `._lookup_options_cache` — already keyed on the scope
    value, so sharing them is safe) with the derived scope bound in place
    of `self.scope`. Every method — `.lookup`/`()`/`[]`/`in`, `.sources()`,
    `.format()` — then reads the view's own scope. `.scoped(...)` layers:
    calling it again on a view derives from *that* view's scope, not the
    original instance's, so nested calls compose. The original instance's
    own scope, and any other existing view, are never affected.
  - **`.sources() -> list`** — resolve+load the ordered candidate source
    paths for the bound scope (cached per scope value; a fresh `Hiera`
    instance if the on-disk tree may have changed).
  - **`.format(text) -> Any`** — interpolates `text` exactly as a data
    value is interpolated (Puppet's `Context#interpolate`): all five
    methods, whitespace inside `%{ }` ignored, literal braces untouched,
    undefined variables per the scope's `strict`. Returns a `str`, except
    that a `text` that is exactly one `%{alias('k')}` returns `k`'s value.
    Raises `TypeError` for a non-`str` `text`.
  - Coming from `hiera()`/`hiera_array()`/`hiera_hash()`/`hiera_include()`
    (Puppet's legacy functions always force a merge, ignoring
    `lookup_options`): `hiera(k[, d])` → `h.lookup(k, None, "first"[, d])`;
    `hiera_array(k)` → `h.lookup(k, None, "unique")`; `hiera_hash(k)` →
    `h.lookup(k, None, "hash")`; `hiera_include(k)` has no equivalent (it
    applies classes to a catalog, which hyera has no notion of) — none of
    the four are implemented as methods; use `.lookup()` directly.
  - **`.clear_cache() -> None`** — drops every cached location,
    `lookup_options` mapping, glob listing and parsed data file; the next
    lookup re-reads whatever it needs from disk. Safe to call while other
    threads are looking things up on this instance or a `.scoped(...)` view
    of it (they share every cache). There are no public cache attributes to
    inspect or clear individually.
  - Gotcha: parsed data files are cached per `(path, strict)` for the
    instance's life — a YAML file's own non-hash validation is
    `strict`-sensitive, so the same file can be cached independently under
    two different `strict` values — and unbounded, like Puppet's own
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
    only when a directory it walked has itself changed — one probe per
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
    process's working directory): the parsed-file cache, the location and
    `lookup_options` caches (and the lock they share), and the glob-listing
    cache do NOT survive a copy — each starts empty, so the next lookup
    re-reads every data file (re-decrypting sops plaintext along with it)
    and rebuilds whatever else it needs (`Scope`'s own warning-dedup state
    is NOT carried over verbatim either — its internal lock cannot be
    pickled, so a copy starts with the same dedup keys but a fresh,
    unlocked mutex). The same is NOT true of an `eyaml_lookup_key`/other
    `lookup_key`/`data_dig` hierarchy entry: its result (decrypted
    plaintext included) lives in the view's own per-provider
    `LookupContext` cache (see the Gotchas section below), which is a
    plain instance attribute `copy.deepcopy`/`pickle` copies right along
    with the rest of the instance, unlike the caches above.
    Concurrent `.lookup()` calls on one instance (or its views) from
    multiple threads are safe on GIL builds, where they only mutate the
    shared caches under one lock per instance (untested on free-threaded
    builds).
- **`HieraLevel`** (`NamedTuple`: `name`, `backend`, `datadir`,
  `location_key`, `locations`, `kind`, `options`) — one hierarchy entry,
  stored exactly as written in hiera.yaml (`locations`/`options` are never
  interpolated or normalized here). `.new(conf, backend, kind="data_hash")`
  builds one from a hierarchy dict (`location_key` is the first of
  `path`/`paths`/`glob`/`globs`/`uri`/`uris`/`mapped_paths` present, or
  `None`; `locations` is that key's raw value(s) — one string for a
  singular key, the declared tuple for a plural one, or `(collection_var,
  item_var, template)` for `mapped_paths`; `kind` is the resolved function
  kind, `"data_hash"`/`"lookup_key"`/`"data_dig"`; `options` is the entry's
  own `options`, else `defaults`'s, uninterpolated).
  `.paths(base_path, scope) -> list[Path]` resolves candidate source
  *file* paths for a bound `Scope`, through the same `%{...}` engine as
  data values (`allow_methods=False`): an undefined variable interpolates
  as `''` plus the scope's `strict`-mode warning and the resulting path is
  still probed, **never** a skipped level; `datadir` interpolates
  separately, under the scope's `strict` (raises under `"error"`, unlike a
  location itself, which is always lenient); method-call syntax
  (`%{lookup(...)}` etc.) raises `ConfigError` in any of these positions.
  A location-less entry, or one using `uri`/`uris`, contributes no paths
  here (`[]`) -- a `uri` is never a filesystem path.
  `hyera._location_resolver.resolve_locations(level, base_path, scope)`
  (private; `.paths` is its body) returns `None` for a location-less entry
  (distinct from a location key that itself expands to zero candidates,
  `[]`) or a list of `ResolvedLocation(original, location, is_uri, exist)`.
  A `uri`/`uris` location is validated against Ruby's `URI()` RFC 3986
  grammar and normalized like `URI#to_s` (lowercase scheme; drop an empty
  or default port for `http`/`ws` (80), `https`/`wss` (443), `ftp` (21) and
  `ldap` (389); percent-encode a literal space in the query as `%20`) --
  `ConfigError("bad URI (is not URI?): <Ruby-inspected text>")` on a
  malformed one; `.exist` is always `True` for a `uri` (never fetched or
  stat'ed), and `.location` is the normalized string, not a `Path`. A
  `mapped_paths` collection is a
  scope reference (dotted, `::`-qualified) — `None`/`""`/an empty
  Array/Hash contributes no paths, a `String` becomes a one-element list, an
  Array is used as-is, a Hash contributes its `[key, value]` pairs; a
  Boolean/Integer/Float collection raises `ConfigError`. Each item binds as
  one local-scope variable layer (`%{item}` reads it; `%{::item}` still
  reaches a top-scope variable of the same name, bypassing the local
  layer). A `path`/`paths`/mapped location that names a directory raises
  `BackendError` ("Is a directory") when loaded, instead of reading its
  files; a glob match that is a directory is dropped instead. A `glob`/
  `globs` location matches through hyera's own Ruby `Dir.glob` port
  (`_location_resolver.glob`), never `pathlib_next.Path.glob`: `{a,b}`
  brace alternation (nested, in written order, duplicates kept); a
  dotfile matches only an explicit leading `.` in the pattern, never a
  bare `*`/`?`/`[...]`; `**/` never descends through a symlink or a
  Windows junction, and a trailing `**` is plain `*`; `\` escapes a
  metacharacter in the pattern text; each directory's entries sort in
  byte order and every wildcard is case-sensitive, on every OS; a missing
  or unreadable directory contributes nothing; `datadir`'s own glob
  metacharacters are live for a glob level (a literal directory for a
  `path`/`paths`/mapped one).
- **`Sensitive(value)`** — redacting wrapper produced by `convert_to:
  Sensitive`, mirroring Puppet's `Sensitive` type (`p_sensitive_type.rb`).
  `str()`/`repr()` both show `Sensitive [value redacted]`; `.unwrap()`
  returns the real value. Equality and hashing follow Puppet: two
  `Sensitive` values are equal (and hash equal) exactly when their wrapped
  values are Ruby-`eql?` — `1`, `1.0` and `True` are distinct wrapped
  values, but a list or dict payload compares/hashes by content (in any key
  order for a dict) despite being unhashable in plain Python.
- **`ExplainResult`** — `Hiera.explain(...)`'s return value; see `.explain`
  above for the full contract. `.to_hash() -> dict`, `.text() -> str`
  (also `str(result)`), and the read-only property `.error ->
  Optional[HieraError]`. Never constructed directly.

## Scope (`_scope.py`)

Puppet's top scope, as one immutable, hashable value, bound to every
`Hiera` instance, views included (`Hiera(..., scope=...)`, `.scope`,
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
- **`Backend(conf=None, *, strict=None)`** — `.conf`. `.strict`
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
  `._require_path_only(path, options)` then `._as_data_hash(self.load(path),
  path)`); `._as_data_hash(parsed, path)` adapts a parsed document into
  hiera data (base: identity; `YAMLBackend` overrides it for the non-Hash
  rule). `._require_path_only(path, options)` — Puppet's
  `Struct[{path=>String[1]}]` contract every built-in file function
  (`yaml_data`/`json_data`/`hocon_data`/`sops_data`) follows: raises
  `ConfigError("'<name>' one of 'path', 'paths' 'glob', 'globs' or
  'mapped_paths' must be declared in hiera.yaml when using this data_hash
  function")` when `path is None` or `options` carries anything besides the
  `path` key `data_hash` itself received (a `uri` location, or any
  user-declared hierarchy option) — before any file is read.
  `.lookup_key(key, options, context)` / `.data_dig(key_segments, options,
  context)` raise `NotImplementedError` in the base; a backend that
  overrides either is called per key and per location with a
  `hyera.LookupContext` as `context` (below). Neither hook's return value is
  interpolated by the engine — call `context.interpolate(value)` yourself;
  signal a miss with `context.not_found()`, never a sentinel return value.
  `options` carries `path` (a `str`) or `uri` (interpolated and normalized
  like Ruby's `URI#to_s`, never fetched) for a located entry, or neither
  for a location-less one — the same mapping a `data_hash` hook receives.
- **`LookupContext`** (in `hyera` and `hyera.backends`; Puppet's public
  `Context`, `pops/lookup/context.rb:126-206`) — the `context` argument a
  `lookup_key`/`data_dig` hook receives, one per hierarchy entry and
  location, scoped to the `Hiera`/`h.scoped(...)` view the lookup runs
  against (never shared with another view, same as the provider itself):
  - `.interpolate(value)` — `%{...}` interpolation with method calls
    allowed, against the current lookup's scope.
  - `.not_found()` — raises internally (a `BaseException` subclass, so a
    backend's own `except Exception:` cannot swallow it); never returns.
  - `.explain(producer)` — a no-op until an explain facility exists;
    `producer` (a zero-argument callable) is never invoked.
  - `.cache(key, value)` / `.cache_all(mapping)` / `.cache_has_key(key)` /
    `.cached_value(key)` (`None` when absent) / `.cached_entries()` (an
    iterator of `(key, value)` pairs) — a per-location cache private to this
    hierarchy entry, living for the bound view's lifetime; a
    `lookup_key`/`data_dig` result a hook itself cached (`.cache(key, ...)`)
    is returned from that cache on a later call for the same key, and a copy
    (never the cached object) leaves the cache each time, so a caller
    mutating a returned list/dict never corrupts it. A miss
    (`context.not_found()`) is never cached — a later call for the same key
    calls the hook again.
  - `.cached_file_data(path, parse=None)` — reads and, if `parse` is given,
    parses `path` once, revalidated by `(inode, mtime_ns, size)` on every
    call (not by content); shared by every hierarchy entry on the same
    `Hiera` instance (like the `data_hash` file cache), never per-location.
  - `.environment_name` (the scope's `environment`, `"production"` when
    unset) / `.module_name` (always `None` here — a future layers plan
    passes its module name through).
- **`default_backends() -> list[type[Backend]]`** — the distinct classes
  registered in the `function` namespace, in definition order:
  `[YAMLBackend, JSONBackend, HOCONBackend, SopsBackend]`.
  `Hiera(backends=...)` takes this same kind of list as an allow-list; a
  `data_hash` name whose registered class is not in it is refused exactly
  like an unknown name.
- **`YAMLBackend`** — `NAMES = {"function": ("yaml_data",), "format":
  ("yaml",)}`, `EXTENSIONS = (".yaml", ".yml")`. (The `render`-kind `yaml`
  name belongs to `hyera._render.YAMLRender`, a separate class -- see
  "Rendering" below.)
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
  ("json",)}`, `EXTENSIONS = (".json",)`. (The `render`-kind `json` name
  belongs to `hyera._render.JSONRender`, a separate class -- see
  "Rendering" below.) `.loads`
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
- **`EyamlBackend`** — `NAMES = {"function": ("eyaml_lookup_key",)}`, a
  `lookup_key` provider, Puppet's own name for hiera-eyaml. Requires the
  optional `hyera[eyaml]` extra (`cryptography`); `check_available()`
  raises `BackendError` naming the extra when it is missing, at level
  build via `Backend.new`, the same shape as the `hocon_data`/missing-
  `pyhocon` message. **PKCS7 only** — the private key alone is needed (no
  certificate); GPG or any other hiera-eyaml encryptor plugin raises
  hiera-eyaml's own `LoadError` text, unwrapped, naming
  `hyera[eyaml]`-only support. Options: `pkcs7_private_key` (a path
  **relative to the process's current working directory**, not
  `base_path` or the data file's own directory — matches hiera-eyaml
  itself), `pkcs7_private_key_env_var` (wins over the plain path, with a
  logged warning if both are set), `pkcs7_b64_private_key_env_var` (wins
  over the plain path silently, base64-decoded with Ruby's lenient
  `Base64.decode64` rules); `pkcs7_public_key*` options are accepted
  (Puppet's own schema has them) but never read. The raw `.eyaml` file's
  own non-Hash rule matches `yaml_data`'s (reads `self.strict` fresh on
  every read, never cached, so a later call under a different `strict`
  sees its own rule — the same trap `Hiera._load_file`'s cache key
  already guards against for `data_hash`). A decrypted value is
  interpolated (methods allowed) exactly like a `data_hash` result — a
  Hash's **keys** are interpolated but never decrypted, only its values
  recurse; every other type (int/float/bool/None) passes through
  unchanged. Decrypt failures raise `BackendError("hiera-eyaml backend
  error decrypting <token> when looking up <key> in <path>. Error was
  <message>")`; a missing `path`/`uri` location raises `ConfigError` like
  any other `lookup_key` function. `hyera._eyaml` (private) holds the
  token grammar, key loading and a bounds-checked BER/DER reader that
  decrypts PKCS7 `EnvelopedData` (RSA PKCS#1 v1.5 key transport,
  AES-128/192/256-CBC content, definite or indefinite lengths, no
  certificate parsing) — any wrong-key/garbled-ciphertext symptom (wrong
  key, bad padding, implicit-rejection garbage) reports as `"bad
  decrypt"`, OpenSSL's own text for exactly that case, never a
  distinguishable error.
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
  - **`KeyNotFoundError`** (also a `KeyError`) — `.lookup()`'s miss (no
    default given), with Puppet's message ("Function lookup() did not find
    a value for the name '<key>'", or the "any of the names [...]" plural
    form). `.name` holds the key(s) tried.

Every class above is importable directly from `hyera` (e.g. `hyera.BackendError
is hyera.backends.BackendError`, both paths work since `hyera.__init__`
re-exports it too).

## Rendering (`_render.py`)

Three `render`-kind-only `Backend` subclasses -- Puppet's `puppet lookup
--render-as` output, found via `Backend.new(fmt, kind="render")`. Each
implements only `dumps(obj) -> str`; none of the Hiera 5 provider hooks or
`loads` apply. Imported once, for its side-effect registration, right
after `backends` in `hyera/__init__.py`.

- **`StringRender`** (`s`) — `hyera._interpolation._to_puppet_str`: Ruby
  `to_s`, the same renderer a bare `%{var}`/function-call result uses
  (Ruby 3.2 AIO hash form `{"k"=>v}`, `Sensitive [value redacted]`).
- **`JSONRender`** (`json`) — `json.dumps(ensure_ascii=False,
  allow_nan=False, separators=(",", ":"))` over a projected value: a
  `Sensitive` becomes its redacted text, a `dict` keeps insertion order (a
  non-`str` key renders through `_to_puppet_str`), a `list`/`tuple`
  becomes a plain `list`. A non-finite `float` raises `ValueError` with
  Puppet's own text (`NaN not allowed in JSON`, `Infinity not allowed in
  JSON`, `-Infinity not allowed in JSON`); anything else not representable
  as Puppet data raises `TypeError("<type name> is not a Puppet data
  value")`.
- **`YAMLRender`** (`yaml`) — a `yaml.SafeDumper` subclass
  (`explicit_start=True, default_flow_style=False, sort_keys=False,
  allow_unicode=True`), byte-compatible with Psych's `to_yaml` for every
  measured shape: `None` renders as an empty plain scalar; a `str` uses
  literal style (`|`) when it contains `\n`, double-quoted style for
  exactly `y`/`Y`/`n`/`N` or text matching `^:.` (Psych quotes these,
  PyYAML's own resolver does not), else PyYAML's own default quoting
  rules; a `Sensitive` renders as its redacted text; a `tuple` renders as
  a list. A trailing `...` document-end line, when PyYAML adds one, is
  stripped. Every render ends with exactly one trailing `\n`.

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
  `deep`), `knockout_prefix` (`--knockout-prefix`), `render_as`
  (`--render-as FORMAT`, default `None` meaning `"yaml"`; case-insensitive;
  an unrecognized format exits 2 with `Unknown rendering format '<f>'`
  before any lookup runs), `default` (`--default`). Output goes through
  `hyera._render`'s `s`/`json`/`yaml` render backends (`Backend.new(fmt,
  kind="render")`), the same shapes `puppet lookup --render-as` prints
  (Ruby `to_s` for `s`, byte-compatible YAML for `yaml`, compact
  insertion-ordered JSON for `json`); a `Sensitive` value redacts in every
  format, including `yaml` (Puppet's own YAML leaks the plaintext). A
  non-finite float under `--render-as json` exits 2 with Puppet's own text
  (`NaN not allowed in JSON`, `Infinity not allowed in JSON`, `-Infinity
  not allowed in JSON`). Output is written as UTF-8 bytes with LF line
  endings via `_emit` (never `print`), regardless of the console or locale
  encoding, with a trailing newline added only if the rendered text lacks
  one (Ruby `puts` semantics); a reader that closes the pipe early raises
  `BrokenPipeError`, silenced and reported as exit 2 with nothing on
  stderr. Omitting `--merge` lets the data's
  `lookup_options` decide (else first-match-wins); an explicit `--merge`,
  `first` included, always overrides `lookup_options`. Exit codes: `0`
  found (or `--default` printed), `1` the key was not found (a
  `KeyNotFoundError` and nothing else), `2` any other error, an unknown
  render format, an unrenderable value or a closed output pipe. A `2` from
  a lookup or render failure logs exactly one `hyera`-logger ERROR line:
  `Lookup of key 'K' failed: …` for a lookup failure (construction
  included), `Cannot render the value of key 'K': …` if printing the
  found/default value itself fails. The traceback is omitted unless `-v`
  or `DUHO_TRACEBACK=1` is set.
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

- Debug trace: with the `hyera` logger at `DEBUG` (and a handler attached),
  each `lookup`/`dig`/`get`/`explain` call logs one record on the
  `hyera._explain` logger: `Lookup of '<key>'` followed by the same report
  `explain()` returns, each line indented two spaces. Each
  `%{lookup()}`/`%{hiera()}`/`%{alias()}` encountered while resolving a
  value logs its own record first, in the order it actually runs. With
  `DEBUG` off (the default), nothing is recorded and no explain tree is
  built at all — checked once per top-level call, not once per hook.
- The engine interpolates a `data_hash` value (methods allowed) but never a
  `lookup_key`/`data_dig` result — a backend that wants interpolation calls
  `context.interpolate(value)` itself. `lookup_key`/`data_dig` providers and
  their `LookupContext` caches are per scope-binding object (`Hiera`/
  `h.scoped(...)` view), never shared with another view; `cached_file_data`
  is per `Hiera` instance, shared by every hierarchy entry. A backend's
  `context.not_found()` raises a `BaseException` subclass
  (`hyera._function_provider._NotFound`), not `Exception` — a backend
  wrapping its own logic in `except Exception:` does not accidentally
  swallow it.
- A self- or mutually-referencing interpolation (`%{lookup('a')}` inside
  `a`; a variable whose value refers to itself; a chain `a` -> `b` -> `a`)
  raises `InterpolationError` "Recursive lookup detected in [a, b]" (the
  keys/scope-references visited, in the order first reached) instead of
  Python's own `RecursionError`.
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
  (`(?<name>…)`, `\A`, `\z`, `\h`/`\H` and a lookbehind all work), match by
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
  `split_key`/`sub_lookup` sub-key grammar, in hierarchy paths, `datadir`,
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
