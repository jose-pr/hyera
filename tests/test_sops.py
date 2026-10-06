"""``SopsBackend`` argv safety and plaintext-free parse errors.

Tests that run the real subprocess path put a fake ``sops`` first on
``PATH`` (``fake_program``; skipped on Windows, where a fake ``sops`` is a
batch shim hyera refuses). The remaining tests replace the runner
(``_install_recorder``) to check argv shape and that a decrypted file which
fails to parse never leaks its plaintext into an error, a log record, or
the CLI's output.
"""

import json
import logging
import os
import sys
import time

import pytest

import hyera.backends
from hyera import BackendTimeoutError, ConfigError, Hiera, Scope
from hyera.backends import Backend, BackendError, RubySymbol, SopsBackend


def _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n"):
    """Replace the subprocess runner and record every call as ``(argv, kwargs)``."""
    calls = []

    def _run(program, args, **kwargs):
        calls.append(([program, *args], kwargs))
        return stdout

    monkeypatch.setattr("hyera.backends._sops._run", _run)
    return calls, "sops"


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
    assert h.lookup("k") == "v"


def test_sops_success_argv_and_value(monkeypatch, tmp_path):
    calls, which_path = _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    backend = SopsBackend({})
    secret = tmp_path / "secret.yaml"

    result = backend.data_hash(secret, {})

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [
        which_path,
        "--input-type=yaml",
        "--output-type=yaml",
        "-d",
        "--",
        os.path.abspath(str(secret)),
    ]
    assert kwargs["timeout"] == hyera.backends.SOPS_TIMEOUT
    assert kwargs["refuse_batch"] is True
    assert result == {"k": "v"}


def test_sops_end_to_end_lookup(monkeypatch, tmp_path, make_tree):
    _install_recorder(monkeypatch, tmp_path, stdout=b"k: v\n")
    # Content is irrelevant -- sops runs on the path, and the runner is
    # replaced -- but the level's file must exist for it to be considered.
    root = make_tree(
        {
            "defaults": {"data_hash": "sops_data"},
            "hierarchy": [{"name": "secret", "path": "secret.yaml"}],
        },
        files={"data/secret.yaml": b""},
    )

    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"


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

    h = Hiera("hiera.yaml", scope=Scope(variables={"node": "--output=pwned"}))
    assert h.lookup("k") == "v"

    assert calls, "sops was never invoked for the per-node file"
    args, _kwargs = calls[-1]
    assert args[-2] == "--"
    last = args[-1]
    assert not last.startswith("-")
    assert os.path.isabs(last)


def _write_sops_tree(make_tree, **entry):
    return make_tree(
        {
            "defaults": {"data_hash": "sops_data"},
            "hierarchy": [{"name": "s", "path": "secret.yaml", **entry}],
        },
        files={"data/secret.yaml": b""},
    )


def test_sops_runs_a_real_program_and_passes_the_argv(fake_program, tmp_path):
    log = tmp_path / "argv.json"
    fake_program(
        "sops",
        "import json, sys\njson.dump(sys.argv[1:], open({!r}, 'w'))\n".format(str(log))
        + 'sys.stdout.write("k: v\\n")\n',
    )
    secret = tmp_path / "-rf.yaml"
    assert SopsBackend({}).data_hash(secret, {}) == {"k": "v"}
    with open(log) as fh:
        logged = json.load(fh)
    assert logged == [
        "--input-type=yaml",
        "--output-type=yaml",
        "-d",
        "--",
        os.path.abspath(str(secret)),
    ]


