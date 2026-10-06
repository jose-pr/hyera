"""CLI verbosity flags and the log lines they enable."""

import logging
import os
import subprocess
import sys

import pytest

duho = pytest.importorskip("duho")

import hyera  # noqa: E402
from hyera.cli import main  # noqa: E402
from cli_support import (  # noqa: F401
    _error_records,
    _flags_argv,
    flags_root,
    hiera_root,
)

# ---------------------------------------------------------------------------
# Puppet's own log levels, a silent miss, and plain-text help.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_hyera_logger():
    yield
    logging.getLogger("hyera").setLevel(logging.NOTSET)


def _hyera_records_at_or_above(caplog, level):
    return [
        r for r in caplog.records if r.name.startswith("hyera") and r.levelno >= level
    ]


def test_plain_miss_prints_nothing(hiera_root, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        rc = main(
            [
                "nope::key",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
            ]
        )
    assert rc == 1
    assert capsys.readouterr().out == ""
    assert not _hyera_records_at_or_above(caplog, logging.WARNING)


def test_plain_miss_subprocess_is_silent(hiera_root):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
            "nope::key",
        ],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": src},
        timeout=60,
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert proc.stderr == b""


@pytest.mark.parametrize(
    "flags,expected_level",
    [
        ([], logging.WARNING),
        (["-v"], logging.INFO),
        (["-v", "-v"], logging.DEBUG),
        (["-d"], logging.DEBUG),
        (["-q"], logging.ERROR),
        (["-q", "-q"], logging.CRITICAL),
    ],
    ids=["none", "-v", "-vv", "-d", "-q", "-qq"],
)
def test_verbosity_levels(flags, expected_level, hiera_root, _reset_hyera_logger):
    rc = main(
        [
            "nope::key",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
        ]
        + flags
    )
    assert rc == 1
    assert logging.getLogger("hyera").level == expected_level


def test_debug_logs_the_miss(hiera_root, caplog):
    with caplog.at_level(logging.DEBUG):
        rc = main(
            [
                "nope::key",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
                "-d",
            ]
        )
    assert rc == 1
    assert any(
        "did not find a value for the name 'nope::key'" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("extra", [[], ["-q"]], ids=["default", "-q"])
def test_warnings_follow_quiet(extra, flags_root, caplog):
    with caplog.at_level(logging.DEBUG):
        rc = main(_flags_argv(flags_root, "--strict", "warning", "u", *extra))
    assert rc == 0
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Undefined variable 'nosuch'" in r.getMessage()
    ]
    if extra:
        assert not warnings
    else:
        assert warnings


def test_traceback_with_debug(hiera_root, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)

    def _raise(self, *a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(hyera.Hiera, "lookup", _raise)

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "k",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
                "-d",
            ]
        )

    assert rc == 2
    assert _error_records(caplog)[-1].exc_info is not None
