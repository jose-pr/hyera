"""Run the ``examples/`` scripts for real and pin their output against the
Puppet oracle (see this file's own Verification section in the plan for the
``puppet lookup`` re-check)."""

import json
import runpy
from pathlib import Path

import pytest

import hyera.cli

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
_LOOKUP_SCRIPT = _EXAMPLES_DIR / "lookup.py"

_EXPECTED_LINES = [
    'ntp::servers = ["ntp.web.example.com", "0.pool.ntp.org", "1.pool.ntp.org"]',
    "nginx::workers = 8",
    'packages::manager = "dnf"',
    'motd = "Welcome to web01.example.com"',
    'users = {"alice": {"shell": "/bin/bash", "uid": 1001}}',
]


def test_lookup_script_output(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    runpy.run_path(str(_LOOKUP_SCRIPT), run_name="__main__")
    out = capsys.readouterr().out.splitlines()
    assert out == _EXPECTED_LINES


def test_cli_invocation(capsys):
    pytest.importorskip("duho")
    argv = [
        "--hiera_config",
        str(_EXAMPLES_DIR / "hiera.yaml"),
        "--facts",
        str(_EXAMPLES_DIR / "facts.yaml"),
        "--render-as",
        "json",
        "ntp::servers",
    ]
    code = hyera.cli.main(argv)
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out) == [
        "ntp.web.example.com",
        "0.pool.ntp.org",
        "1.pool.ntp.org",
    ]
