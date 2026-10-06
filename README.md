# hyera

[![PyPI](https://img.shields.io/pypi/v/hyera.svg)](https://pypi.org/project/hyera/)
[![Python versions](https://img.shields.io/pypi/pyversions/hyera.svg)](https://pypi.org/project/hyera/)
[![License](https://img.shields.io/badge/license-MIT_AND_Apache--2.0_AND_BSD--2--Clause-blue.svg)](https://github.com/jose-pr/hyera#license)
[![Docs](https://img.shields.io/badge/docs-latest-blue.svg)](https://jose-pr.github.io/hyera/)
[![CI](https://img.shields.io/github/actions/workflow/status/jose-pr/hyera/test.yml)](https://github.com/jose-pr/hyera/actions/workflows/test.yml)

**hyera** resolves [Puppet Hiera](https://www.puppet.com/docs/puppet/7/hiera.html)
data the way Puppet 8's own `lookup` does: `pip install hyera`, `import hyera`,
and run the `hyera` command. It reads a Hiera base config, walks the
hierarchy for a given context, and fully resolves values -- `%{...}`
interpolation, the `hiera`/`lookup`/`scope`/`literal`/`alias` functions, and
array/hash/deep-hash merging included. See the
[documentation site](https://jose-pr.github.io/hyera/) for the full guide,
and [Differences from Puppet](#differences-from-puppet) for where hyera
intentionally diverges.

## Features

- **Hiera 5 configuration** -- full `hiera.yaml` version 5 schema validation,
  with Puppet's own error messages.
- **Hiera 1-4 configs, too** -- a versionless or `version: 3` `hiera.yaml`
  (Hiera 1, 2 and 3's own dialect) and `version: 4` module/environment
  configs are read the way Puppet reads them.
- **Global, environment and module layers** -- `environmentpath`/
  `basemodulepath`/`modulepath` reproduce Puppet's own layer stack,
  including a module's `default_hierarchy`.
- **Every lookup form** -- `lookup`/`__call__`/`h[...]`/`in`, plus
  `dig`/`get`/`getvar` navigation and `explain()`.
- **Puppet's merge strategies** -- `first`, `unique`, `hash` and `deep`
  (with `knockout_prefix`, `sort_merged_arrays`, `merge_hash_arrays`),
  driven by an explicit `merge=` or by data-declared `lookup_options`.
- **`convert_to`** -- Puppet's `new()` type conversion, including a
  redacting `Sensitive` wrapper.
- **YAML, JSON and HOCON backends**, plus `eyaml_lookup_key` (PKCS7) and a
  `sops_data` backend Puppet itself does not have.
- **A CLI that mirrors `puppet lookup`**'s own flags, exit codes and
  `--render-as` output, runnable as `hyera`, `python -m hyera`, or as an
  MCP tool (`HYERA_MCP=stdio`).
- **Bounded, revalidating caches** -- lookups reuse resolved locations and
  parsed data across calls, and pick up changed files without restarting.
- **A typed exception hierarchy** -- every failure derives from
  `HieraError`, so callers can catch precisely.

## Installation

```bash
pip install hyera
```

| Extra | Install | Adds | Needed for |
| --- | --- | --- | --- |
| `cli` | `pip install "hyera[cli]"` | `duho>=0.6.0,<0.7` | the `hyera` command / `python -m hyera` |
| `hocon` | `pip install "hyera[hocon]"` | `pyhocon>=0.3.62,<0.4` | `hocon_data` hierarchy levels |
| `eyaml` | `pip install "hyera[eyaml]"` | `cryptography>=50.0,<51` | `eyaml_lookup_key` (PKCS7) levels |

The `sops_data`/`sops`/`sops_<format>` backend needs the external
[`sops`](https://github.com/getsops/sops) binary on `PATH`, not a Python
extra.

## Quick start

```yaml
version: 5
defaults:
  datadir: data
  data_hash: yaml_data
hierarchy:
  - name: "Per node"
    path: "nodes/%{trusted.certname}.yaml"
  - name: "Per OS family"
    path: "os/%{facts.os.family}.yaml"
  - name: "Per role"
    path: "roles/%{role}.yaml"
  - name: "Common"
    path: "common.yaml"
```

This is [`examples/hiera.yaml`](https://github.com/jose-pr/hyera/blob/main/examples/hiera.yaml);
its data lives under `examples/data/` and a matching fact file sits at
`examples/facts.yaml`. From the repo root:

```pycon
>>> from hyera import Hiera, Scope, load_facts
>>> h = Hiera("examples/hiera.yaml", scope=Scope(facts=load_facts("examples/facts.yaml")))
>>> h.lookup("ntp::servers")
['ntp.web.example.com', '0.pool.ntp.org', '1.pool.ntp.org']
>>> h["nginx::workers"]
8
>>> h.dig("users", "alice", "uid")
1001
>>> h.lookup("users")
{'alice': {'shell': '/bin/bash', 'uid': 1001}}
>>> "motd" in h
True

```

With the `cli` extra installed:

```console
$ hyera --hiera_config examples/hiera.yaml --facts examples/facts.yaml --node web01.example.com --render-as json ntp::servers
["ntp.web.example.com","0.pool.ntp.org","1.pool.ntp.org"]
```

`puppet lookup` takes the same flags, as
[`examples/README.md`](https://github.com/jose-pr/hyera/blob/main/examples/README.md)
shows.

## API overview

| Module | Purpose | Reference |
| --- | --- | --- |
| `hyera` | hyera: a Python implementation of Puppet Hiera data lookup. | https://jose-pr.github.io/hyera/api/hyera/ |
| `hyera.types` | Public Puppet type objects (`Integer`, `Optional`, `Struct`, ...). | https://jose-pr.github.io/hyera/api/types/ |
| `hyera.backends` | Data backends: a self-registering `Backend` registry. | https://jose-pr.github.io/hyera/api/backends/ |
| `hyera.cli` | Command-line interface for hyera, built on duho. | https://jose-pr.github.io/hyera/api/cli/ |
| `hyera` (command) / `python -m hyera` | Runs a lookup from the command line, mirroring `puppet lookup`'s own flags. | [#command-line](#command-line) |

## Guide

### Configuration

A standard Hiera 5 `hiera.yaml` works. Each level names a `data_hash`,
`lookup_key` or `data_dig` backend and a source (`path`, `paths`, `glob`,
`globs`, `uri`, `uris`, or `mapped_paths`):

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

See [Backends](#backends) below for the registered `data_hash`/`lookup_key`
names and the one non-Puppet backend.

#### Hiera 3 and 4 configs

A hiera.yaml without a `version` key, or with `version: 3` (Hiera 1, 2 and 3
are the same dialect to Puppet), is read and validated against Puppet's own
version 3 schema, then resolved with Puppet's backend-major provider order:
one data source per listed `backends` name, over the *whole* hierarchy, in
list order.

```yaml
---
:backends:
  - yaml
:yaml:
  :datadir: data
  :extension: yaml
:hierarchy:
  - "nodes/%{::trusted.certname}"
  - common
```

`yaml`/`json`/`hocon`/`eyaml` map onto the same `YAMLBackend`/`JSONBackend`/
`HOCONBackend`/`EyamlBackend` a v5 `data_hash: yaml_data` etc. would use;
any other name must be a third-party `hyera.Backend` registered under that
name in the `"v3"` namespace (`NAMES = {"v3": (...)}`), or it raises
`ConfigError` (see [Differences from Puppet](#differences-from-puppet) --
Puppet, with real Hiera 3 installed, would instead skip that backend
silently). `merge_behavior`/`deep_merge_options`/`logger` are validated but
never applied, matching Puppet: only an explicit `merge=`/`--merge` changes
how results combine. A relative `datadir` (including the default,
`<codedir>/environments/%{::environment}/hieradata`) resolves against the
process's working directory *at construction*, never the hiera.yaml
directory -- `Hiera(..., codedir=...)`/`hyera --codedir` set `$codedir`
(Puppet's own AIO default per platform otherwise).

hiera.yaml version 4 (`backend: yaml|json|hocon` instead of `data_hash:`,
`path`/`paths` defaulting to the entry's own `name`) is accepted in the
*environment* and *module* layers only -- Puppet rejects it at the global
layer, after validating its schema (a schema-invalid version 4 file at the
global layer raises its schema error, never the layer one). Its `datadir`
is joined onto the config root exactly as written, with no interpolation at
all -- unlike every other version. A version 3 (or missing-`version`)
hiera.yaml at an environment or module root is likewise still fully
schema-validated, then ignored with a warning (or, under
`Scope(strict="error")`, raised) rather than read -- see
[Layers](#layers) below. `hiera3_backend` follows the same backend-name
rule as a v3 `backends:` entry, and is accepted only in the global layer.

A `%{lookup()}`/`%{hiera()}`/`%{alias()}` reached while interpolating a
version 3 *global* layer's own data stays confined to the global layer --
never reaching an environment or module, even for an otherwise-qualified
key -- unless the current environment has a real version 5 hiera.yaml (an
absent, ignored-version-3, or version 4 environment all count as none).

#### Layers

`hiera.yaml` above is the *global* layer. Puppet also reads an
*environment* layer and, for a `module::key`-shaped lookup, a *module*
layer -- pass `environmentpath`/`basemodulepath`/`modulepath` to
`Hiera(...)` to enable them:

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

The lookup order, at every level, is global then environment then module --
a merge (`merge="unique"`, `merge="deep"`, ...) spans all three. A key not
qualified `<module>::...` never reaches the module layer at all, and a
module's own data that is not qualified with that module's name is dropped
(with a warning) rather than leaking into another module's namespace.
`hiera3_backend` is accepted only in the global layer's hiera.yaml. A
version-3 (or missing-`version`) hiera.yaml at an environment or module
root is silently ignored (with a warning); `puppet lookup`'s own
`strict=error` raises instead. A version 4 hiera.yaml at an environment or
module root is read normally (it is only the global layer that rejects
it). See the
[shipped API header](https://github.com/jose-pr/hyera/blob/main/src/hyera/AGENTS.md)'s
"Layers" entry for the full discovery and error rules.

A module's own `hiera.yaml` may also declare a `default_hierarchy`
(`default_hierarchy` is rejected everywhere else -- global or environment
-- with `ConfigError`):

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
The caller's `merge=` does not apply there -- the merge comes from the
default hierarchy's own `lookup_options` instead -- while the main
hierarchy's `convert_to` still applies to whatever value it returns.

### Lookups

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

#### Navigating values

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

#### Explaining a lookup

```python
print(h.explain("ntp::servers").text())
```

`explain` takes exactly `lookup`'s own arguments and returns a
`hyera.ExplainResult`: `.text()` is the indented report `puppet lookup
--explain` prints (every hierarchy entry and path consulted, merges and
their results, interpolations, the `lookup_options` search); `.to_hash()`
is the same tree, keyed the way `--render-as json --explain` renders it.
`explain_options=True` reports only how `lookup_options` was assembled.

#### Type-checked lookups

`value_type` (on `lookup`/`dig`/`get`/`explain`/`()`/`[]`) takes a Puppet
type-expression string, or the equivalent object from `hyera.types` -- one
isinstance-aware class per Puppet type, never a builtin subclass:

```python
from hyera import types

h.lookup("ntp::servers", types.Array[types.String])  # same as "Array[String]"
h.lookup("retries", types.Integer[1, 10])             # same as "Integer[1, 10]"

isinstance(5, types.Integer)          # True
isinstance(5, types.Integer[1, 10])   # True
isinstance(11, types.Integer[1, 10])  # False

types.Integer("42")   # 42 (an int) -- Puppet's new(), same as convert_to
types.Array("ab")      # ["a", "b"]
```

A bare class (`types.Integer`) is the unparameterized type; subscripting
(`types.Integer[1, 10]`) builds a parameterized one, equal to parsing the
same Puppet text; calling either is Puppet's `new()`. `hyera.types` is not
re-exported from top-level `hyera` except `Sensitive`, already public there
as the redacting wrapper (`types.Sensitive` is the same object).

### Merging and `lookup_options`

Pass `merge=` to `lookup()` -- one of Puppet's strategy names, a `hyera.Merge`
member (`Merge.DEEP` is the same value as `"deep"`, so either spelling works
everywhere `merge=` is accepted), or a hash of deep options:

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
data under the reserved `lookup_options` key -- then callers need not pass
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
module data all apply to the same key -- global wins over environment,
which wins over module -- and a module's own keys/patterns must start with
`<module>::`.

An explicit `merge=` argument overrides only the *merge* `lookup_options`
would have picked; `convert_to` always applies. `convert_to` takes
a Puppet type string (`Integer`, `Optional[Integer]`) or `[Type, *args]`
(`[Integer, 16]`, `[String, '%x']`) and converts with Puppet's `new()`:
Integer, Float, Numeric, String, Boolean, Array, Hash, Tuple, Struct,
Optional, NotUndef and Sensitive (a redacting `hyera.Sensitive` wrapper). An
invalid type or a failed conversion raises `hyera.HieraLookupError`.

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
from hyera import Hiera, Scope, Strict, load_facts

scope = Scope(facts=load_facts("facts.yaml"), environment="production", strict=Strict.ERROR)
h = Hiera("hiera.yaml", scope=scope)
h.lookup("ntp::servers")
```

`strict=` takes a `hyera.Strict` member (`OFF`/`WARNING`/`ERROR`) or the
plain string it equals (`"off"`/`"warning"`/`"error"`); `hyera.Merge`,
`hyera.FunctionKind`, `hyera.BackendKind` and `hyera.RenderAs` are the same
kind of `str`-mixin enum for the other closed-set arguments described below.

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

`strict` (`"off"`, `"warning"` -- the default -- or `"error"`) controls
what happens when an interpolated variable is undefined; see
[Differences from Puppet](#differences-from-puppet).

### Backends

Backends (by `data_hash` name -- Puppet function names only; see
[Differences from Puppet](#differences-from-puppet) below for the one
exception):

| Backend        | `data_hash` name | Notes                                          |
| -------------- | ----------------- | ---------------------------------------------- |
| `YAMLBackend`  | `yaml_data`       | parses YAML the way Puppet's Psych does (types, symbols, BOM), on libyaml when available |
| `JSONBackend`  | `json_data`       |                                                 |
| `HOCONBackend` | `hocon_data`      | requires `pip install "hyera[hocon]"`          |
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
Pass `hocon_includes=False` to `HOCONBackend`, or set `options:
{hocon_includes: false}` on a `hocon_data` hierarchy entry (or in
`defaults: {options: ...}`; hyera's own extension, which Puppet rejects --
it refuses every `options` key on `hocon_data`), to restore the stricter,
pre-fidelity behaviour instead:
every form but a plain quoted include raises, `include file(…)` included.
In either mode, pyhocon's own include-resolving methods stay wrapped as a
fail-closed backstop, so an undiscovered gap in the text scanner still
cannot read a file or reach the network for a form the active mode does
not intend to resolve.

#### sops and unattended runs

`SopsBackend` (`data_hash: sops_data`) shells out to `sops` to decrypt a
level on the fly. The format (YAML, JSON, INI or dotenv) is inferred from
the file's extension the same way the `sops` CLI itself picks it
(`.yaml`/`.yml`/`.json`/`.env`/`.ini`, case-sensitive); any other
extension is a clear error, since `sops` would read that file as binary.
It is hardened so an automated lookup never hangs, dies opaquely, or
leaks a decrypted secret:

- a finite subprocess timeout (`hyera.backends.SOPS_TIMEOUT`, default 30 s;
  `SopsBackend(timeout=...)` overrides it for one backend); on expiry `sops` and its child processes are killed and a
  `BackendTimeoutError` (a `BackendError` and a `TimeoutError`) is raised,
- `sops` runs with its standard input closed, so it can never consume the
  caller's own input,
- the last 2,000 characters of its stderr surfaced in a `BackendError`,
- a clear error when the `sops` binary is not on `PATH`,
- the data file is passed to `sops` as an absolute path after a literal
  `--`, so a level or scope value that starts with `-` can never be read as
  a `sops` option,
- the `sops` found on `PATH` is the one executed, by its full resolved
  path; a `sops.bat`/`sops.cmd` shim is refused (`cmd.exe` re-parses a
  batch file's argument line, which a data-derived path could abuse),
- a decrypted file that fails to parse reports only the problem and its
  line/column -- never the decrypted plaintext; a YAML value shaped to
  quote itself into the error message (`!!float`, `!ruby/object:...`) is
  redacted instead,
- an INI file is always decrypted through sops's own JSON view, never
  ini text -- sops's INI writer can otherwise emit a value that a text
  parser reads as a different key or an injected section.

#### eyaml_lookup_key

`EyamlBackend` (`lookup_key: eyaml_lookup_key`) decrypts hiera-eyaml's
`ENC[PKCS7,...]` values, behind the optional `eyaml` extra
(`cryptography`). **PKCS7 only** -- the private key alone is needed, no
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
  working directory**, exactly like hiera-eyaml itself -- not `base_path`
  and not the data file's own directory.
- Other hiera-eyaml encryptors (GPG and third-party plugins) are not
  supported; a value using one raises the same "cannot load such file"
  error Puppet itself gives without that plugin installed.

### Command line

Install the `cli` extra to get the command: `pip install "hyera[cli]"`
(without it, `hyera`/`python -m hyera` print that hint and exit 2).

`hyera` accepts `puppet lookup`'s own flags:

```sh
hyera [options] KEY [KEY ...]

hyera --hiera_config hiera.yaml --facts facts.yaml --node web01.example.com ntp::servers
hyera --merge deep --knock-out-prefix=-- --render-as json profile::settings
hyera --explain ntp::servers
python -m hyera --hiera_config hiera.yaml --facts facts.yaml ntp::servers
```

Options, grouped:

- **lookup**: one or more `KEY`s (the first one found wins); `--merge
  first|unique|hash|deep`; `--knock-out-prefix`, `--sort-merged-arrays` and
  `--merge-hash-arrays` (only with `--merge deep`); `--type` (asserts the
  found value and `--default` against a Puppet type expression); `--default`;
  `--explain`/`--explain-options`.
- **facts and scope**: `--facts FILE` (`.json`/`.yaml`/`.yml`, or any other
  name tried as JSON then YAML); `--node NAME` (used in messages only, sets
  no fact); `--scope NAME=VALUE`/`-s` (repeatable; VALUE is YAML, a dotted
  NAME builds a hash -- hyera's one flag with no `puppet lookup` counterpart).
- **settings**: `--hiera_config PATH` (default `./hiera.yaml` if present,
  else Puppet's built-in default configuration); `--environment NAME`;
  `--environmentpath`/`--modulepath`/`--basemodulepath` (each a list of
  paths separated by the OS path separator); `--codedir`; `--strict
  off|warning|error` (default `warning`).
- **output**: `--render-as s|json|yaml` (default `yaml`, or `s` while
  explaining).
- **logging**: `-v`/`--verbose` (repeatable; adds info, then debug),
  `-d`/`--debug` (debug, same as `-vv`), `-q`/`--quiet` (repeatable; drops
  to error, then critical), `--loglevel [NAME:]LEVEL`.

Without `--merge`, the data's `lookup_options` decides; an explicit
`--merge`, `first` included, overrides it. See
[Errors and exit codes](#errors-and-exit-codes) below for what each exit
status means and what this CLI does not (yet) support.

`HYERA_MCP=stdio hyera` runs the same command as an MCP server over
stdin/stdout, so an MCP client can drive lookups: it exposes one tool,
`hyera`, whose arguments are the command-line fields (`keys`, `hiera_config`,
`facts`, `scope`, `merge`, ...) and whose result is what the command would
print.

### Errors and exit codes

Everything raised derives from `HieraError` (`.path` names the file
concerned, where there is one):

- `ConfigError` -- `hiera.yaml` is missing, unreadable, unparsable, or
  violates Puppet's version 5 schema. `.line` names the 1-based line in
  `.path` the problem was found at, when known.
- `BackendError` -- a data file could not be read or parsed; `.path` names
  it.
- `HieraLookupError` -- a failure while resolving a key, with subclasses
  `InterpolationError` (an unknown interpolation method, a misplaced
  `%{alias(...)}`, a recursive lookup, or an undefined variable under
  `strict="error"`), `MergeError` (an unknown or invalid merge strategy),
  and `KeyNotFoundError` (also a `KeyError`) -- `lookup()`'s miss, with no
  default given.

The CLI's exit codes: `0` found (or `--default`/`--explain` printed), `1`
the key was not found -- nothing is printed, matching `puppet lookup`'s own
silent miss -- `2` any other error: a usage problem, a bad config or data
file, an unrenderable value, or a reader that closes the output early
(also silent: nothing on stderr). A `2` is reported as one stderr line
(`-v`, `-d` or `DUHO_TRACEBACK=1` adds the traceback). `puppet lookup`
exits `1` for both a miss and an error, printing nothing for the error
case; hyera's CLI tells the two apart.

### Caching

Each `Hiera` (and every `.scoped(...)` view of it, which shares the same
caches) keeps two scope-keyed caches -- resolved hierarchy locations and
merged `lookup_options` -- plus an unbounded cache of parsed data files,
bounded like Puppet's own per-environment cache: its size follows the data
tree, not the number of scopes seen. `Hiera(..., cache_size=256)` bounds
each scope-keyed cache (least-recently-used entries dropped; `None` for no
bound, `0` to disable); `Hiera.clear_cache()` drops every cache, including
parsed data files. `Hiera(..., revalidate=True)` (the default): each
lookup re-checks the data files and glob listings it uses and re-reads one
whose inode, modification time or size changed, as Puppet does between
compilations -- files added or removed at `path`/`paths`/`mapped_paths`
locations and under globbed directories are seen by the next lookup.
`revalidate=False` keeps every file and glob listing as first read until
`clear_cache()`. Neither mode re-reads `hiera.yaml` itself; construct a new
`Hiera` to pick up a changed base config.

## Hiera coverage

**`hiera.yaml` keys**

| Feature | Status | Notes |
| --- | --- | --- |
| `version` | Supported | `5` is fully validated; a versionless or `3` config is read as Hiera 3; `4` only in environment/module layers; any other value raises. |
| `defaults` | Supported | applies to any hierarchy entry lacking its own value; missing/empty/null becomes Puppet's built-in default. |
| `name` | Supported | required, non-empty, unique per hierarchy. |
| `path` | Supported | |
| `paths` | Supported | |
| `glob` | Partial | matched through hyera's own Ruby `Dir.glob` port; case-sensitive and byte-sorted on every OS, unlike Ruby on Windows. (id: `glob-case-sensitive-byte-order`) |
| `globs` | Partial | same as `glob`. (id: `glob-case-sensitive-byte-order`) |
| `mapped_paths` | Supported | a scope reference iterated as `[key, value]` pairs; each item is a local scope variable. |
| `uri` | Supported | validated with Ruby's `URI()` grammar, passed to the entry's function, never fetched. |
| `uris` | Supported | same as `uri`. |
| `datadir` | Supported | defaults to `data`, next to hiera.yaml. |
| `options` | Supported | interpolated, passed to the backend with `path`/`uri`. |
| `data_hash` | Supported | value must be a real Puppet function name (or the one non-Puppet `sops_data` name -- see Backends below). |
| `lookup_key` | Supported | called per key and per location with a `hyera.LookupContext`. |
| `data_dig` | Supported | same calling convention as `lookup_key`, plus the requested key segments. |
| `hiera3_backend` | Partial | global layer only; an unregistered name raises `ConfigError` where Puppet, with real Hiera 3 installed, silently contributes nothing. (id: `v3-ruby-backend-unavailable`) |
| `default_hierarchy` | Supported | module layer only; consulted after every other layer misses. |
| `plan_hierarchy` | Not supported | schema-validated but never consulted -- hyera does not run Puppet Bolt plans, the only context where Puppet applies it. |

**Features**

| Feature | Status | Notes |
| --- | --- | --- |
| Hiera 3 and 4 configs | Supported | version 3 (or missing) resolved with Puppet's backend-major provider order; version 4 accepted in the environment/module layers only. |
| `version` 1, 2 and others | Supported | 1 and 2 are Hiera 3's own dialect to Puppet, read the same way; any other version raises "This runtime does not support hiera.yaml version N". |
| Global/environment/module layers | Supported | `Hiera(..., environmentpath=, basemodulepath=, modulepath=)`. |
| Interpolation variables (`%{x}`, `%{::x}`, `%{facts.x}`, `%{trusted.x}`) | Supported | |
| Interpolation functions (`hiera`/`lookup`/`alias`/`scope`/`literal`) | Supported | |
| Undefined variables (`strict`) | Partial | default is `"warning"` (interpolates as `""` and logs); Puppet 8 defaults to `"error"`. (id: `strict-default-warning`) |
| Merge strategies (`first`/`default`/`unique`/`hash`/`deep`) | Supported | |
| Deep-merge options (`knockout_prefix`, `sort_merged_arrays`, `merge_hash_arrays`) | Partial | a `knockout_prefix` Python's `re` cannot compile raises, where Ruby accepts it with a warning. (id: `knockout-prefix-not-python-regex`) |
| `reverse_deep`/`unconstrained_deep` | Supported | Hiera-3-era deep-merge variants. |
| `lookup_options` merge (exact and `^` keys) | Supported | |
| `convert_to` | Partial | SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI, Type and Object all raise. (id: `convert-to-unsupported-type`) |
| Lookup forms (name list, `value_type`, `default_value`, `default_values_hash`, `override`, `block`) | Supported | |
| Dotted keys | Supported | |
| `dig`/`get`/`getvar` | Supported | |
| `explain`/`explain_options` | Supported | |
| Type expressions | Partial | a type alias other than `Data`/`RichData` is unsupported. (id: `convert-to-unsupported-type`) |
| `yaml_data` | Supported | |
| `json_data` | Supported | |
| `hocon_data` | Partial | `include file("*.conf")` globs by default, where Puppet's never does, and pyhocon parses a few constructs differently; see [Backends](#backends) and Differences from Puppet. (id: `hocon-include-glob`) |
| `eyaml_lookup_key` | Partial | PKCS7 only; other hiera-eyaml encryptors are not supported. (id: `eyaml-pkcs7-only`) |
| `sops_data` | Supported | the one backend with no Puppet equivalent. (id: `sops-backend`) |
| `puppet lookup` CLI flags | Partial | every flag except `--compile`/`--trusted` and the binary `--render-as` formats. (id: `environment-conf-compile-trusted-unsupported`) |

**Not supported**

| Feature | Status | Notes |
| --- | --- | --- |
| Running an arbitrary Ruby Hiera 3 backend | Not supported | a v3/`hiera3_backend` name must be a Puppet-mapped one or a third-party Python `hyera.Backend`. |
| Encrypted-value `convert_to` beyond `Sensitive` | Not supported | |
| hiera-eyaml encryptors other than PKCS7 | Not supported | GPG and third-party plugins. |
| `environment.conf`'s `modulepath`/`environment_data_provider`, `metadata.json`'s deprecated `data_provider` | Not supported | superseded by the explicit `modulepath=` keyword. |
| `--render-as binary\|msgpack` | Not supported | |
| `--compile`/`--trusted` | Not supported | |
| `calling_class`/`calling_module` | Not supported | |
| Facts from PuppetDB or the Puppet server | Not supported | facts come only from `--facts`/`Scope(facts=...)`. |
| `puppet.conf` discovery | Not supported | |
| Type aliases other than `Data`/`RichData`; `new()` for SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI, Type, Object | Not supported | |
| `hiera()`/`hiera_array()`/`hiera_hash()`/`hiera_include()` as methods | Not supported | use `.lookup()` -- see the mapping table under [Lookups](#lookups). |
| The types `Iterable`, `Iterator`, `Init` and `Unit` in a type expression | Not supported | a `value_type` or `convert_to` naming one raises `HieraLookupError`. |

## Differences from Puppet

hyera aims to resolve exactly like `puppet lookup`. Every deliberate
difference is listed here, tagged with a slug; each is also either a
recorded conformance-harness deviation (checked against the real Puppet
oracle) or a documented-only difference the harness cannot record a golden
for.

- **A missing hiera.yaml raises `ConfigError`, not Puppet's built-in
  fallback.** `Hiera(path)` raises when `path` does not exist, and the
  CLI's `--hiera_config` behaves the same way for a named file that is
  missing; Puppet then falls back to its built-in default configuration.
  Ask for that explicitly with `Hiera(None, base_path=...)`, or omit
  `--hiera_config` so `./hiera.yaml`-if-present is tried first. (id: `missing-config-raises`)
- **The directory holding hiera.yaml is used literally.** hyera never
  interpolates `%{...}` inside that absolute base directory, and for glob
  levels treats any glob metacharacter in it as a literal pattern
  character; Puppet interpolates `%{...}` there too. There is no opt-in,
  since the directory is fixed at construction. (id: `config-dir-not-interpolated`)
- **A changed hiera.yaml is not re-read by an existing `Hiera`.** Puppet
  re-reads it between compilations; construct a new `Hiera` to pick up a
  changed base config (data files and glob listings *are* re-checked by
  default; see [Caching](#caching)). (id: `config-not-revalidated`)
- **`environmentpath=None` (the default) means no environment directories
  at all.** Every environment name then resolves with no environment layer
  and no error; Puppet always has an `environmentpath`, so an environment
  name it cannot find always raises. Pass a real `environmentpath` to get
  Puppet's raising behaviour. (id: `environmentpath-none-means-no-layer`)
- **An unregistered Hiera 3 backend name raises `ConfigError`.** Puppet,
  with real Hiera 3 installed, silently contributes nothing for a
  `backends:`/`hiera3_backend:` name it cannot run; hyera cannot run a
  Ruby Hiera 3 backend at all, so register a third-party Python
  `hyera.Backend` under that name instead, or drop it from `backends:`. (id: `v3-ruby-backend-unavailable`)
- **`codedir` defaults to Puppet's AIO system location for the platform**
  (`%ALLUSERSPROFILE%\PuppetLabs\code` on Windows, `/etc/puppetlabs/code`
  elsewhere) -- never the per-user `~/.puppetlabs/etc/code` default or a
  value discovered from `puppet.conf`. Pass `codedir=`/`--codedir`
  explicitly to match a differently-configured Puppet install. (id: `codedir-aio-default`)
- **`sops_data`** (also `sops`, and `sops_yaml`/`sops_json`/`sops_ini`/
  `sops_dotenv` to force the format) -- a `data_hash` backend with no
  Puppet equivalent, for decrypting a
  [sops](https://github.com/getsops/sops)-encrypted data file on the fly.
  A hierarchy that uses it does not load under real Puppet, and there is
  no Puppet equivalent to fall back to. (id: `sops-backend`)
- **`hocon_data`'s `include file("*.conf")` globs.** hyera lets pyhocon's
  own resolution run for real, which expands a glob in a `file(...)`
  argument and includes every match; Puppet's own `hocon_data` never
  expands such a glob (it contributes nothing). There is no opt-in that
  reproduces Puppet's non-globbing `file(...)` exactly, though
  `hocon_includes=False` (or `options: {hocon_includes: false}` on the
  entry or in `defaults`) is available as a stricter, non-resolving
  alternative for every include form. Every other `include` form matches Puppet exactly
  (see [Backends](#backends)). (id: `hocon-include-glob`)
- **`hocon_data` is parsed by pyhocon, not Ruby's hocon gem.** Quoted keys,
  `null` inside a concatenation and unicode escapes match Puppet, but these
  constructs differ (Puppet, then hyera): `list = [1]` then `list += 2`
  gives `[1,2]`, then `2`; `enabled = True` is the string `"True"`, then
  the boolean `true`; `label = true x` is `"true x"`, then `"Truex"`;
  `mode = 010` is `8`, then `10`; `ratio = 1.0` is `1`, then `1.0`; a key
  set first to an object and then to a scalar, a quoted-path key such as
  `"x"."y" = 4`, the empty-string key `"" = 2` and a leading byte-order
  mark load in Puppet and are parse errors in hyera; `[1,, 2]` and `+1`
  are errors in Puppet and are accepted by hyera; a backslash-slash escape
  and a unicode escape with non-hex digits are kept as written; an object's
  keys come back in a different order. (id: `hocon-pyhocon-parser`)
- **`eyaml_lookup_key` supports only the PKCS7 encryptor.** hiera-eyaml's
  other encryptors (GPG, and any third-party plugin) raise the same
  "cannot load such file" error real Puppet gives without that plugin's
  gem installed -- this project never adds one, so there is no way to opt
  into GPG support here. (id: `eyaml-pkcs7-only`)
- **Navigating a dotted sub-key into an Integer keeps hyera's own error
  text.** Puppet crashes with a raw Ruby `NoMethodError` ("undefined method
  'include?' for an instance of Integer") instead of a designed message;
  hyera raises `HieraLookupError` ("Data Provider type mismatch: Got
  Integer when a hash-like object was expected ..."), the same shape it
  already uses for a String or Array in this position. There is no
  opt-in. (id: `integer-dotted-navigation-error-text`)
- **A deep-merge `knockout_prefix` that Python's `re` module cannot compile
  raises `MergeError`.** Ruby accepts a prefix like `**` (with a warning
  about a redundant nested repeat operator) and uses it as a regex; choose
  a `knockout_prefix` that is valid in both regex dialects to avoid the
  difference. (id: `knockout-prefix-not-python-regex`)
- **`convert_to` (Puppet's `new()`) does not support every type Puppet
  does.** SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI,
  Type and Object all raise `hyera.HieraLookupError` ("hiera does not
  support new() for the Puppet type '...'") instead of converting -- these
  are types whose values are not plain data. A type alias other than
  `Data`/`RichData` is also unsupported (`parse_type` resolves only the
  five Puppet static-loader aliases; any other capitalized name becomes an
  unresolved type reference). There is no opt-in. (id: `convert-to-unsupported-type`)
- **Undefined variables default to `strict="warning"`** (an undefined
  `%{var}`/`%{scope('var')}` interpolates as `""` and logs a warning);
  Puppet 8 defaults to `strict="error"`, which fails the lookup. Pass
  `Scope(strict="error")` (or `.scoped(strict="error")`) to match Puppet's
  own default. Hierarchy locations of a version 5 `hiera.yaml` are
  lenient in every mode, as in Puppet; a version 3 or 4 one fails the lookup
  under `strict="error"`, as in Puppet. (id: `strict-default-warning`)
- **Glob wildcards are case-sensitive and results sort by byte order on
  every OS**, as on Puppet's Linux servers; Ruby on Windows matches glob
  wildcards case-insensitively instead, so a hierarchy authored against a
  Windows Puppet server may need adjusting. (id: `glob-case-sensitive-byte-order`)
- **`--render-as yaml` prints `Sensitive` values redacted**, as the other
  formats do; Puppet prints the plaintext. There is no opt-in to print the
  plaintext here. (id: `render-yaml-sensitive-redacted`)
- **`--render-as s` prints hashes in Ruby 3.2's AIO form** (`{"a"=>1}`), as
  Puppet 8's own packages do; Puppet on Ruby 3.4 or later renders
  `{"a" => 1}` (with spaces around `=>`) instead. There is no opt-in, since
  hyera targets the AIO packages' own Ruby version. (id: `aio-hash-rendering`)
- **`--scope NAME=VALUE` sets node parameters**, which `puppet lookup`
  takes from the node classifier instead; use `--scope` for every value a
  real Puppet run would source from the classifier. (id: `scope-flag-sets-node-parameters`)
- **Facts come only from `--facts`/`Scope(facts=...)`.** `puppet lookup`
  also reads the local node's facter facts or PuppetDB-stored facts when
  `--facts` is omitted; hyera always requires an explicit facts source (a
  `--facts` file with no facts is rejected, as in Puppet). (id: `facts-from-file-only`)
- **`$server_facts` holds only `serverversion` (`8.10.0`) and
  `environment`.** A real Puppet server populates several more; pass the
  missing ones through `Scope(server_facts=...)` directly if a hierarchy
  needs them. (id: `server-facts-minimal`)
- **`environment.conf` is not read; `--compile` and `--trusted` are not
  supported.** Configure `environmentpath`/`modulepath`/`basemodulepath`
  explicitly instead of relying on `environment.conf` discovery, and there
  is no catalog-compilation mode to fall back to. (id: `environment-conf-compile-trusted-unsupported`)
- **Hash keys Python cannot tell apart are an error or one key.** Ruby
  keeps `1`, `1.0` and `true` as three keys; Python treats them as one. A
  YAML mapping whose keys collide only that way (`{1: a, 1.0: b}`) raises
  `BackendError` naming them, rather than silently dropping one entry, and
  `--merge deep` joins such keys from different levels into one. Write the
  keys as strings to avoid it. (id: `python-equal-hash-keys`)
- **Data that is not valid UTF-8 is rejected as a whole.** hyera reads data
  files and `eyaml` plaintext as strict UTF-8. A `json_data` file with one
  non-UTF-8 byte fails every lookup that reaches it, where Puppet answers
  the other keys and fails only when it renders that value; an `eyaml`
  plaintext that is not UTF-8 raises, where Puppet returns the bytes; a
  `!!binary` value whose bytes are not UTF-8 cannot be rendered as `s` or
  `json`. (id: `non-utf8-data`)
- **Collections nested more than 500 levels deep are a parse error**, in
  YAML and JSON files, `--facts` files and `--scope` values, with the
  message `nested too deeply`. Puppet reads YAML to about 10,000 levels and
  stops JSON at 100; the bound keeps a hostile file from ending the
  interpreter, which libyaml's recursive composer does on Python 3.9.
  (id: `nesting-bound`)
- **A few Ruby regex constructs are refused, and POSIX bracket classes are
  ASCII-only.** A `Pattern`/`Regexp` type, a `convert_to`/`value_type`
  expression or a `lookup_options` key using `\p{..}`, `\P{..}`, `\R`,
  `\X`, `\G`, `\K`, `\g<..>`, `&&` or a nested class inside `[...]`, a
  negated shorthand (`\D \W \S \H`) inside `[...]`, a nested repeat such as
  `a**`, or (on Python 3.9 and 3.10) a possessive quantifier or an atomic
  group, raises `HieraLookupError` naming the construct, where Ruby accepts
  it. `[[:alpha:]]` and the other POSIX classes match ASCII letters only,
  where Ruby's match Unicode. There is no match-time bound, as in Ruby: a
  pattern with nested quantifiers can take exponential time on a long
  subject. (id: `ruby-regex-constructs`)

## Development

```bash
python -m venv .venv/3.14-posix-x86_64
.venv/3.14-posix-x86_64/bin/pip install -e ".[dev]"
python -m pytest -q
python -m black --check src/ tests/ benchmarks/ examples/
```

Windows uses `Scripts\python` instead of `bin/python`; venvs are named
`<version>-<os>-<arch>`. Build the docs with `pip install -e ".[docs]"`,
then `python -m mkdocs build --strict`. See the
[root `AGENTS.md`](https://github.com/jose-pr/hyera/blob/main/AGENTS.md)
for the conformance recorder and the full contributor guide.

### Releasing

This project follows [Semantic Versioning](https://semver.org/) and keeps a
[`CHANGELOG.md`](https://github.com/jose-pr/hyera/blob/main/CHANGELOG.md).
Pushing a tag matching `v*` runs `release.yml`: the test gate, then `build`
(which checks the tag names the version actually built), then a strict
docs build (`docs-gate`), then the GitHub release, then publishing to PyPI
through Trusted Publishing. A pre-release tag (`v1.0.0-rc.1`) is published
as a PyPI pre-release, which `pip install hyera` skips unless you ask for it
(`--pre` or an exact version pin); only a final tag also dispatches
`docs.yml` to redeploy the docs at that tag.

## License

MIT, for this project's own code. Several modules port code translated from
[Puppet](https://github.com/puppetlabs/puppet) (Apache-2.0), the
[deep_merge](https://github.com/danielsdeleo/deep_merge) gem (MIT),
[Psych](https://github.com/ruby/psych) (MIT), Ruby's YAML library, and
Ruby's [uri](https://github.com/ruby/uri) library (2-clause BSDL); those
files carry their own notice. See
[`NOTICE`](https://github.com/jose-pr/hyera/blob/main/NOTICE) and
[`LICENSES/`](https://github.com/jose-pr/hyera/tree/main/LICENSES).
