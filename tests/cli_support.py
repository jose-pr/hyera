"""Fixtures and helpers shared by the ``hyera`` CLI test modules."""

import logging
import os

import pytest

duho = pytest.importorskip("duho")

import hyera  # noqa: E402

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(hyera.__file__)))


def _error_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "hyera.cli" and r.levelno == logging.ERROR
    ]


@pytest.fixture
def hiera_root(make_tree):
    """``conftest.hiera_root``, plus a ``facts.yaml`` -- the CLI now
    requires ``--facts`` for every lookup (an empty facts mapping is
    Puppet's own "No facts available" error), so every CLI test needs a
    real facts file even when the data itself never reads a fact.
    Overrides (shadows) the shared ``conftest.py`` fixture of the same
    name for this module only; other test files keep the plain one.
    """
    return make_tree(
        {
            "version": 5,
            "defaults": {"data_hash": "yaml_data", "datadir": "data"},
            "hierarchy": [
                {"name": "Per-environment", "path": "environments/%{environment}.yaml"},
                {"name": "Modules", "globs": ["modules/*.yaml"]},
                {"name": "Common", "path": "common.yaml"},
            ],
        },
        files={
            "data/common.yaml": """\
                ---
                app::name: myapp
                greeting: "hello %{environment}"
                ntp::servers:
                  - a.pool.ntp.org
                classes:
                  - base
                db:
                  host: localhost
                  port: 5432
                alias_target: "%{alias('app::name')}"
                alias_list: "%{alias('ntp::servers')}"
                literal_pct: "100%{literal('%')} done"
                port_msg: "listening on %{hiera('db.port')}"
                """,
            "data/environments/production.yaml": """\
                ---
                ntp::servers:
                  - prod.pool.ntp.org
                classes:
                  - prod
                db:
                  host: db.prod.internal
                lookup_greeting: "%{hiera('app::name')} in prod"
                """,
            "data/modules/web.yaml": """\
                ---
                classes:
                  - web
                """,
        },
        facts={"role": "web"},
    )


@pytest.fixture
def render_root(make_tree):
    """A tree exercising Puppet-shaped rendering end to end (parsing,
    ``convert_to: Sensitive``, then ``--render-as``). ``big`` is large
    enough (20 000 20-char strings) to overflow an OS pipe buffer, so a
    reader that closes early forces a real ``BrokenPipeError``.
    """
    common_yaml = (
        "str: one\n"
        "nil: ~\n"
        "nested:\n"
        "  b: 2\n"
        "  a:\n"
        "    - 1\n"
        "    - z: 1\n"
        "      y: 2\n"
        'unicode: "café \u2603"\n'
        "nan: .nan\n"
        "secret: hunter2\n"
        "lookup_options:\n"
        "  secret: { convert_to: Sensitive }\n"
        "big:\n"
    ) + "".join("  - {}\n".format("x" * 20) for _ in range(20000))
    return make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": common_yaml},
        facts={"role": "web"},
    )


# ---------------------------------------------------------------------------
# puppet lookup's flag set: merge validation and deep-merge options, --type,
# scope/facts/node, layers (--environment*/--modulepath*), --strict,
# --explain/--explain-options, and the flags this CLI removed outright.
# ---------------------------------------------------------------------------


@pytest.fixture
def flags_root(make_tree):
    """A two-level hierarchy (``high`` over ``low``) plus a staging
    environment, a ``mymod`` module reachable from two different module
    roots, and three alternate facts files -- everything the flag tests
    below need from one tree."""
    root = make_tree(
        {
            "hierarchy": [
                {"name": "high", "path": "high.yaml"},
                {"name": "low", "path": "low.yaml"},
            ]
        },
        files={
            "data/high.yaml": """\
                h:
                  items: [d, "--b"]
                  rows:
                    - x: 1
                v: "%{role}-%{facts.os.family}|%{environment}|%{server_facts.environment}|[%{trusted.certname}]|%{server_facts.serverversion}"
                u: "[%{nosuch}]"
                str: one
                int0: 0
                sv: "%{n}|%{b}|%{o.x}|%{l}"
                """,
            "data/low.yaml": """\
                h:
                  items: [b, a, c]
                  rows:
                    - y: 2
                """,
            "empty_facts.yaml": "{}\n",
            "partial_facts.yaml": "fqdn: a.example.com\n",
            "environments/staging/hiera.yaml": """\
                version: 5
                defaults: {datadir: data, data_hash: yaml_data}
                hierarchy: [{name: e, path: env.yaml}]
                """,
            "environments/staging/data/env.yaml": "envkey: fromstaging\n",
            "modules/mymod/hiera.yaml": """\
                version: 5
                defaults: {datadir: data, data_hash: yaml_data}
                hierarchy: [{name: m, path: common.yaml}]
                """,
            "modules/mymod/data/common.yaml": "mymod::k: frommodule\n",
            "othermods/mymod/hiera.yaml": """\
                version: 5
                defaults: {datadir: data, data_hash: yaml_data}
                hierarchy: [{name: m, path: common.yaml}]
                """,
            "othermods/mymod/data/common.yaml": "mymod::k: fromother\n",
        },
        facts={"role": "web", "os": {"family": "RedHat"}},
    )
    (root / "empty_environments").mkdir(parents=True, exist_ok=True)
    return root


def _flags_argv(flags_root, *extra):
    return [
        "--hiera_config",
        str(flags_root / "hiera.yaml"),
        "--facts",
        str(flags_root / "facts.yaml"),
        "--node",
        "web01.example.com",
    ] + list(extra)
