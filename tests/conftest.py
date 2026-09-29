"""Shared fixtures: build a valid Hiera 5 tree on disk, LF/UTF-8 always."""

import copy
import textwrap

import pytest
import yaml


@pytest.fixture
def make_tree(tmp_path):
    """Factory fixture: write a hiera config plus data files under a root.

    ``config`` is either a ``dict`` (deep-copied, then given ``version: 5``
    and a ``data_hash``/``datadir`` default via ``setdefault`` -- never
    overriding a value the test set on purpose -- and dumped with
    ``yaml.safe_dump``) or a ``str`` (dedented and written verbatim). A
    string config must parse to a mapping with a ``version`` key unless
    ``raw=True``: Puppet reads a versionless ``hiera.yaml`` as a Hiera 3
    config, so a test that forgets ``version:`` should fail loudly rather
    than silently exercise the wrong dialect.

    ``files`` maps a relative path under the tree to its content: a
    ``str`` is dedented and written as UTF-8, ``bytes`` are written as-is
    (for a test that needs different bytes on purpose). ``facts`` is
    written to ``facts.yaml``. Every write is ``write_bytes``, so the
    result is LF and UTF-8 on every OS -- the same shape a conformance
    case needs.
    """

    def _make_tree(config, files=None, facts=None, *, root=None, raw=False):
        base = tmp_path if root is None else tmp_path / root

        def write(rel, data):
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return path

        if isinstance(config, dict):
            config = copy.deepcopy(config)
            config.setdefault("version", 5)
            defaults = config.setdefault("defaults", {})
            defaults.setdefault("data_hash", "yaml_data")
            defaults.setdefault("datadir", "data")
            text = yaml.safe_dump(config, sort_keys=False)
        else:
            text = textwrap.dedent(config)
            if not raw:
                parsed = yaml.safe_load(text)
                if not isinstance(parsed, dict) or "version" not in parsed:
                    raise ValueError(
                        "Puppet reads a versionless hiera.yaml as version 3; "
                        "pass raw=True if that is the point of this test"
                    )
        write("hiera.yaml", text.encode("utf-8"))

        for rel, content in (files or {}).items():
            data = (
                content
                if isinstance(content, bytes)
                else textwrap.dedent(content).encode("utf-8")
            )
            write(rel, data)

        if facts is not None:
            write("facts.yaml", yaml.safe_dump(facts, sort_keys=False).encode("utf-8"))

        return base

    return _make_tree


@pytest.fixture
def hiera_root(make_tree):
    """A hiera tree with a config + data files: per-environment, glob, common."""
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
    )
