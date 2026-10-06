"""CLI/entry-point behavior when the ``cli`` extra (duho) is not installed.

Each check runs in a subprocess with ``sys.modules["duho"]`` set to ``None``.
"""

import os
import subprocess
import sys
from pathlib import Path

import hyera

_SRC = str(Path(hyera.__file__).resolve().parents[1])


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
        "from hyera.cli import main\n"
        "sys.exit(main(['k']))\n"
    )
    result = _run(code)
    assert result.returncode == 2
    assert 'pip install "hyera[cli]"' in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


def test_module_entrypoint_without_cli_extra():
    code = (
        "import sys\n"
        "sys.modules['duho'] = None\n"
        "sys.argv = ['hyera', '--help']\n"
        "import runpy\n"
        "runpy.run_module('hyera', run_name='__main__')\n"
    )
    result = _run(code)
    assert result.returncode == 2
    assert 'pip install "hyera[cli]"' in result.stderr
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout
