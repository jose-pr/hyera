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

from hyera import ConfigError, Hiera
from hyera.backends import SOPS_TIMEOUT, Backend, BackendError, RubySymbol, SopsBackend


def _install_recorder(
    monkeypatch, tmp_path, stdout=b"k: v\n", stderr=b"sops: noise", returncode=0
):
    """Patch ``shutil.which``/``subprocess.run`` and record every call."""
    calls = []
    which_path = str(tmp_path / "bin" / "sops.exe")
    monkeypatch.setattr("hyera.backends.shutil.which", lambda _name: which_path)

    def _run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr("hyera.backends.subprocess.run", _run)
    return calls, which_path


def test_data_hash_sops_is_sops_data(monkeypatch, tmp_path, make_tree):
    # `sops` is an alias for `sops_data` -- over the same mocked
    # decrypt, the two names must produce identical results.
    _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    root = make_tree(
        {
            "defaults": {"data_hash": "sops"},
            "hierarchy": [{"name": "secret", "path": "secret.yaml"}],
        },
        files={"data/secret.yaml": b""},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "v"


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
    monkeypatch.setattr("hyera.backends.shutil.which", lambda _n: ".\\sops.EXE")
    called = []
    monkeypatch.setattr(
        "hyera.backends.subprocess.run", lambda *a, **k: called.append((a, k))
    )

    with pytest.raises(BackendError, match="relative"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})

    assert not called


def test_sops_refuses_batch_shim(monkeypatch, tmp_path):
    monkeypatch.setattr("hyera.backends.shutil.which", lambda _n: r"C:\tools\sops.CMD")
    called = []
    monkeypatch.setattr(
        "hyera.backends.subprocess.run", lambda *a, **k: called.append((a, k))
    )

    with pytest.raises(BackendError, match="batch"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})

    assert not called


@pytest.mark.parametrize(
    "bad",
    [
        b"db_password: *HUNTER2\n",
        b"a: &HUNTER2 1\nb: &HUNTER2 2\n",
    ],
    ids=["undefined-alias", "duplicate-anchor"],
)
def test_sops_parse_error_strips_quoted_tokens(monkeypatch, tmp_path, bad):
    # These two PyYAML error shapes quote the offending scalar verbatim in
    # ``context``/``problem`` (an undefined alias name, or a duplicate
    # anchor name) -- exactly the token an attacker-controlled or merely
    # malformed decrypted value could carry. (An unknown *tag* is no longer
    # a parse error under the Psych-compatible loader -- it tokenizes the
    # node's content instead, matching Ruby.)
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
        b"a: &HUNTER2 1\nb: &HUNTER2 2\n",
    ],
    ids=["undefined-alias", "duplicate-anchor"],
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
        "hyera.backends.shutil.which",
        lambda _n: str(tmp_path / "bin" / "sops.exe"),
    )
    monkeypatch.setattr("hyera.backends.subprocess.run", _timeout)

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
    from hyera.cli import main

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


# ---------------------------------------------------------------------------
# Format inference (sops's own case-sensitive extension rule,
# `cmd/sops/formats/formats.go`, verified against the real v3.13.3 binary
# and source 2026-09-29 -- see the sub-plan's own Progress for the WSL
# capture). Each recorded (format, native stdout) pair below is the exact
# bytes a real `sops -d --input-type=<f> --output-type=<f>` printed for a
# matching input file, captured the same day.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_format",
    [
        ("a.yaml", "yaml"),
        ("a.yml", "yaml"),
        ("a.json", "json"),
        ("a.env", "dotenv"),
        (".env", "dotenv"),
        ("a.ini", "ini"),
    ],
)
def test_sops_data_format_inference_accepted(
    monkeypatch, tmp_path, name, expected_format
):
    calls, _which = _install_recorder(
        monkeypatch, tmp_path, stdout=b"{}\n" if expected_format == "json" else b""
    )
    path = tmp_path / name
    try:
        SopsBackend({}).data_hash(path, {})
    except BackendError:
        pass  # empty/garbage stdout may fail to parse; only the argv matters here
    assert calls, "sops should have been invoked"
    args, _kwargs = calls[-1]
    assert "--input-type={}".format(expected_format) in args
    assert "--output-type={}".format(expected_format) in args


@pytest.mark.parametrize("name", ["a.YAML", "a.enc", "a.conf", "a.yaml.bak"])
def test_sops_data_format_inference_rejected(monkeypatch, tmp_path, name):
    calls, _which = _install_recorder(monkeypatch, tmp_path)
    path = tmp_path / name
    with pytest.raises(ConfigError, match="has no .yaml/.yml/.json/.env/.ini suffix"):
        SopsBackend({}).data_hash(path, {})
    assert not calls, "sops must not be invoked when the format can't be inferred"


# Recorded (format, real sops-re-emitted native stdout, expected parsed
# value) triples, captured 2026-09-29 against real sops 3.13.3 + age 1.3.2
# in WSL, decrypting a fixed age key's own encrypted copies of the inputs
# named in the sub-plan.
_YAML_NATIVE_NO_DATE = (
    b'a: 1\nb:\n    c:\n        - x\n        - "y"\ne: bar\nsym: :foo\n'
    b':q: 1\nbin: hello\nnul: null\nt: "yes"\noct: 493\n'
)
_YAML_NATIVE_WITH_DATE = (
    b'a: 1\nb:\n    c:\n        - x\n        - "y"\nd: 2024-01-15T00:00:00Z\n'
    b'e: bar\nsym: :foo\n:q: 1\nbin: hello\nnul: null\nt: "yes"\noct: 493\n'
)
_JSON_NATIVE = b'{"a": 1, "b": {"c": ["x", "y"]}, "n": null, "f": 1.5}\n'
_INI_NATIVE = (
    b"top = 1\n\n[sec1]\nk   = v\nnum = 42\n; c\nq   = quoted value\nsp  = lead\n\n"
    b"[sec2]\nx = has=eq\nK = upper\n"
)
_ENV_NATIVE = (
    b'# top comment\nK1=v1\nK2=has=eq\nK3=line1\\nline2\nK4= spaced \nK5="quoted"\n'
)


