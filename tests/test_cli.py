"""CLI behavior and exit codes (unattended-friendly)."""

import json
import os
import runpy
import subprocess
import sys

import pytest
import yaml

duho = pytest.importorskip("duho")

from pyera.cli import main  # noqa: E402


@pytest.fixture
def values_root(make_tree):
    """A tree exercising every CLI output type, including ``Sensitive``."""
    return make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": """\
                str: hello
                int: 42
                bool: true
                flt: 1.5
                nested:
                  b: 2
                  a:
                    - 1
                    - z: 1
                      y: 2
                lst:
                  - 1
                  - - 2
                    - 3
                  - k: v
                secret: hunter2
                secret_hash:
                  user: admin
                  pass: hunter2
                lookup_options:
                  secret: { convert_to: Sensitive }
                  secret_hash: { convert_to: Sensitive }
                """},
    )


@pytest.fixture
def mergefirst_root(make_tree):
    """A tree where ``lookup_options`` declares ``unique`` for ``classes``,
    so an explicit ``--merge first`` overriding it is observable.
    """
    return make_tree(
        {
            "hierarchy": [
                {"name": "os", "path": "os/%{facts.os.family}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": """\
                classes:
                  - base
                lookup_options:
                  classes: { merge: unique }
                """,
            "data/os/RedHat.yaml": """\
                classes:
                  - redhat
                """,
        },
    )


_VALUES_EXPECTED = {
    "str": "hello",
    "int": 42,
    "bool": True,
    "flt": 1.5,
    "nested": {"b": 2, "a": [1, {"z": 1, "y": 2}]},
    "lst": [1, [2, 3], {"k": "v"}],
    "secret": "Sensitive(<redacted>)",
    "secret_hash": "Sensitive(<redacted>)",
}


@pytest.mark.parametrize("key", list(_VALUES_EXPECTED))
@pytest.mark.parametrize("fmt", ["raw", "json", "yaml"])
def test_output_formats(fmt, key, values_root, capsys):
    rc = main(["-c", str(values_root / "hiera.yaml"), "-o", fmt, key])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out

    expected = _VALUES_EXPECTED[key]
    if fmt == "yaml":
        assert yaml.safe_load(out) == expected
    elif fmt == "json":
        assert json.loads(out) == expected
    else:  # raw
        if isinstance(expected, (dict, list)):
            assert json.loads(out) == expected
        else:
            assert out.strip() == str(expected)


@pytest.mark.parametrize("fmt", ["raw", "json", "yaml"])
def test_default_in_each_format(fmt, values_root, capsys):
    rc = main(
        [
            "nope::key",
            "-c",
            str(values_root / "hiera.yaml"),
            "--default",
            "fallback",
            "-o",
            fmt,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    if fmt == "yaml":
        assert yaml.safe_load(out) == "fallback"
    elif fmt == "json":
        assert json.loads(out) == "fallback"
    else:
        assert out.strip() == "fallback"


def test_explicit_merge_first_overrides_lookup_options(mergefirst_root, capsys):
    base_args = [
        "classes",
        "-c",
        str(mergefirst_root / "hiera.yaml"),
        "-s",
        "facts.os.family=RedHat",
        "-o",
        "json",
    ]

    assert main(base_args) == 0
    assert json.loads(capsys.readouterr().out) == ["redhat", "base"]

    assert main(base_args + ["--merge", "first"]) == 0
    assert json.loads(capsys.readouterr().out) == ["redhat"]

    assert main(base_args + ["--merge", "unique"]) == 0
    assert json.loads(capsys.readouterr().out) == ["redhat", "base"]


def test_mcp_stdio_serves_lookup(hiera_root):
    # PYERA_MCP=stdio runs the CLI as an MCP server exposing one tool. Both
    # the tool name and the initialize response's serverInfo.name come from
    # duho's root tool-name resolution (Lookup._parsername_ -- see R7a),
    # which is "pyera", not the command class's own name "Lookup".
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "pyera",
                "arguments": {
                    "key": "app::name",
                    "config": str(hiera_root / "hiera.yaml"),
                    "scope": ["environment=production"],
                },
            },
        },
    ]
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [sys.executable, "-m", "pyera"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, "PYERA_MCP": "stdio", "PYTHONPATH": src},
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "pyera"
    assert replies[2]["result"]["content"] == [{"type": "text", "text": "myapp\n"}]


def test_mcp_trigger_follows_declared_name_not_argv0(hiera_root, monkeypatch, capsys):
    # The MCP trigger env var name must come from Lookup's own declared
    # `_parsername_`, not from sys.argv[0]'s stem -- otherwise embedding
    # pyera's CLI in another script (or running it as `python -m pyera.cli`)
    # silently changes which env var launches the MCP server, contradicting
    # the documented PYERA_MCP contract.
    monkeypatch.setattr(sys, "argv", ["/x/cli.py"])
    monkeypatch.setenv("PYERA_MCP", "bogus")

    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_mcp_unknown_transport_exits_2(hiera_root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pyera"])
    monkeypatch.setenv("PYERA_MCP", "bogus")

    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_lookup_found(hiera_root, capsys):
    rc = main(
        [
            "app::name",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "-s",
            "environment=production",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "myapp"


def test_lookup_missing_exit_1(hiera_root):
    rc = main(["nope::key", "-c", str(hiera_root / "hiera.yaml")])
    assert rc == 1


def test_lookup_missing_with_default_exit_0(hiera_root, capsys):
    rc = main(
        ["nope::key", "-c", str(hiera_root / "hiera.yaml"), "--default", "fallback"]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "fallback"


def test_bad_config_exit_2(tmp_path):
    rc = main(["k", "-c", str(tmp_path / "does-not-exist.yaml")])
    assert rc == 2


def test_merge_array_alias_extension(hiera_root, capsys):
    # Non-Puppet extension; this test goes with the feature: `--merge array`
    # is our own legacy alias for `unique`.
    rc = main(
        [
            "classes",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "-s",
            "environment=production",
            "--merge",
            "array",
            "-o",
            "json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == ["prod", "web", "base"]


def test_unique_merge_json_output(hiera_root, capsys):
    rc = main(
        [
            "classes",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "-s",
            "environment=production",
            "--merge",
            "unique",
            "-o",
            "json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == ["prod", "web", "base"]


def test_invalid_scope_exit_2(hiera_root):
    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml"), "-s", "noequals"])
    assert rc == 2


def test_module_entrypoint_smoke(monkeypatch):
    # `python -m pyera` wires through to cli.main.
    monkeypatch.setattr(sys, "argv", ["pyera", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("pyera", run_name="__main__")
    assert exc.value.code == 0  # --help exits 0
