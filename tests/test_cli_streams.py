"""CLI behavior on broken, closed and non-UTF-8 standard streams."""

import contextlib
import io
import logging
import os
import subprocess
import sys

import pytest

duho = pytest.importorskip("duho")

from hyera.cli import main  # noqa: E402
from cli_support import (  # noqa: F401
    _SRC,
    _error_records,
    _flags_argv,
    flags_root,
    render_root,
)


def test_utf8_on_cp1252_stdout(render_root, monkeypatch):
    import io

    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)
    rc = main(
        [
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--render-as",
            "s",
            "unicode",
        ]
    )
    wrapper.flush()
    assert rc == 0
    assert buf.getvalue() == "café ☃\n".encode("utf-8")


def test_utf8_on_cp1252_pipe(render_root):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--render-as",
            "s",
            "unicode",
        ],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": src},
        timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == "café ☃\n".encode("utf-8")


def test_closed_stdout_exits_2_quietly(render_root, tmp_path):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--render-as",
            "json",
            "big",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": src},
    )
    try:
        proc.stdout.read(1)
        proc.stdout.close()
        err = proc.stderr.read()
        rc = proc.wait(timeout=60)
    finally:
        proc.stderr.close()
    assert err == b""
    assert rc == 2


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError(32, "Broken pipe"),
        OSError(22, "Invalid argument"),
        OSError(28, "No space left on device"),
    ],
    ids=["EPIPE", "EINVAL", "ENOSPC"],
)
def test_stdout_write_failure_exits_2_and_silences_stdout(
    error, monkeypatch, render_root, caplog
):
    # In-process counterpart to test_closed_stdout_exits_2_quietly, which needs a real
    # pipe. _silence_stdout is mocked: its dup2() would redirect the test process's
    # own stdout.
    import hyera.cli as cli

    def refuse(text):
        raise error

    monkeypatch.setattr(cli, "_emit", refuse)
    silenced = []
    monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))
    with caplog.at_level(logging.ERROR):
        rc = cli.main(
            [
                "--hiera_config",
                str(render_root / "hiera.yaml"),
                "--facts",
                str(render_root / "facts.yaml"),
                "--render-as",
                "json",
                "str",
            ]
        )
    assert rc == 2
    assert silenced == [True]
    assert _error_records(caplog) == []


def test_reader_gone_before_the_first_write_exits_2_quietly(render_root):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "str",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": _SRC},
    )
    try:
        proc.stdout.close()
        err = proc.stderr.read()
        rc = proc.wait(timeout=60)
    finally:
        proc.stderr.close()
    assert err == b""
    assert rc == 2


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
def test_full_device_stdout_exits_2_quietly(render_root):
    with open("/dev/full", "wb") as full:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyera",
                "--hiera_config",
                str(render_root / "hiera.yaml"),
                "--facts",
                str(render_root / "facts.yaml"),
                "str",
            ],
            stdout=full,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": _SRC},
            timeout=60,
        )
    assert proc.stderr == b""
    assert proc.returncode == 2


def test_emit_falls_back_to_plain_write_without_a_buffer_attr():
    # _emit's fallback for a stdout with no `.buffer` (a StringIO under redirect_stdout,
    # as the conformance harness uses); the except-branch is covered in-process in
    # test_broken_pipe_while_emitting_exits_2_and_silences_stdout.
    from hyera.cli import _emit, _silence_stdout

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit("hello")
    assert buf.getvalue() == "hello\n"

    # _silence_stdout's own best-effort no-op: a StringIO has no real file
    # descriptor, so `.fileno()` raises `io.UnsupportedOperation`.
    with contextlib.redirect_stdout(io.StringIO()):
        _silence_stdout()  # must not raise


def test_main_leaves_the_stdio_encodings_alone(flags_root):
    code = (
        "import sys\n"
        "before = (sys.stdout.encoding, sys.stderr.encoding)\n"
        "from hyera.cli import main\n"
        "rc = main(sys.argv[1:])\n"
        "after = (sys.stdout.encoding, sys.stderr.encoding)\n"
        "sys.stderr.write(repr(before) + ('SAME' if before == after else 'CHANGED'))\n"
        "sys.exit(rc)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C", PYTHONPATH=_SRC)
    proc = subprocess.run(
        [sys.executable, "-c", code] + _flags_argv(flags_root, "str"),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    if "utf" in proc.stderr.lower().split("same")[0].split("changed")[0]:
        pytest.skip("the interpreter's piped stdio is already UTF-8")
    assert proc.stderr.endswith("SAME")
