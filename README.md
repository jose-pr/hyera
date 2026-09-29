# pyera

A small, dependency-light Python implementation of [Puppet
Hiera](https://www.puppet.com/docs/puppet/7/hiera.html) hierarchical data
lookup. It reads a Hiera base config, walks the hierarchy for a given context,
and fully resolves values — including `%{...}` interpolation and the
`hiera`/`lookup`/`scope`/`literal`/`alias` functions — with optional array,
hash, and deep-hash merging.

## Install

```sh
pip install pyera          # library only
pip install pyera[cli]     # + the `pyera` command-line tool (via duho)
```

The PyPI distribution, the import package and the command are all named
`pyera`.

## Library

```python
from pyera import Hiera

h = Hiera("hiera.yaml", context={"environment": "production"})

# First match wins:
h.get("ntp::servers")

# Merge across the whole hierarchy:
h.get("classes", merge=list)                 # array merge
h.get("users", merge=dict, merge_deep=True)  # deep hash merge

# Missing keys return the default (or raise with throw=True):
h.get("missing", default="fallback")
h.has("some::key")

# Bind a context once and reuse:
prod = h.scoped(environment="production")
prod.get("ntp::servers")
```

### Base config

A standard Hiera 5 `hiera.yaml` works. Each level names a `data_hash` backend
and a source (`path`, `paths`, `glob`, `globs`, or `mapped_paths`):

```yaml
---
version: 5
defaults:
  data_hash: yaml_data
  data_dir: data

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

Backends (by `data_hash` name):

| Backend           | `data_hash` names       | Notes                                   |
| ----------------- | ----------------------- | --------------------------------------- |
| `YAMLBackend`     | `yaml_data`, `yaml`     | parsed with PyYAML `SafeLoader`         |
| `JSONBackend`     | `json_data`, `json`     |                                         |
| `SopsYAMLBackend` | `yaml.enc`, `sops`      | decrypts via the `sops` CLI on the fly  |
| `HOCONBackend`    | `hocon_data`, `hocon`   | requires `pip install pyera[hocon]`     |

`HOCONBackend` sanitizes `include` directives before parsing, matching
Puppet: a plain `include "file"` contributes nothing. `include file(…)`,
`url(…)`, `classpath(…)` and `required(…)` all raise `BackendError`
instead of reading a file or fetching a URL.

### Merging and `lookup_options`

Pass `merge=` to `get()` — a strategy name, a legacy type, or a hash of deep
options:

```python
h.get("classes", merge="unique")                 # flatten + dedupe arrays
h.get("classes", merge=list)                      # legacy alias for unique
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

An explicit `merge=` argument overrides `lookup_options`. `convert_to`
supports `Integer`, `Float`, `String`, `Boolean`, `Array`, and `Sensitive`
(the last wraps the value in a redacting `pyera.Sensitive` marker).

## Command line

```sh
pyera KEY [options]

pyera ntp::servers --config hiera.yaml --scope environment=production
pyera classes --merge unique --output json
pyera missing::key --default '(none)'
```

Options: `--config/-c`, `--scope key=value` (repeatable), `--merge
first|unique|hash|deep` (`array`/`set` alias `unique`), `--deep`,
`--knockout-prefix`, `--output/-o raw|json|yaml`, `--default`, plus duho's
`-v/-q/--loglevel`. Without `--merge`, the data's `lookup_options` decides;
an explicit `--merge`, `first` included, overrides it.

The CLI is built for unattended use: no interactive prompts, deterministic
output, and meaningful exit codes — `0` found, `1` key missing, `2`
usage/config error.

## sops and unattended runs

`SopsYAMLBackend` shells out to `sops` to decrypt `*.yaml` levels. It is
hardened so an automated lookup never hangs, dies opaquely, or leaks a
decrypted secret:

- a finite subprocess timeout (`pyera.backends.SOPS_TIMEOUT`, default 30 s),
- captured stderr surfaced in a `BackendError`,
- a clear error when the `sops` binary is not on `PATH`,
- the data file is passed to `sops` as an absolute path after a literal
  `--`, so a level or scope value that starts with `-` can never be read as
  a `sops` option,
- the `sops` found on `PATH` is the one executed, by its full resolved
  path; a `sops.bat`/`sops.cmd` shim is refused (`cmd.exe` re-parses a
  batch file's argument line, which a data-derived path could abuse),
- a decrypted file that fails to parse reports only the problem and its
  line/column — never the decrypted plaintext.

## Hiera 5 spec coverage

Supported: `version: 5` validation · `defaults` · `hierarchy` · `name` ·
`path`/`paths`/`glob`/`globs`/`mapped_paths` · `datadir`/`data_dir` ·
`default_hierarchy` · `data_hash` backends (yaml/json/hocon/sops) · all five
interpolation methods (`hiera`/`lookup`/`alias`/`scope`/`literal`) with dotted
subkeys and alias native-type preservation · merges `first`/`unique`/`hash`/
`deep` with `knockout_prefix`/`sort_merged_arrays`/`merge_hash_arrays` ·
`lookup_options` (per-key/regex merge strategy + `convert_to`).

Not implemented: `lookup_key`/`data_dig` provider backends · `uri`/`uris`
sources · `eyaml_lookup_key` (use the `sops` backend instead) ·
`hiera3_backend` legacy shim · encrypted-value `convert_to` beyond `Sensitive` ·
HOCON `include file()` (Puppet reads the file; pyera always raises instead).

## Notes

Values that fail to interpolate raise `InterpolationError`; invalid base config
raises `ConfigError`; backend/parse failures raise `BackendError`. All inherit
from `HieraError`.

## License

MIT, for this project's own code. It is derived from
[phiera](https://github.com/Nike-Inc/phiera), which is Apache-2.0; the files
taken from it keep that license. See `NOTICE` and
`LICENSES/phiera-Apache-2.0.txt`.