def test_sops_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError, match="sops executable not found") as excinfo:
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert excinfo.value.path == str(tmp_path / "secret.yaml")


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shebang")
def test_sops_start_failure_wraps_oserror(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    program = bindir / "sops"
    program.write_bytes(b"#!/nonexistent/interpreter\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    with pytest.raises(BackendError, match="Failed to run sops"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})


def test_sops_nonzero_exit_keeps_a_bounded_stderr_tail_and_no_stdout(
    fake_program, tmp_path
):
    fake_program(
        "sops",
        """
        import sys
        sys.stdout.write("secret: HUNTER2SECRETVALUE")
        sys.stderr.write("E" * 200000 + "decryption failed: no key")
        sys.exit(128)
        """,
    )
    with pytest.raises(BackendError, match="sops failed \\(exit 128\\)") as excinfo:
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    message = str(excinfo.value)
    assert message.endswith("decryption failed: no key")
    assert "HUNTER2" not in message
    assert len(message) < 2200


def test_sops_failure_reaches_the_caller_with_its_own_text(fake_program, make_tree):
    fake_program("sops", "import sys\nsys.stderr.write('no key')\nsys.exit(1)\n")
    root = _write_sops_tree(make_tree)
    with pytest.raises(BackendError) as excinfo:
        Hiera(str(root / "hiera.yaml")).lookup("k")
    assert str(excinfo.value).startswith("sops failed (exit 1)")
    assert not str(excinfo.value).startswith("Unable to parse")
    assert excinfo.value.path.endswith("secret.yaml")


def test_sops_missing_binary_is_not_labelled_unable_to_parse(
    make_tree, tmp_path, monkeypatch
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    root = _write_sops_tree(make_tree)
    with pytest.raises(BackendError) as excinfo:
        Hiera(str(root / "hiera.yaml")).lookup("k")
    assert str(excinfo.value).startswith("sops executable not found")


def test_sops_timeout_kills_the_whole_process_group(
    fake_program, process_tree, tmp_path
):
    fake_program("sops", process_tree.source)
    # Long enough that the fake has started its grandchild before the kill.
    with pytest.raises(BackendTimeoutError, match="sops timed out after 6s") as e:
        SopsBackend({}, timeout=6).data_hash(tmp_path / "secret.yaml", {})
    assert isinstance(e.value, TimeoutError) and isinstance(e.value, BackendError)
    assert e.value.__cause__ is None and e.value.__context__ is None
    process_tree.assert_killed()


def test_assigning_the_package_timeout_takes_effect(
    fake_program, tmp_path, monkeypatch
):
    fake_program("sops", "import time\ntime.sleep(60)\n")
    monkeypatch.setattr(hyera.backends, "SOPS_TIMEOUT", 0.5)
    started = time.monotonic()
    with pytest.raises(BackendTimeoutError, match="after 0.5s"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert time.monotonic() - started < 10


@pytest.mark.parametrize("bad", [0, -1, "5", True])
def test_sops_timeout_must_be_a_positive_number(bad):
    with pytest.raises(ConfigError, match="timeout"):
        SopsBackend({}, timeout=bad)


def test_sops_invalid_utf8_from_a_real_program(fake_program, tmp_path):
    fake_program(
        "sops", "import sys\nsys.stdout.buffer.write(b'k: \\xff HUNTER2\\n')\n"
    )
    with pytest.raises(BackendError, match="invalid UTF-8 at byte offset 3") as e:
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert "HUNTER2" not in str(e.value)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="redirecting file descriptor 0 does not change the Windows standard handle",
)
def test_sops_stdin_is_the_null_device(fake_program, tmp_path):
    fake_program(
        "sops",
        "import sys\nsys.stdout.write('stdin: ' + repr(sys.stdin.buffer.read().decode()) + chr(10))\n",
    )
    read_end, write_end = os.pipe()
    os.write(write_end, b"caller-input")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        data = SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)
    assert data == {"stdin": ""}


def test_sops_refuses_a_relative_path_entry(fake_program, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    fake_program(
        "sops",
        "import pathlib\npathlib.Path({!r}).write_text('x')\n".format(str(marker)),
        directory=tmp_path / "rel",
        on_path=False,
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "rel")
    with pytest.raises(BackendError, match="relative"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert not marker.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="batch shims are a Windows form")
def test_sops_refuses_a_batch_shim(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sops.bat").write_bytes(
        '@echo off\r\necho x> "{}"\r\n'.format(marker).encode()
    )
    monkeypatch.setenv("PATH", str(bindir))
    with pytest.raises(BackendError, match="batch"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert not marker.exists()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows searches the current directory"
)
def test_sops_never_runs_a_batch_file_from_the_current_directory(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    (tmp_path / "sops.bat").write_bytes(
        '@echo off\r\necho x> "{}"\r\n'.format(marker).encode()
    )
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert not marker.exists()


# `_psych` messages that quote the offending scalar/class name
# verbatim -- `invalid value for Float()/Integer(): "<data>"` and `Tried to
# load unspecified class: <data>` -- reproduced against the fake-sops
# `probe_leak.py` harness (93 payloads through all four formats; these 10
# were the ones that actually leaked as YAML, 2026-09-29).
_QUOTED_FLOAT_LEAK_PAYLOADS = [
    b"a: !!float HUNTER2\n",
    b"a: !!float 'HUNTER2'\n",
    b'a: !!float "HUNTER2"\n',
    b"a: !!float |\n  HUNTER2\n",
    b"a: !<tag:yaml.org,2002:float> HUNTER2\n",
    b"a: !!float 1.HUNTER2\n",
    b"a: !!float 0x1HUNTER2\n",
    b"a: !!float 1__.5e+HUNTER2\n",
]
_QUOTED_CLASS_LEAK_PAYLOADS = [
    b"a: !ruby/object:HUNTER2 {}\n",
    b"a: !ruby/hash:HUNTER2 {}\n",
]
_QUOTED_TOKEN_IDS = [
    "float-bare",
    "float-single-quoted",
    "float-double-quoted",
    "float-block",
    "float-full-tag",
    "float-dot-suffix",
    "float-hex-suffix",
    "float-exp-suffix",
    "ruby-object-class",
    "ruby-hash-class",
]


@pytest.mark.parametrize(
    "bad",
    [
        b"db_password: *HUNTER2\n",
        b"a: &HUNTER2 1\nb: &HUNTER2 2\n",
    ]
    + _QUOTED_FLOAT_LEAK_PAYLOADS
    + _QUOTED_CLASS_LEAK_PAYLOADS,
    ids=["undefined-alias", "duplicate-anchor"] + _QUOTED_TOKEN_IDS,
)
def test_sops_parse_error_strips_quoted_tokens(monkeypatch, tmp_path, bad):
    # These PyYAML/`_psych` error shapes quote the offending scalar
    # or class name verbatim in ``context``/``problem`` (an undefined alias
    # name, a duplicate anchor name, an invalid Float()/Integer() scalar,
    # or a `!ruby/object`/`!ruby/hash` tag's class text) -- exactly the
    # token an attacker-controlled or merely malformed decrypted value
    # could carry. (An unknown *tag* on its own is no longer a parse error
    # under the Psych-compatible loader -- it tokenizes the node's content
    # instead, matching Ruby.)
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
    ]
    + _QUOTED_FLOAT_LEAK_PAYLOADS
    + _QUOTED_CLASS_LEAK_PAYLOADS,
    ids=["undefined-alias", "duplicate-anchor"] + _QUOTED_TOKEN_IDS,
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
        "  datadir: data\n"
        "hierarchy:\n"
        "  - name: secret\n"
        "    path: secret.yaml\n",
        encoding="utf-8",
    )

    h = Hiera(str(config))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BackendError) as excinfo:
            h.lookup("anything")

    exc = excinfo.value
    seen = []
    while exc is not None:
        seen.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    assert not any("HUNTER2" in s for s in seen)
    assert not any("HUNTER2" in r.getMessage() for r in caplog.records)


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

    h = Hiera(str(config))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(BackendError) as excinfo:
            h.lookup("anything")

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
        facts={"role": "web"},
    )
    config = root / "hiera.yaml"

    with caplog.at_level(logging.DEBUG):
        rc = main(
            ["k", "--hiera_config", str(config), "--facts", str(root / "facts.yaml")]
        )

    assert rc == 2
    captured = capsys.readouterr()
    assert "hunter2-SECRET" not in captured.out
    assert "hunter2-SECRET" not in captured.err
    assert not any("hunter2-SECRET" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Format inference (sops's own case-sensitive extension rule,
# `cmd/sops/formats/formats.go`, verified against the real v3.13.3 binary
# and source 2026-09-29 in WSL). Each recorded (format, native stdout) pair below is the exact
# bytes a real `sops -d --input-type=<f> --output-type=<f>` printed for a
# matching input file, captured the same day.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_format,expected_output_type",
    [
        ("a.yaml", "yaml", "yaml"),
        ("a.yml", "yaml", "yaml"),
        ("a.json", "json", "json"),
        ("a.env", "dotenv", "dotenv"),
        (".env", "dotenv", "dotenv"),
        # INI is always decrypted as sops's own JSON view, never as
        # ini text -- sops's INI *writer* is ambiguous (a decrypted value
        # can inject a key or replace a whole other section), so the
        # input type is still ini (that is the file's real format) but
        # the output type forced to json.
        ("a.ini", "ini", "json"),
    ],
)
def test_sops_data_format_inference_accepted(
    monkeypatch, tmp_path, name, expected_format, expected_output_type
):
    calls, _which = _install_recorder(
        monkeypatch,
        tmp_path,
        stdout=b"{}\n" if expected_output_type == "json" else b"",
    )
    path = tmp_path / name
    try:
        SopsBackend({}).data_hash(path, {})
    except BackendError:
        pass  # empty/garbage stdout may fail to parse; only the argv matters here
    assert calls, "sops should have been invoked"
    args, _kwargs = calls[-1]
    assert "--input-type={}".format(expected_format) in args
    assert "--output-type={}".format(expected_output_type) in args


@pytest.mark.parametrize("name", ["a.YAML", "a.enc", "a.conf", "a.yaml.bak"])
def test_sops_data_format_inference_rejected(monkeypatch, tmp_path, name):
    calls, _which = _install_recorder(monkeypatch, tmp_path)
    path = tmp_path / name
    with pytest.raises(ConfigError, match="has no .yaml/.yml/.json/.env/.ini suffix"):
        SopsBackend({}).data_hash(path, {})
    assert not calls, "sops must not be invoked when the format can't be inferred"


def test_run_sops_output_type_defaults_to_input_type(monkeypatch, tmp_path):
    # SopsBackend.data_hash always passes an explicit output_type ("json"
    # for ini, the same format otherwise), so _run_sops's own "default to
    # input_type" branch is never reached through the public API --
    # exercised directly against the private helper instead.
    from hyera.backends._sops import _run_sops

    calls, _which = _install_recorder(monkeypatch, tmp_path, stdout=b"a: 1\n")
    path = tmp_path / "a.yaml"
    path.write_bytes(b"")
    _run_sops(path, "yaml")
    args, _kwargs = calls[-1]
    assert "--output-type=yaml" in args


# Recorded (format, real sops-re-emitted native stdout, expected parsed
# value) triples, captured 2026-09-29 against real sops 3.13.3 + age 1.3.2
# in WSL, decrypting a fixed age key's own encrypted copies of the recorded
# inputs.
_YAML_NATIVE_NO_DATE = (
    b'a: 1\nb:\n    c:\n        - x\n        - "y"\ne: bar\nsym: :foo\n'
    b':q: 1\nbin: hello\nnul: null\nt: "yes"\noct: 493\n'
)
_YAML_NATIVE_WITH_DATE = (
    b'a: 1\nb:\n    c:\n        - x\n        - "y"\nd: 2024-01-15T00:00:00Z\n'
    b'e: bar\nsym: :foo\n:q: 1\nbin: hello\nnul: null\nt: "yes"\noct: 493\n'
)
_JSON_NATIVE = b'{"a": 1, "b": {"c": ["x", "y"]}, "n": null, "f": 1.5}\n'
# INI is always decrypted with --output-type=json, never the ini text a
# real sops would otherwise write; this is that JSON view for the same
# `s.ini` fixture the (now-removed) IniBackend's own recorded-pair test
# used, captured 2026-09-29 against real sops 3.13.3 + age 1.3.2 in WSL
# (`sops -d --input-type=ini --output-type=json -- enc.s.ini`).
_INI_AS_JSON = (
    b'{"DEFAULT": {"top": "1"}, "sec1": {"k": "v", "num": "42", '
    b'"q": "quoted value", "sp": "lead"}, "sec2": {"x": "has=eq", "K": "upper"}}\n'
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
    calls, _which = _install_recorder(monkeypatch, tmp_path, stdout=_INI_AS_JSON)
    result = SopsBackend({}).data_hash(tmp_path / "secret.ini", {})
    assert result == {
        "DEFAULT": {"top": "1"},
        "sec1": {"k": "v", "num": "42", "q": "quoted value", "sp": "lead"},
        "sec2": {"x": "has=eq", "K": "upper"},
    }
    args, _kwargs = calls[-1]
    assert "--input-type=ini" in args
    assert "--output-type=json" in args


def test_sops_data_ini_writer_ambiguity_is_not_reachable(monkeypatch, tmp_path):
    # Regression test: a real sops 3.13.3 encrypt/decrypt of
    # `{"db": {"password": "real-secret"}, "zz": {"note": "x\"\"\"\n[db]\n
    # password = attacker\nq = \"\"\""}}` as ini writes an ambiguous
    # `[db]\npassword = real-secret\n\n[zz]\nnote = """x"""\n[db]\n
    # password = attacker\nq = """"""\n` that the (now-removed) ini text
    # parser read back as `db.password == "attacker"`. Decrypting via
    # sops's own JSON view instead (what SopsBackend now always does for
    # ini) recovers the genuine value.
    stdout = (
        b'{"DEFAULT": {}, "db": {"password": "real-secret"}, "zz": '
        b'{"note": "x\\"\\"\\"\\n[db]\\npassword = attacker\\nq = \\"\\"\\""}}\n'
    )
    _install_recorder(monkeypatch, tmp_path, stdout=stdout)
    result = SopsBackend({}).data_hash(tmp_path / "secret.ini", {})
    assert result["db"]["password"] == "real-secret"


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
        # ini is always decrypted+parsed as sops's own JSON view (see
        # SopsBackend.data_hash), so a malformed decrypted ini payload
        # fails the same JSON parse as the "json" case above, never the
        # (now-removed) IniBackend's own "invalid ini line N".
        ("ini", b'{"a": HUNTER2}\n', "Unable to parse"),
        ("env", b"HUNTER2_no_equals_sign\n", "invalid dotenv line 1"),
    ],
)
def test_sops_data_secret_free_for_json_ini_dotenv(
    monkeypatch, tmp_path, caplog, ext, stdout, match
):
    # A malformed decrypted payload that genuinely fails to parse for each
    # of the three non-YAML formats. dotenv's line-error message never
    # embeds the line's own text at all (by construction -- only the line
    # number); JSON's (and ini's) parse-error path goes through
    # the same chain-free "Unable to parse" wrapping YAML already had
    # covered above.
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


