# hyera

A small, dependency-light Python implementation of [Puppet
Hiera](https://www.puppet.com/docs/puppet/7/hiera.html) hierarchical data
lookup. It reads a Hiera base config, walks the hierarchy for a given context,
and fully resolves values — including `%{...}` interpolation and the
`hiera`/`lookup`/`scope`/`literal`/`alias` functions — with optional array,
hash, and deep-hash merging.

## Install

```sh
pip install hyera          # library only
pip install hyera[cli]     # + the `hyera` command-line tool (via duho)
pip install hyera[eyaml]   # + eyaml_lookup_key (PKCS7) support
```

The PyPI distribution, the import package and the command are all named
`hyera`.

## Library

```python
from hyera import Hiera, Scope

h = Hiera("hiera.yaml", scope=Scope(facts={"os": {"family": "Debian"}}, environment="production"))

# First match wins:
h.lookup("ntp::servers")

# Merge across the whole hierarchy:
h.lookup("classes", merge="unique")          # flatten + dedupe arrays
h.lookup("users", merge="deep")              # deep hash merge

# Missing keys raise KeyNotFoundError (also a KeyError) unless a default is given:
h.lookup("missing", default_value="fallback")
"some::key" in h

# A Hiera is callable, and h[...] takes lookup()'s own arguments:
h("ntp::servers")
h["classes", None, "unique"]

# Bind a derived scope once and reuse -- a view, sharing config and caches:
prod = h.scoped(environment="production")
prod["ntp::servers"]
```

Coming from `hiera()`/`hiera_array()`/`hiera_hash()` -- Puppet's legacy
functions always force a merge, ignoring `lookup_options`; `merge="first"`
below is that forcing, not merely "the default":

| Puppet | hyera |
| --- | --- |
| `hiera('key')` | `h.lookup('key', None, 'first')` |
| `hiera('key', 'default')` | `h.lookup('key', None, 'first', 'default')` |
| `hiera_array('key')` | `h.lookup('key', None, 'unique')` |
| `hiera_hash('key')` | `h.lookup('key', None, 'hash')` |
| `hiera_include('key')` | not supported (applies classes to a catalog) |

### Scope and facts

`Scope` is Puppet's top scope: it holds `variables` (node parameters),
`facts`, `trusted` data, `server_facts`, `environment` and `strict`, and is
what every `Hiera` lookup runs against. Precedence for a top-scope
variable name: an explicit `variables` entry wins over a fact of the same
name, which wins over a `server_facts` entry; `$environment` defaults to
`"production"`; `$trusted` defaults to Puppet's local hash (certname taken
from a `clientcert` variable/fact, else empty). Facts are also reachable as
a whole through `$facts`, and `server_facts` through `$server_facts`.

```python
from hyera import Hiera, Scope, load_facts

scope = Scope(facts=load_facts("facts.yaml"), environment="production", strict="error")
h = Hiera("hiera.yaml", scope=scope)
h.lookup("ntp::servers")
```

`load_facts(path)` reads a `puppet lookup --facts`-style file (JSON for
`.json`, YAML for `.yaml`/`.yml`, otherwise JSON then YAML); the result
must be a mapping, and `hostname`/`domain`/`fqdn`/`clientcert` are
all-or-nothing. `facts_from_facter()` runs a bare `facter -j` instead:

```python
from hyera import facts_from_facter

scope = Scope(facts=facts_from_facter())
```

`h.scoped(**derive_args)` returns a `Hiera` view bound to
`h.scope.derive(**derive_args)`, sharing `h`'s config, backends and caches:
`variables`/`facts`/`server_facts` shallow-update the parent scope's own
(new values win, nothing goes stale); `environment`/`strict`/`trusted`/
`node_name` replace the parent's when given.

### Base config

A standard Hiera 5 `hiera.yaml` works. Each level names a `data_hash` backend
and a source (`path`, `paths`, `glob`, `globs`, or `mapped_paths`):

```yaml
---
version: 5
defaults:
  data_hash: yaml_data
  datadir: data

hierarchy:
  - name: "Per-node"
    path: "nodes/%{trusted.certname}.yaml"
  - name: "Per-environment"
    path: "environments/%{environment}.yaml"
  - name: "Per-role"
    mapped_paths: [roles, role, "roles/%{role}.yaml"]
  - name: "Modules"
    globs:
      - "modules/*.yaml"
  - name: "Common"
    path: "common.yaml"
```

Backends (by `data_hash` name — Puppet function names only; see "Differences
from Puppet" below for the one exception):

| Backend        | `data_hash` name | Notes                                          |
| -------------- | ----------------- | ---------------------------------------------- |
| `YAMLBackend`  | `yaml_data`       | parses YAML the way Puppet's Psych does (types, symbols, BOM), on libyaml when available |
| `JSONBackend`  | `json_data`       |                                                 |
| `HOCONBackend` | `hocon_data`      | requires `pip install hyera[hocon]`            |
| `SopsBackend`  | `sops_data` (also `sops`, `sops_<yaml\|json\|ini\|dotenv>`) | decrypts via the `sops` CLI on the fly |

A third-party backend registers itself the same way, by subclassing
`hyera.Backend` and declaring `NAMES`; `Backend.find`/`.get`/`.new`/`.names`
look a backend up by name, and `Hiera(backends=[...])` restricts a lookup to
an explicit allow-list of classes.

`HOCONBackend` resolves `include` directives exactly as Puppet's own
`hocon_data` does by default: a plain `include "file"` contributes
nothing; `include file(…)` really reads the file (relative to the process
working directory, or absolute); a directive in value position (including
inside a `[...]` array) is kept as literal text; `url(…)`, `classpath(…)`,
`required(…)`, `package(…)` and a case-mismatched keyword all raise
`BackendError`, matching Puppet's own parse/method errors for those forms.
Pass `hocon_includes=False` to `HOCONBackend` (or set `hocon_includes:
false` on the hierarchy entry/`defaults` — hyera's own extension, not
Puppet vocabulary) to restore the stricter, pre-fidelity behaviour instead:
every form but a plain quoted include raises, `include file(…)` included.
In either mode, pyhocon's own include-resolving methods stay wrapped as a
fail-closed backstop, so an undiscovered gap in the text scanner still
cannot read a file or reach the network for a form the active mode does
not intend to resolve.

### Layers

`hiera.yaml` above is the *global* layer. Puppet also reads an
*environment* layer and, for a `module::key`-shaped lookup, a *module*
layer — pass `environmentpath`/`basemodulepath`/`modulepath` to `Hiera(...)`
to enable them:

```
.
├── hiera.yaml                          # global
├── data/common.yaml
└── environments/
    └── production/
        ├── hiera.yaml                  # environment (scope.environment)
        ├── data/common.yaml
        └── modules/
            └── mymod/
                ├── hiera.yaml          # module (mymod::* keys only)
                └── data/common.yaml
```

```python
h = Hiera(
    "hiera.yaml",
    environmentpath="./environments",
    basemodulepath="./modules",
)
h.lookup("mymod::setting")  # global, then environment, then mymod's own hiera.yaml
```

The lookup order, at every level, is global then environment then module —
a merge (`merge="unique"`, `merge="deep"`, ...) spans all three. A key not
qualified `<module>::...` never reaches the module layer at all, and a
module's own data that is not qualified with that module's name is dropped
(with a warning) rather than leaking into another module's namespace.
`hiera3_backend` is accepted only in the global layer's hiera.yaml. A
version-3 (or missing-`version`) hiera.yaml at an environment or module root
is silently ignored (with a warning); `puppet lookup`'s own `strict=error`
raises instead. See `src/hyera/AGENTS.md`'s "Layers" entry for the full
discovery and error rules.

A module's own `hiera.yaml` may also declare a `default_hierarchy`
(`default_hierarchy` is rejected everywhere else — global or environment —
with `ConfigError`):

```yaml
# modules/mymod/hiera.yaml
version: 5
hierarchy:
  - name: "Common"
    path: "common.yaml"
default_hierarchy:
  - name: "Module defaults"
    path: "module_defaults.yaml"
```

It is consulted only for that module's own `mymod::*` keys, and only after
every layer (global, environment, the module's own main hierarchy) misses.
The caller's `merge=` does not apply there — the merge comes from the
default hierarchy's own `lookup_options` instead — while the main
hierarchy's `convert_to` still applies to whatever value it returns.

### Merging and `lookup_options`

Pass `merge=` to `lookup()` — one of Puppet's strategy names, or a hash of
deep options:

```python
h.lookup("classes", merge="unique")               # flatten + dedupe arrays
h.lookup("app::name", merge="default")            # explicit first-match
h.lookup("conf", merge="deep")                    # recursive hash merge
h.lookup("conf", merge={"strategy": "deep",       # deep-merge options
                        "knockout_prefix": "--",
                        "sort_merged_arrays": True,
                        "merge_hash_arrays": True})
```

More idiomatically, declare the strategy (and optional `convert_to`) in the
data under the reserved `lookup_options` key — then callers need not pass
`merge=` at all:

```yaml
# common.yaml
classes:
  - base
lookup_options:
  classes:            { merge: unique }
  "^app::.*":         { merge: { strategy: deep } }   # regex: must start with ^
  port:               { convert_to: Integer }
  db::password:       { convert_to: Sensitive }
```

A `lookup_options` key is treated as a regular expression **only when it
starts with `^`** (Hiera 5's rule); every other key is matched literally, so
a key containing `.` or other metacharacters cannot shadow unrelated keys.
Patterns use Ruby regex syntax (`(?<name>…)`, `\A`, `\z`, `\h`/`\H`, a
lookbehind) and match by searching from the start of the key, so `^app::`
matches `app::ports`; they are tried in the merged order, lower-priority
levels' patterns first. An exact key match always wins over a pattern
match. An invalid pattern, or a `lookup_options` value that is not a hash,
raises `HieraLookupError` for the whole lookup. An entry that is a string
applies no options and stops the search (a matching pattern for the same
key is never tried); any other non-hash, non-string entry raises.

With layers configured, `lookup_options` from the global, environment and
module data all apply to the same key — global wins over environment, which
wins over module — and a module's own keys/patterns must start with
`<module>::`.

An explicit `merge=` argument overrides only the *merge* `lookup_options`
would have picked; `convert_to` always applies. `convert_to` takes
a Puppet type string (`Integer`, `Optional[Integer]`) or `[Type, *args]`
(`[Integer, 16]`, `[String, '%x']`) and converts with Puppet's `new()`:
Integer, Float, Numeric, String, Boolean, Array, Hash, Tuple, Struct,
Optional, NotUndef and Sensitive (a redacting `hyera.Sensitive` wrapper). An
invalid type or a failed conversion raises `hyera.HieraLookupError`.

### Navigating values

`dig`/`get`/`getvar` are Puppet's own navigation functions, each ported
onto `Hiera`:

```python
h.dig("db", "credentials", "user")            # None if any step is missing
h.get("db.credentials.user", default_value="admin")   # a dotted navigation string
h.getvar("facts.os.family")                   # reads the bound scope, not the data
```

`dig` looks up its first argument, then walks the rest of them into the
result (a `list` index or a `dict` key at a time), returning `None` the
moment a step is missing rather than raising. `get` takes the same idea as
one dotted string and a `default_value`, plus an optional `block` that
receives a walk error (a non-collection or a non-integer list index)
instead of raising. `getvar` runs `get`'s own navigation over a scope
variable instead of a looked-up key.

### Explaining a lookup

```python
print(h.explain("ntp::servers").text())
```

`explain` takes exactly `lookup`'s own arguments and returns a
`hyera.ExplainResult`: `.text()` is the indented report `puppet lookup
--explain` prints (every hierarchy entry and path consulted, merges and
their results, interpolations, the `lookup_options` search); `.to_hash()`
is the same tree, keyed the way `--render-as json --explain` renders it.
`explain_options=True` reports only how `lookup_options` was assembled.

## Command line

```sh
hyera KEY [options]

hyera ntp::servers --config hiera.yaml --scope environment=production
hyera classes --merge unique --output json
hyera missing::key --default '(none)'
```

Options: `--config/-c`, `--scope key=value` (repeatable), `--merge
first|unique|hash|deep` (`array`/`set` alias `unique`), `--deep`,
`--knockout-prefix`, `--output/-o raw|json|yaml`, `--default`, plus duho's
`-v/-q/--loglevel`. Without `--merge`, the data's `lookup_options` decides;
an explicit `--merge`, `first` included, overrides it.

The CLI needs the `cli` extra (`pip install "hyera[cli]"`); without it the
command prints that hint and exits 2.

The CLI is built for unattended use: no interactive prompts, deterministic
output, and meaningful exit codes: `0` found (or `--default` printed), `1`
key not found, `2` any other error — reported as one stderr line (`-v` or
`DUHO_TRACEBACK=1` adds the traceback). `puppet lookup` exits `1` for both a
miss and an error, printing nothing for the error case; hyera's CLI tells
the two apart.

`HYERA_MCP=stdio hyera` runs the same command as an MCP server over
stdin/stdout, so an MCP client can drive lookups: it exposes one tool,
`hyera`, whose arguments are the command-line fields (`key`, `config`,
`scope`, `merge`, ...) and whose result is what the command would print.

## sops and unattended runs

`SopsBackend` (`data_hash: sops_data`) shells out to `sops` to decrypt a
level on the fly. The format (YAML, JSON, INI or dotenv) is inferred from
the file's extension the same way the `sops` CLI itself picks it
(`.yaml`/`.yml`/`.json`/`.env`/`.ini`, case-sensitive); any other
extension is a clear error, since `sops` would read that file as binary.
It is hardened so an automated lookup never hangs, dies opaquely, or
leaks a decrypted secret:

- a finite subprocess timeout (`hyera.backends.SOPS_TIMEOUT`, default 30 s),
- captured stderr surfaced in a `BackendError`,
- a clear error when the `sops` binary is not on `PATH`,
- the data file is passed to `sops` as an absolute path after a literal
  `--`, so a level or scope value that starts with `-` can never be read as
  a `sops` option,
- the `sops` found on `PATH` is the one executed, by its full resolved
  path; a `sops.bat`/`sops.cmd` shim is refused (`cmd.exe` re-parses a
  batch file's argument line, which a data-derived path could abuse),
- a decrypted file that fails to parse reports only the problem and its
  line/column — never the decrypted plaintext; a YAML value shaped to
  quote itself into the error message (`!!float`, `!ruby/object:...`) is
  redacted instead,
- an INI file is always decrypted through sops's own JSON view, never
  ini text — sops's INI writer can otherwise emit a value that a text
  parser reads as a different key or an injected section.

## eyaml_lookup_key

`EyamlBackend` (`lookup_key: eyaml_lookup_key`) decrypts hiera-eyaml's
`ENC[PKCS7,...]` values, behind the optional `hyera[eyaml]` extra
(`cryptography`). **PKCS7 only** — the private key alone is needed, no
certificate:

```yaml
hierarchy:
  - name: "secrets"
    lookup_key: eyaml_lookup_key
    path: "secrets.eyaml"
    options:
      pkcs7_private_key: "keys/private_key.pkcs7.pem"
```

- `pkcs7_private_key`, `pkcs7_private_key_env_var` and
  `pkcs7_b64_private_key_env_var` follow hiera-eyaml's own precedence (env
  var beats a plain path, base64-env-var beats both); `pkcs7_public_key*`
  options are accepted but never read.
- A relative `pkcs7_private_key` resolves against the **process's current
  working directory**, exactly like hiera-eyaml itself — not `base_path`
  and not the data file's own directory.
- Other hiera-eyaml encryptors (GPG and third-party plugins) are not
  supported; a value using one raises the same "cannot load such file"
  error Puppet itself gives without that plugin installed.

## Hiera 5 spec coverage

Supported: `version: 5` validation · Puppet's version 5 schema validation
(closed key sets, a required unique `name`, at most one function/location
key, non-empty strings, the `options` name pattern, and the rest) ·
`defaults` · `hierarchy` · `name` ·
`path`/`paths`/`glob`/`globs`/`mapped_paths`, resolved through Puppet's own
interpolation rules (an undefined variable in a location becomes `''` plus
a warning and is still probed, never a skipped level; `mapped_paths` binds
each collection item as a local scope variable), with a `glob`/`globs`
location matched through hyera's own Ruby `Dir.glob` port (brace
alternation, Ruby's dotfile and `**` rules, no recursion through a symlink
or junction) · `datadir` (default `data`,
next to hiera.yaml) ·
`default_hierarchy` (module layer only) · `data_hash` backends (yaml/json/hocon, plus the
non-Puppet `sops_data`) · `lookup_key`/`data_dig` provider backends, called
per key and per location with a `hyera.LookupContext` · hierarchy `options`,
interpolated and passed to the backend with `path`/`uri` · a hierarchy entry
with no location key (calls its function once, with no location) · `uri`/
`uris` locations (for provider backends), validated with Ruby's `URI()`
grammar and normalized like `URI#to_s`, never fetched or checked for
existence · all five
interpolation methods (`hiera`/`lookup`/`alias`/`scope`/`literal`) with
Puppet's parsing and rendering rules, hash-key interpolation, and recursion
detection; `%{alias()}` as the whole value keeps the value's type · merges
`first`/`default`/`unique`/
`hash`/`deep` with `knockout_prefix`/`sort_merged_arrays`/`merge_hash_arrays`,
plus the Hiera-3-era `reverse_deep`/`unconstrained_deep` ·
`lookup_options` (per-key/regex merge strategy + `convert_to`) ·
global/environment/module layers (`Hiera(..., environmentpath=,
basemodulepath=, modulepath=)`), with `hiera3_backend` global-only and a
version-3/missing-`version` environment or module hiera.yaml ignored (or
raising under `strict="error"`) ·
`eyaml_lookup_key` (PKCS7 only, behind the `hyera[eyaml]` extra) ·
`explain()`, reporting a lookup the way `puppet lookup
--explain`/`--explain-options` does.

Not implemented: hiera.yaml version 3/4 (a file without `version` is version
3) · `hiera3_backend` legacy shim · encrypted-value `convert_to` beyond
`Sensitive` · hiera-eyaml encryptors other than PKCS7 (GPG and third-party
plugins) · reading `environment.conf`'s `modulepath`/`environment_data_provider`,
or metadata.json's deprecated `data_provider`, both superseded here by the
explicit `modulepath=` keyword.

## Differences from Puppet

hyera aims to resolve exactly like `puppet lookup`. Every `data_hash`/
`lookup_key`/`data_dig` name it accepts is a real Puppet function name —
with one deliberate exception:

- **`sops_data`** (also `sops`, and `sops_yaml`/`sops_json`/`sops_ini`/
  `sops_dotenv` to force the format) — a `data_hash` backend with no
  Puppet equivalent, for decrypting a
  [sops](https://github.com/getsops/sops)-encrypted data file on the fly.
  A hierarchy that uses it does not load under real Puppet.
- **`eyaml_lookup_key` supports only the PKCS7 encryptor.** hiera-eyaml's
  other encryptors (GPG, and any third-party plugin) raise the same
  "cannot load such file" error real Puppet gives without that plugin's
  gem installed — this project never adds one.
- **`convert_to` (Puppet's `new()`) does not support every type Puppet
  does.** SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI,
  Type and Object all raise `hyera.HieraLookupError` ("hiera does not
  support new() for the Puppet type '...'") instead of converting — these
  are types whose values are not plain data. A type alias other than
  `Data`/`RichData` is also unsupported (`parse_type` resolves only the
  five Puppet static-loader aliases; any other capitalized name becomes an
  unresolved type reference).
- **`hocon_data`'s `include file("*.conf")` globs.** Puppet's own
  `hocon_data` never expands a glob in a `file(...)` argument (it
  contributes nothing); hyera's default lets pyhocon's own resolution run
  for real, which does glob and includes every match. Every other
  `include` form matches Puppet exactly (see "Backends" above).
- **`Hiera(path)` raises `ConfigError` when the file does not exist.**
  Puppet then falls back to its built-in default configuration; ask for
  that explicitly with `Hiera(None, base_path=...)` here.
- **A deep-merge `knockout_prefix` that Python's `re` module cannot compile
  raises `MergeError`.** Ruby accepts a prefix like `**` (with a warning
  about a redundant nested repeat operator) and uses it as a regex; Python
  refuses to compile it at all.
- **Undefined variables default to `strict="warning"`** (an undefined
  `%{var}`/`%{scope('var')}` interpolates as `""` and logs a warning);
  Puppet 8 defaults to `strict="error"`. Pass `Scope(strict="error")` (or
  `.scoped(strict="error")`) to match Puppet's own default.
- **The directory holding hiera.yaml is used literally.** Puppet
  interpolates `%{...}` inside that absolute path too; hyera does not --
  and, for glob levels, treats glob metacharacters in it as a pattern.
- **Glob wildcards are case-sensitive and results sort by byte order on
  every OS, as on Puppet's Linux servers; Ruby on Windows matches glob
  wildcards case-insensitively.**
- **`environmentpath=None` (the default) means no environment directories
  at all.** Puppet always has an `environmentpath`, so an environment name
  it cannot find always raises; here, with no `environmentpath` configured,
  every environment name resolves with no environment layer and no error --
  a library with no layers configured keeps working exactly as before this
  feature existed.
- **A changed `hiera.yaml` is not re-read by an existing `Hiera`.** Puppet
  re-reads it between compilations; construct a new `Hiera` to pick up a
  changed base config. Data files and glob listings *are* re-checked, by
  default — see `revalidate` below.

## Notes

Everything raised derives from `HieraError` (`.path` names the file
concerned, where there is one):

- `ConfigError` — `hiera.yaml` is missing, unreadable, unparsable, or
  violates Puppet's version 5 schema. `.line` names the 1-based line in
  `.path` the problem was found at, when known.
- `BackendError` — a data file could not be read or parsed; `.path` names
  it.
- `HieraLookupError` — a failure while resolving a key, with subclasses
  `InterpolationError` (an unknown interpolation method, a misplaced
  `%{alias(...)}`, a recursive lookup, or an undefined variable under
  `strict="error"`), `MergeError` (an unknown or invalid merge strategy),
  and `KeyNotFoundError` (also a `KeyError`) — `lookup()`'s miss, with no
  default given.

## License

MIT, for this project's own code. It is derived from
[phiera](https://github.com/Nike-Inc/phiera), which is Apache-2.0; the files
taken from it keep that license. Several modules also port code translated
from [Puppet](https://github.com/puppetlabs/puppet) (Apache-2.0) and from
[Psych](https://github.com/ruby/psych) (MIT), Ruby's YAML library; those
files carry their own notice. See `NOTICE` and `LICENSES/`.
