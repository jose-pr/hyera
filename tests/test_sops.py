"""``SopsYAMLBackend`` argv safety and plaintext-free parse errors.

``tests/test_backends.py`` keeps the pre-existing failure-path tests
(missing binary, non-zero exit, timeout); this file covers the success
path, argv shape, batch-shim refusal, and that a decrypted file which
fails to parse never leaks its plaintext into an error, a log record, or
the CLI's output.
"""

import logging
import os
import subprocess

import pytest

from pyera import ConfigError, Hiera
from pyera.backends import SOPS_TIMEOUT, BackendError, SopsYAMLBackend


def _install_recorder(
    monkeypatch, tmp_path, stdout=b"k: v\n", stderr=b"sops: noise", returncode=0
):
    """Patch ``shutil.which``/``subprocess.run`` and record every call."""
    calls = []
    which_path = str(tmp_path / "bin" / "sops.exe")
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _name: which_path)

    def _run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("pyera.backends.subprocess.run", _run)
    return calls, which_path


def test_sops_success_argv_and_value(monkeypatch, tmp_path):
    calls, which_path = _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    backend = SopsYAMLBackend({})
    secret = tmp_path / "secret.yaml"

    data = backend.read_file(secret)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [
        os.path.abspath(which_path),
        "--input-type=yaml",
        "--output-type=yaml",
        "-d",
        "--",
        os.path.abspath(str(secret)),
    ]
    assert kwargs["timeout"] == SOPS_TIMEOUT
    assert not kwargs.get("shell")
    assert backend.load(data) == {"k": "v"}


def test_sops_end_to_end_lookup(monkeypatch, tmp_path):
    _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    (tmp_path / "data").mkdir()
    # Content is irrelevant -- sops runs on the path, and subprocess.run is
    # mocked -- but the level's file must exist for it to be considered.
    (tmp_path / "data" / "secret.yaml").write_bytes(b"")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: sops\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: secret\n"
        "    path: secret.yaml\n",
        encoding="utf-8",
    )

    h = Hiera(str(config))
    assert h.get("k") == "v"


def test_sops_dash_leading_filename_is_data(monkeypatch, tmp_path):
    calls, _which_path = _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "--output=pwned.yaml").write_bytes(b"")
    (tmp_path / "hiera.yaml").write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: sops\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        '  - name: node\n    path: "%{node}.yaml"\n',
        encoding="utf-8",
    )

    h = Hiera("hiera.yaml")
    assert h.get("k", context={"node": "--output=pwned"}) == "v"

    assert calls, "sops was never invoked for the per-node file"
    args, _kwargs = calls[-1]
    assert args[-2] == "--"
    last = args[-1]
    assert not last.startswith("-")
    assert os.path.isabs(last)


def test_sops_refuses_batch_shim(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: r"C:\tools\sops.CMD")
    called = []
    monkeypatch.setattr(
        "pyera.backends.subprocess.run", lambda *a, **k: called.append((a, k))
    )

    with pytest.raises(BackendError, match="batch"):
        SopsYAMLBackend({}).read_file(tmp_path / "secret.yaml")

    assert not called


def test_sops_parse_error_has_no_plaintext():
    backend = SopsYAMLBackend({})
    bad = b'db_user: admin\ndb_password: "hunter2-SECRET\n'

    with pytest.raises(BackendError) as excinfo:
        backend.load(bad)

    e = excinfo.value
    assert "hunter2-SECRET" not in str(e)
    assert "hunter2-SECRET" not in repr(e)
    assert e.__cause__ is None
    assert e.__context__ is None
    assert "line 2" in str(e)


def test_sops_parse_error_plaintext_absent_via_hiera_and_logs(
    monkeypatch, tmp_path, caplog
):
    stdout = b'db_user: admin\ndb_password: "hunter2-SECRET\n'
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.yaml").write_bytes(b"")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: sops\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: secret\n"
        "    path: secret.yaml\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConfigError) as excinfo:
            Hiera(str(config))

    exc = excinfo.value
    seen = []
    while exc is not None:
        seen.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    assert not any("hunter2-SECRET" in s for s in seen)
    assert not any("hunter2-SECRET" in r.getMessage() for r in caplog.records)


def test_sops_parse_error_plaintext_absent_from_cli(
    monkeypatch, tmp_path, capsys, caplog
):
    pytest.importorskip("duho")
    from pyera.cli import main

    stdout = b'db_user: admin\ndb_password: "hunter2-SECRET\n'
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.yaml").write_bytes(b"")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: sops\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: secret\n"
        "    path: secret.yaml\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG):
        rc = main(["k", "-c", str(config)])

    assert rc == 2
    captured = capsys.readouterr()
    assert "hunter2-SECRET" not in captured.out
    assert "hunter2-SECRET" not in captured.err
    assert not any("hunter2-SECRET" in r.getMessage() for r in caplog.records)
