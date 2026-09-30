# hyera

A small, dependency-light Python implementation of [Puppet
Hiera](https://www.puppet.com/docs/puppet/7/hiera.html) hierarchical data
lookup. It reads a Hiera base config, walks the hierarchy for a given context,
and fully resolves values — including `%{...}` interpolation and the
`hiera`/`lookup`/`scope`/`literal`/`alias` functions — with optional array,
hash, and deep-hash merging. The PyPI distribution, the import package and
the command are all named `hyera`; `Hiera` is the class it exports.

## Installation

```bash
pip install hyera
```

Extras add optional features:

```bash
pip install "hyera[cli]"
```

| Extra | Adds | Needed for |
| --- | --- | --- |
| `cli` | `duho` | the command-line tool |
| `hocon` | `pyhocon` | `hocon_data` levels |
| `eyaml` | `cryptography` | `eyaml_lookup_key` (PKCS7) levels |

## 30-second tour

```yaml
# hiera.yaml
---
version: 5
defaults:
  datadir: data
  data_hash: yaml_data
hierarchy:
  - name: "Per-environment"
    path: "environments/%{environment}.yaml"
  - name: "Common"
    path: "common.yaml"
```

```yaml
# data/common.yaml
ntp::servers:
  - 0.pool.ntp.org
classes:
  - base
```

```yaml
# data/environments/production.yaml
ntp::servers:
  - ntp1.prod.example.com
classes:
  - monitoring
```

```yaml
# facts.yaml
role: web
```

```pycon
>>> from hyera import Hiera, Scope
>>> h = Hiera("hiera.yaml", scope=Scope(environment="production"))
>>> h.lookup("ntp::servers")
['ntp1.prod.example.com']
>>> h.lookup("classes", merge="unique")
['monitoring', 'base']
```

```console
$ python -m hyera classes --hiera_config hiera.yaml --facts facts.yaml --environment production --merge unique --render-as json
["monitoring","base"]
```

## Learn more

- [hyera](api/hyera.md)
- [hyera.backends](api/backends.md)
- [hyera.cli](api/cli.md)
- [Changelog](changelog.md)
- <https://github.com/jose-pr/hyera>
