"""``SopsBackend`` argv safety and plaintext-free parse errors.

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
from pyera.backends import SOPS_TIMEOUT, BackendError, SopsBackend


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


def test_data_hash_sops_not_registered(make_tree):
    # A later change adds the `sops` name; until
    # then it is simply unknown, exactly like any other unregistered name.
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "secret.yaml", "data_hash": "sops"}]},
    )
    with pytest.raises(ConfigError, match="Unable to find 'data_hash' function"):
        Hiera(str(root / "hiera.yaml"))


def test_sops_success_argv_and_value(monkeypatch, tmp_path):
    calls, which_path = _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    backend = SopsBackend({})
    secret = tmp_path / "secret.yaml"

    result = backend.data_hash(secret, {})

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
    assert result == {"k": "v"}


def test_sops_end_to_end_lookup(monkeypatch, tmp_path, make_tree):
    _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    # Content is irrelevant -- sops runs on the path, and subprocess.run is
    # mocked -- but the level's file must exist for it to be considered.
    root = make_tree(
        {
            "defaults": {"data_hash": "sops_data"},
            "hierarchy": [{"name": "secret", "path": "secret.yaml"}],
        },
        files={"data/secret.yaml": b""},
    )

    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "v"


def test_sops_dash_leading_filename_is_data(monkeypatch, tmp_path, make_tree):
    calls, _which_path = _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    make_tree(
        {
            "defaults": {"data_hash": "sops_data"},
            "hierarchy": [{"name": "node", "path": "%{node}.yaml"}],
        },
        files={"data/--output=pwned.yaml": b""},
    )
    monkeypatch.chdir(tmp_path)

    h = Hiera("hiera.yaml")
    assert h.get("k", context={"node": "--output=pwned"}) == "v"

    assert calls, "sops was never invoked for the per-node file"
    args, _kwargs = calls[-1]
    assert args[-2] == "--"
    last = args[-1]
    assert not last.startswith("-")
    assert os.path.isabs(last)


def test_sops_refuses_relative_which_result(monkeypatch, tmp_path):
    # Python 3.9's shutil.which searches the cwd first and can return a
    # relative path even with NoDefaultCurrentDirectoryInExePath set; running
    # whatever that resolves to would be the same implicit-cwd exposure the
    # absolute-path hardening elsewhere in this module is meant to close.
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: ".\\sops.EXE")
    called = []
    monkeypatch.setattr(
        "pyera.backends.subprocess.run", lambda *a, **k: called.append((a, k))
    )

    with pytest.raises(BackendError, match="relative"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})

    assert not called


def test_sops_refuses_batch_shim(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: r"C:\tools\sops.CMD")
    called = []
    monkeypatch.setattr(
        "pyera.backends.subprocess.run", lambda *a, **k: called.append((a, k))
    )

    with pytest.raises(BackendError, match="batch"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})

    assert not called


@pytest.mark.parametrize(
    "bad",
    [
        b"db_password: *HUNTER2\n",
        b"db_password: !HUNTER2\n",
        b"a: &HUNTER2 1\nb: &HUNTER2 2\n",
    ],
    ids=["undefined-alias", "unknown-tag", "duplicate-anchor"],
)
def test_sops_parse_error_strips_quoted_tokens(monkeypatch, tmp_path, bad):
    # These three PyYAML error shapes quote the offending scalar verbatim in
    # ``context``/``problem`` (an undefined alias name, an unknown tag, or a
    # duplicate anchor name) -- exactly the token an attacker-controlled or
    # merely malformed decrypted value could carry.
    _install_recorder(monkeypatch, tmp_path, stdout=bad)
    backend = SopsBackend({})

    with pytest.raises(BackendError) as excinfo:
        backend.data_hash(tmp_path / "secret.yaml", {})

    e = excinfo.value
    assert "HUNTER2" not in str(e)
    assert "HUNTER2" not in repr(e)
    assert e.__cause__ is None
    assert e.__context__ is None


@pytest.mark.parametrize(
    "stdout",
    [
        b"db_password: *HUNTER2\n",
        b"db_password: !HUNTER2\n",
        b"a: &HUNTER2 1\nb: &HUNTER2 2\n",
    ],
    ids=["undefined-alias", "unknown-tag", "duplicate-anchor"],
)
def test_sops_parse_error_quoted_tokens_absent_via_hiera_and_logs(
    monkeypatch, tmp_path, caplog, stdout
):
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secret.yaml").write_bytes(b"")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: sops_data\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: secret\n"
        "    path: secret.yaml\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BackendError) as excinfo:
            Hiera(str(config))

    exc = excinfo.value
    seen = []
    while exc is not None:
        seen.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    assert not any("HUNTER2" in s for s in seen)
    assert not any("HUNTER2" in r.getMessage() for r in caplog.records)


def test_sops_timeout_chain_free(monkeypatch, tmp_path):
    # A TimeoutExpired carries the subprocess's partial stdout as an
    # attribute; chaining "from e" keeps that reachable via __cause__.stdout
    # even though the BackendError's own message never echoes it.
    def _timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args, timeout=SOPS_TIMEOUT, output=b"partial-HUNTER2", stderr=b""
        )

    monkeypatch.setattr(
        "pyera.backends.shutil.which",
        lambda _n: str(tmp_path / "bin" / "sops.exe"),
    )
    monkeypatch.setattr("pyera.backends.subprocess.run", _timeout)

    with pytest.raises(BackendError) as excinfo:
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_sops_parse_error_has_no_plaintext(monkeypatch, tmp_path):
    bad = b'db_user: admin\ndb_password: "hunter2-SECRET\n'
    _install_recorder(monkeypatch, tmp_path, stdout=bad)
    backend = SopsBackend({})

    with pytest.raises(BackendError) as excinfo:
        backend.data_hash(tmp_path / "secret.yaml", {})

    e = excinfo.value
    assert "hunter2-SECRET" not in str(e)
    assert "hunter2-SECRET" not in repr(e)
    assert e.__cause__ is None
    assert e.__context__ is None
    assert str(e).startswith("Unable to parse (")
    assert str(e).endswith(
        "found unexpected end of stream while scanning a quoted scalar "
        "at line 2 column 14"
    )


def test_sops_parse_error_plaintext_absent_via_hiera_and_logs(
    monkeypatch, tmp_path, caplog, make_tree
):
    stdout = b'db_user: admin\ndb_password: "hunter2-SECRET\n'
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    root = make_tree(
        {
            "defaults": {"data_hash": "sops_data"},
            "hierarchy": [{"name": "secret", "path": "secret.yaml"}],
        },
        files={"data/secret.yaml": b""},
    )
    config = root / "hiera.yaml"

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BackendError) as excinfo:
            Hiera(str(config))

    exc = excinfo.value
    seen = []
    while exc is not None:
        seen.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    assert not any("hunter2-SECRET" in s for s in seen)
    assert not any("hunter2-SECRET" in r.getMessage() for r in caplog.records)


def test_sops_parse_error_plaintext_absent_from_cli(
    monkeypatch, tmp_path, capsys, caplog, make_tree
):
    pytest.importorskip("duho")
    from pyera.cli import main

    stdout = b'db_user: admin\ndb_password: "hunter2-SECRET\n'
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    root = make_tree(
        {
            "defaults": {"data_hash": "sops_data"},
            "hierarchy": [{"name": "secret", "path": "secret.yaml"}],
        },
        files={"data/secret.yaml": b""},
    )
    config = root / "hiera.yaml"

    with caplog.at_level(logging.DEBUG):
        rc = main(["k", "-c", str(config)])

    assert rc == 2
    captured = capsys.readouterr()
    assert "hunter2-SECRET" not in captured.out
    assert "hunter2-SECRET" not in captured.err
    assert not any("hunter2-SECRET" in r.getMessage() for r in caplog.records)
