# hiera

A small, dependency-light Python implementation of [Puppet
Hiera](https://www.puppet.com/docs/puppet/7/hiera.html) hierarchical data
lookup. It reads a Hiera base config, walks the hierarchy for a given context,
and fully resolves values — including `%{...}` interpolation and the
`hiera`/`lookup`/`scope`/`literal`/`alias` functions — with optional array,
hash, and deep-hash merging.

## Install

```sh
pip install hiera          # library only
pip install hiera[cli]     # + the `hiera` command-line tool (via duho)
```

## Library

```python
from hiera import Hiera

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

A standard Hiera 5 config works. Each level names a `data_hash` backend and a
`path`, `paths`, `glob`, or `globs`:

```yaml
---
defaults:
  data_hash: yaml_data
  data_dir: data

hierarchy:
  - name: "Per-node"
    path: "nodes/%{trusted.certname}.yaml"
  - name: "Per-environment"
    path: "environments/%{environment}.yaml"
  - name: "Modules"
    globs:
      - "modules/*.yaml"
  - name: "Common"
    path: "common.yaml"
```

Backends (by `data_hash` name):

| Backend           | `data_hash` names       | Notes                                   |
| ----------------- | ----------------------- | --------------------------------------- |
| `YAMLBackend`     | `yaml_data`, `yaml`     | parsed with PyYAML `SafeLoader`         |
| `JSONBackend`     | `json_data`, `json`     |                                         |
| `SopsYAMLBackend` | `yaml.enc`, `sops`      | decrypts via the `sops` CLI on the fly  |

## Command line

```sh
hiera KEY [options]

hiera ntp::servers --config hiera.yaml --scope environment=production
hiera classes --merge array --output json
hiera missing::key --default '(none)'
```

Options: `--config/-c`, `--scope key=value` (repeatable), `--merge
first|array|hash|set`, `--deep`, `--output/-o raw|json|yaml`, `--default`,
plus duho's `-v/-q/--loglevel`.

The CLI is built for unattended use: no interactive prompts, deterministic
output, and meaningful exit codes — `0` found, `1` key missing, `2`
usage/config error.

## sops and unattended runs

`SopsYAMLBackend` shells out to `sops` to decrypt `*.yaml` levels. It is
hardened so an automated lookup never hangs or dies opaquely:

- a finite subprocess timeout (`hiera.backends.SOPS_TIMEOUT`, default 30 s),
- captured stderr surfaced in a `BackendError`,
- a clear error when the `sops` binary is not on `PATH`.

## Notes

Values that fail to interpolate raise `InterpolationError`; invalid base config
raises `ConfigError`; backend/parse failures raise `BackendError`. All inherit
from `HieraError`.

## License

MIT
