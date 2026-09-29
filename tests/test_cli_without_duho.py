"""CLI/entry-point behavior when the ``cli`` extra (duho) is not installed.

No ``duho`` import at module level here -- both local venvs have duho
installed (it's in the ``dev`` extra), so each check runs in a fresh
subprocess with ``sys.modules["duho"]`` forced to ``None`` before importing
``pyera``, to simulate a plain ``pip install pyera`` with no extras.
"""

import os
import subprocess
import sys
from pathlib import Path

import pyera

_SRC = str(Path(pyera.__file__).resolve().parents[1])


def _run(code: str) -> "subprocess.CompletedProcess":
    env = {**os.environ, "PYTHONPATH": _SRC}
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )


def test_console_script_without_cli_extra():
    code = (
        "import sys\n"
        "sys.modules['duho'] = None\n"
        "from pyera.cli import main\n"
        "sys.exit(main(['k']))\n"
    )
    result = _run(code)
    assert result.returncode == 2
    assert 'pip install "pyera[cli]"' in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def test_module_entrypoint_without_cli_extra():
    code = (
        "import sys\n"
        "sys.modules['duho'] = None\n"
        "sys.argv = ['pyera', '--help']\n"
        "import runpy\n"
        "runpy.run_module('pyera', run_name='__main__')\n"
    )
    result = _run(code)
    assert result.returncode == 2
    assert 'pip install "pyera[cli]"' in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