def test_sops_data_yaml_recorded_pair(monkeypatch, tmp_path):
    _install_recorder(monkeypatch, tmp_path, stdout=_YAML_NATIVE_NO_DATE)
    result = SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert result == {
        "a": 1,
        "b": {"c": ["x", "y"]},
        "e": "bar",
        "sym": RubySymbol("foo"),
        "q": 1,
        "bin": "hello",
        "nul": None,
        "t": "yes",
        "oct": 493,
    }


def test_sops_data_yaml_date_shaped_value_is_disallowed(monkeypatch, tmp_path):
    _install_recorder(monkeypatch, tmp_path, stdout=_YAML_NATIVE_WITH_DATE)
    with pytest.raises(BackendError, match="unspecified class: Time"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})


def test_sops_data_json_recorded_pair(monkeypatch, tmp_path):
    _install_recorder(monkeypatch, tmp_path, stdout=_JSON_NATIVE)
    result = SopsBackend({}).data_hash(tmp_path / "secret.json", {})
    assert result == {"a": 1, "b": {"c": ["x", "y"]}, "n": None, "f": 1.5}


def test_sops_data_ini_recorded_pair(monkeypatch, tmp_path):
    _install_recorder(monkeypatch, tmp_path, stdout=_INI_NATIVE)
    result = SopsBackend({}).data_hash(tmp_path / "secret.ini", {})
    assert result == {
        "DEFAULT": {"top": "1"},
        "sec1": {"k": "v", "num": "42", "q": "quoted value", "sp": "lead"},
        "sec2": {"x": "has=eq", "K": "upper"},
    }


def test_sops_data_dotenv_recorded_pair(monkeypatch, tmp_path):
    _install_recorder(monkeypatch, tmp_path, stdout=_ENV_NATIVE)
    result = SopsBackend({}).data_hash(tmp_path / "secret.env", {})
    assert result == {
        "K1": "v1",
        "K2": "has=eq",
        "K3": "line1\nline2",
        "K4": " spaced ",
        "K5": '"quoted"',
    }


@pytest.mark.parametrize(
    "ext,stdout,match",
    [
        ("json", b'{"a": HUNTER2}\n', "Unable to parse"),
        ("ini", b"HUNTER2_no_equals_sign\n", "invalid ini line 1"),
        ("env", b"HUNTER2_no_equals_sign\n", "invalid dotenv line 1"),
    ],
)
def test_sops_data_secret_free_for_json_ini_dotenv(
    monkeypatch, tmp_path, caplog, ext, stdout, match
):
    # A malformed decrypted payload that genuinely fails to parse for each
    # of the three non-YAML formats. INI/dotenv line-error messages never
    # embed the line's own text at all (by construction -- only the line
    # number); JSON's parse-error path goes through the same chain-free
    # "Unable to parse" wrapping YAML already had covered above.
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BackendError, match=match) as excinfo:
            SopsBackend({}).data_hash(tmp_path / "secret.{}".format(ext), {})
    exc = excinfo.value
    seen = []
    while exc is not None:
        seen.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    assert not any("HUNTER2" in s for s in seen)
    assert not any("HUNTER2" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The `sops` alias and the `sops_<format>` pattern name.
# ---------------------------------------------------------------------------


def test_sops_names_registered():
    names = Backend.names()
    assert names[-3:] == ["sops_data", "sops", "sops_<yaml|json|ini|dotenv>"]
    assert Backend.find("sops_toml") is None


@pytest.mark.parametrize("data_hash", ["ini", "dotenv"])
def test_ini_and_dotenv_are_not_data_hash_names(make_tree, data_hash):
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "secret.ini", "data_hash": data_hash}]},
    )
    with pytest.raises(ConfigError, match="Unable to find 'data_hash' function"):
        Hiera(str(root / "hiera.yaml"))


def test_sops_format_pattern_forces_format_regardless_of_extension(
    monkeypatch, tmp_path
):
    calls, _which = _install_recorder(monkeypatch, tmp_path, stdout=b'{"a": 1}\n')
    path = tmp_path / "secrets.enc"
    result = Backend.new("sops_json", {}).data_hash(path, {})
    assert result == {"a": 1}
    args, _kwargs = calls[-1]
    assert "--input-type=json" in args
    assert "--output-type=json" in args


def test_sops_format_pattern_overrides_extension_inference(monkeypatch, tmp_path):
    # sops_ini forces ini even over a .yaml-looking name.
    calls, _which = _install_recorder(monkeypatch, tmp_path, stdout=b"[s]\nk = v\n")
    path = tmp_path / "secret.yaml"
    result = Backend.new("sops_ini", {}).data_hash(path, {})
    assert result == {"DEFAULT": {}, "s": {"k": "v"}}
    args, _kwargs = calls[-1]
    assert "--input-type=ini" in args
