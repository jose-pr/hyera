"""Shared fixtures: a small on-disk hiera hierarchy."""

import textwrap

import pytest


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture
def hiera_root(tmp_path):
    """A hiera tree rooted at ``tmp_path`` with a config + data files."""
    _write(
        tmp_path / "hiera.yaml",
        """\
        ---
        defaults:
          data_hash: yaml_data
          data_dir: data
        hierarchy:
          - name: "Per-environment"
            path: "environments/%{environment}.yaml"
          - name: "Modules"
            globs:
              - "modules/*.yaml"
          - name: "Common"
            path: "common.yaml"
        """,
    )
    _write(
        tmp_path / "data" / "common.yaml",
        """\
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
    )
    _write(
        tmp_path / "data" / "environments" / "production.yaml",
        """\
        ---
        ntp::servers:
          - prod.pool.ntp.org
        classes:
          - prod
        db:
          host: db.prod.internal
        lookup_greeting: "%{hiera('app::name')} in prod"
        """,
    )
    _write(
        tmp_path / "data" / "modules" / "web.yaml",
        """\
        ---
        classes:
          - web
        """,
    )
    return tmp_path
