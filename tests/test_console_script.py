"""The installed `hyera` console script: entry-point metadata and a real
subprocess run of the script itself (not `python -m hyera`), on every OS.
"""

import importlib.metadata
import shutil
import subprocess
import sysconfig

import pytest

pytest.importorskip("duho")

import hyera


def test_console_script_entry_points():
    entry_points = {
        ep.name: ep.value
        for ep in importlib.metadata.distribution("hyera").entry_points
        if ep.group == "console_scripts"
    }
    assert entry_points == {"hyera": "hyera.cli:main"}


def test_installed_script_runs(tmp_path):
    script = shutil.which("hyera", path=sysconfig.get_path("scripts"))
    assert script is not None, (
        "the 'hyera' console script is not installed in this interpreter's "
        'scripts directory; run pip install -e ".[dev]" first'
    )

    proc = subprocess.run(
        [script, "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "hyera {}".format(hyera.__version__)
