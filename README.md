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
```

The PyPI distribution, the import package and the command are all named
`hyera`.

## Library

```python
from hyera import Hiera, Scope

h = Hiera("hiera.yaml", scope=Scope(facts={"os": {"family": "Debian"}}, environment="production"))

# First match wins:
h.get("ntp::servers")

# Merge across the whole hierarchy:
h.get("classes", merge="unique")             # flatten + dedupe arrays
h.get("users", merge="deep")                 # deep hash merge

# Missing keys return the default (with throw=True they raise KeyNotFoundError, a KeyError):
h.get("missing", default="fallback")
h.has("some::key")

# Bind a derived scope once and reuse:
prod = h.scoped(environment="production")
prod.get("ntp::servers")
```

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
h.get("ntp::servers")
```

`load_facts(path)` reads a `puppet lookup --facts`-style file (JSON for
`.json`, YAML for `.yaml`/`.yml`, otherwise JSON then YAML); the result
must be a mapping, and `hostname`/`domain`/`fqdn`/`clientcert` are
all-or-nothing. `facts_from_facter()` runs a bare `facter -j` instead:

```python
from hyera import facts_from_facter

scope = Scope(facts=facts_from_facter())
```

`h.scoped(**derive_args)` returns a `ScopedHiera` bound to
`h.scope.derive(**derive_args)`: `variables`/`facts`/`server_facts`
shallow-update the parent scope's own (new values win, nothing goes
stale); `environment`/`strict`/`trusted`/`node_name` replace the parent's
when given.

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

default_hierarchy:            # consulted only when the hierarchy above misses
  - name: "Module defaults"
    path: "module_defaults.yaml"
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

### Merging and `lookup_options`

Pass `merge=` to `get()` — one of Puppet's strategy names, or a hash of deep
options:

```python
h.get("classes", merge="unique")                 # flatten + dedupe arrays
h.get("app::name", merge="default")               # explicit first-match
h.get("conf", merge="deep")                       # recursive hash merge
h.get("conf", merge={"strategy": "deep",          # deep-merge options
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
An exact key match always wins over a pattern match.

An explicit `merge=` argument overrides `lookup_options`. `convert_to` takes
a Puppet type string (`Integer`, `Optional[Integer]`) or `[Type, *args]`
(`[Integer, 16]`, `[String, '%x']`) and converts with Puppet's `new()`:
Integer, Float, Numeric, String, Boolean, Array, Hash, Tuple, Struct,
Optional, NotUndef and Sensitive (a redacting `hyera.Sensitive` wrapper). An
invalid type or a failed conversion raises `hyera.HieraLookupError`.

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

## Hiera 5 spec coverage

Supported: `version: 5` validation · Puppet's version 5 schema validation
(closed key sets, a required unique `name`, at most one function/location
key, non-empty strings, the `options` name pattern, and the rest) ·
`defaults` · `hierarchy` · `name` ·
`path`/`paths`/`glob`/`globs`/`mapped_paths`, resolved through Puppet's own
interpolation rules (an undefined variable in a location becomes `''` plus
a warning and is still probed, never a skipped level; `mapped_paths` binds
each collection item as a local scope variable) · `datadir` (default `data`,
next to hiera.yaml) ·
`default_hierarchy` · `data_hash` backends (yaml/json/hocon, plus the
non-Puppet `sops_data`) · all five
interpolation methods (`hiera`/`lookup`/`alias`/`scope`/`literal`) with
Puppet's parsing and rendering rules, hash-key interpolation, and recursion
detection; `%{alias()}` as the whole value keeps the value's type · merges
`first`/`default`/`unique`/
`hash`/`deep` with `knockout_prefix`/`sort_merged_arrays`/`merge_hash_arrays`,
plus the Hiera-3-era `reverse_deep`/`unconstrained_deep` ·
`lookup_options` (per-key/regex merge strategy + `convert_to`).

Not implemented: hiera.yaml version 3/4 (a file without `version` is version
3) · `lookup_key`/`data_dig` provider backends (such entries raise
`ConfigError`) · `uri`/`uris`
sources · `eyaml_lookup_key` (use the `sops` backend instead) ·
`hiera3_backend` legacy shim · encrypted-value `convert_to` beyond `Sensitive`.

## Differences from Puppet

hyera aims to resolve exactly like `puppet lookup`. Every `data_hash`/
`lookup_key`/`data_dig` name it accepts is a real Puppet function name —
with one deliberate exception:

- **`sops_data`** (also `sops`, and `sops_yaml`/`sops_json`/`sops_ini`/
  `sops_dotenv` to force the format) — a `data_hash` backend with no
  Puppet equivalent, for decrypting a
  [sops](https://github.com/getsops/sops)-encrypted data file on the fly.
  A hierarchy that uses it does not load under real Puppet.
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
  interpolates `%{...}` inside that absolute path too; hyera does not.

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
  and `KeyNotFoundError` (also a `KeyError`) — `.get(..., throw=True)`'s
  miss.

## License

MIT, for this project's own code. It is derived from
[phiera](https://github.com/Nike-Inc/phiera), which is Apache-2.0; the files
taken from it keep that license. Several modules also port code translated
from [Puppet](https://github.com/puppetlabs/puppet) (Apache-2.0) and from
[Psych](https://github.com/ruby/psych) (MIT), Ruby's YAML library; those
files carry their own notice. See `NOTICE` and `LICENSES/`.
