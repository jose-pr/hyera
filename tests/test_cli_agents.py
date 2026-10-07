"""The CLI served as an MCP tool, and its help text for agents."""

import json
import os
import shlex
import subprocess
import sys

import pytest

duho = pytest.importorskip("duho")

from hyera.cli import main  # noqa: E402
from cli_support import (  # noqa: F401
    _SRC,
    flags_root,
    hiera_root,
)


def test_mcp_stdio_serves_lookup(hiera_root):
    # HYERA_MCP=stdio serves one tool; its name and the serverInfo.name both come from
    # duho's root tool-name resolution (Lookup._parsername_): "hyera", not "Lookup".
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
                "name": "hyera",
                "arguments": {
                    "keys": ["app::name"],
                    "hiera_config": str(hiera_root / "hiera.yaml"),
                    "facts": str(hiera_root / "facts.yaml"),
                    "scope": ["environment=production"],
                    "render_as": "s",
                },
            },
        },
    ]
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, "HYERA_MCP": "stdio", "PYTHONPATH": src},
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "hyera"
    assert replies[2]["result"]["content"] == [{"type": "text", "text": "myapp\n"}]


def test_mcp_tool_call_accepts_double_dash_as_the_knockout_prefix(flags_root):
    # An option value that is exactly "--" is a valid tool argument, not an
    # invalid-arguments error: the knockout prefix applies to the merge.
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
                "name": "hyera",
                "arguments": {
                    "keys": ["h"],
                    "hiera_config": str(flags_root / "hiera.yaml"),
                    "facts": str(flags_root / "facts.yaml"),
                    "merge": "deep",
                    "knock_out_prefix": "--",
                    "render_as": "json",
                },
            },
        },
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, "HYERA_MCP": "stdio", "PYTHONPATH": _SRC},
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr
    reply = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}[2]
    assert "error" not in reply, reply
    assert json.loads(reply["result"]["content"][0]["text"]) == {
        "items": ["a", "c", "d"],
        "rows": [{"y": 2}, {"x": 1}],
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {"merge": "bogus"},
        {"merge": "first", "merge_hash_arrays": True},
        {"value_type": "Bogus["},
        {"value_type": "Integer[10,0]"},
    ],
    ids=repr,
)
def test_mcp_tool_call_reports_a_bad_option_value_as_one_error(arguments, flags_root):
    # The same result as the command line: one ERROR line, no traceback, and no
    # exception class named in front of the message.
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
                "name": "hyera",
                "arguments": {
                    "keys": ["h"],
                    "hiera_config": str(flags_root / "hiera.yaml"),
                    "facts": str(flags_root / "facts.yaml"),
                    **arguments,
                },
            },
        },
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, "HYERA_MCP": "stdio", "PYTHONPATH": _SRC},
        timeout=120,
    )

    reply = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}[2]
    assert reply["result"]["isError"] is True, reply
    text = reply["result"]["content"][0]["text"]
    assert text.splitlines()[-1].startswith("exit code: 2"), text
    assert len([line for line in text.splitlines() if "ERROR" in line]) == 1, text
    for noise in ("Traceback", "ValueError", "TypeError"):
        assert noise not in text + proc.stderr


def test_mcp_trigger_follows_declared_name_not_argv0(hiera_root, monkeypatch, capsys):
    # The MCP trigger env var name comes from Lookup's `_parsername_`, not from
    # sys.argv[0]'s stem, so embedding the CLI in another script keeps HYERA_MCP.
    monkeypatch.setattr(sys, "argv", ["/x/cli.py"])
    monkeypatch.setenv("HYERA_MCP", "bogus")

    rc = main(["app::name", "--hiera_config", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_mcp_unknown_transport_exits_2(hiera_root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hyera"])
    monkeypatch.setenv("HYERA_MCP", "bogus")

    rc = main(["app::name", "--hiera_config", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_help_is_plain_text(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: hyera")
    assert "lookup_options" in out
    assert "``" not in out


def _agent_help(*extra):
    proc = subprocess.run(
        [sys.executable, "-m", "hyera", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "AGENT_HELP": "1", "PYTHONPATH": _SRC},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_agent_help_declares_the_real_exit_codes():
    codes = _agent_help()["exit_codes"]
    assert codes["1"] == "No value found for the key"
    assert codes["2"].startswith("Any other error")
    assert "130" in codes


def test_agent_help_examples_run(monkeypatch, capsys):
    root = os.path.dirname(_SRC)
    monkeypatch.chdir(root)
    examples = _agent_help()["examples"]
    assert len(examples) >= 3
    for example in examples:
        argv = shlex.split(example["command"])
        assert argv[0] == "hyera"
        assert main(argv[1:]) == 0, example["command"]
        assert capsys.readouterr().out.strip() != ""


def test_agent_help_field_help_is_one_line_and_facts_is_required():
    options = {o["dest"]: o["help"] for o in _agent_help()["options"] if "dest" in o}
    assert "REQUIRED" in options["facts"]
    for dest, text in options.items():
        assert "\n" not in text, dest