def test_sops_data_invalid_utf8_reports_byte_offset_only(monkeypatch, tmp_path):
    # A lone continuation byte is never valid UTF-8 at any position -- the
    # message names the byte offset only, never the offending byte value
    # or any surrounding plaintext.
    _install_recorder(monkeypatch, tmp_path, stdout=b"a: 1\n\x80\n")
    with pytest.raises(
        BackendError, match=r"invalid UTF-8 at byte offset \d+"
    ) as excinfo:
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})
    assert "\\x80" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# The `sops` alias and the `sops_<format>` pattern name.
# ---------------------------------------------------------------------------


def test_sops_names_registered():
    names = Backend.names()
    # `sops_data`/`sops` are exact names, in definition order; every pattern
    # name (regardless of where its class is defined) sorts after every
    # exact name (`Backend.names()`'s own contract).
    assert "sops_data" in names and "sops" in names
    assert names.index("sops_data") < names.index("sops")
    assert names[-1] == "sops_<yaml|json|ini|dotenv>"
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
    # sops_ini forces ini (as the sops --input-type) even over a
    # .yaml-looking name; the output is still decrypted as JSON.
    calls, _which = _install_recorder(
        monkeypatch, tmp_path, stdout=b'{"DEFAULT": {}, "s": {"k": "v"}}\n'
    )
    path = tmp_path / "secret.yaml"
    result = Backend.new("sops_ini", {}).data_hash(path, {})
    assert result == {"DEFAULT": {}, "s": {"k": "v"}}
    args, _kwargs = calls[-1]
    assert "--input-type=ini" in args
    assert "--output-type=json" in args


def test_sops_timeout_resolution_order(monkeypatch, tmp_path):
    # The constructor keyword, else the package attribute read at call time.
    calls, _program = _install_recorder(monkeypatch, tmp_path)
    secret = tmp_path / "secret.yaml"
    monkeypatch.setattr(hyera.backends, "SOPS_TIMEOUT", 7)
    SopsBackend({}).data_hash(secret, {})
    SopsBackend({}, timeout=3).data_hash(secret, {})
    assert [kwargs["timeout"] for _argv, kwargs in calls] == [7, 3]
