# Example

A minimal, runnable Hiera 5 hierarchy, exercised the same way `hyera` itself
is tested against the Puppet oracle.

- `hiera.yaml` — the base config: four levels, per node (`trusted.certname`),
  per OS family (`facts.os.family`), per role (a top-scope `role` variable),
  and a common fallback.
- `facts.yaml` — a Puppet `--facts`-shaped fact file for node `web01`
  (`hostname`/`domain`/`fqdn`/`clientcert`, plus `role` and `os.family`).
- `data/` — the data tree: `common.yaml` (with `lookup_options` for
  `ntp::servers` and `users`), `roles/web.yaml`, `os/RedHat.yaml`, and
  `nodes/web01.example.com.yaml`.
- `lookup.py` — loads the facts, builds a `Hiera` instance, and prints five
  resolved values.

## Run it

```
python examples/lookup.py
```

```
ntp::servers = ["ntp.web.example.com", "0.pool.ntp.org", "1.pool.ntp.org"]
nginx::workers = 8
packages::manager = "dnf"
motd = "Welcome to web01.example.com"
users = {"alice": {"shell": "/bin/bash", "uid": 1001}}
```

Or, with the `cli` extra installed (`pip install "hyera[cli]"`), the
console script or `python -m hyera` take the same argv:

```
python -m hyera -c examples/hiera.yaml --scope role=web --scope clientcert=web01.example.com -o json ntp::servers
```

which prints `ntp::servers`'s value as JSON.

The equivalent with real Puppet:

```
puppet lookup --hiera_config examples/hiera.yaml --facts examples/facts.yaml --node web01.example.com --render-as json ntp::servers
```
