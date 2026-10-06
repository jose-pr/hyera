"""``load_facts`` (Puppet's ``--facts`` file rules) and ``facts_from_facter``."""

import os
import shutil
import signal
import sys
import threading

import pytest

from hyera import (
    BackendError,
    BackendTimeoutError,
    facts_from_facter,
    load_facts,
)


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_load_facts_json(tmp_path):
    path = _write(
        tmp_path, "facts.json", '{"os": {"family": "Suse"}, "flag": true, "n": 3}'
    )
    assert load_facts(path) == {"os": {"family": "Suse"}, "flag": True, "n": 3}


def test_load_facts_yaml_extensions(tmp_path):
    for name in ("facts.yaml", "facts.yml"):
        path = _write(tmp_path, name, "os:\n  family: Debian\n")
        assert load_facts(path) == {"os": {"family": "Debian"}}


def test_load_facts_other_extension_tries_json_then_yaml(tmp_path):
    json_path = _write(tmp_path, "facts.txt", '{"os": {"family": "Arch"}}')
    assert load_facts(json_path) == {"os": {"family": "Arch"}}

    yaml_path = _write(tmp_path, "facts2.txt", "os:\n  family: Arch\n")
    assert load_facts(yaml_path) == {"os": {"family": "Arch"}}


def test_load_facts_must_be_a_mapping(tmp_path):
    a_list = _write(tmp_path, "facts_list.yaml", "- a\n- b\n")
    with pytest.raises(BackendError, match="Incorrectly formatted data"):
        load_facts(a_list)

    empty = _write(tmp_path, "empty.yaml", "")
    with pytest.raises(BackendError, match="Incorrectly formatted data"):
        load_facts(empty)

    scalar = _write(tmp_path, "scalar.json", '"just a string"')
    with pytest.raises(BackendError, match="Incorrectly formatted data"):
        load_facts(scalar)


def test_load_facts_parse_errors_name_the_file(tmp_path):
    bad_json = _write(tmp_path, "bad.json", "{not valid json")
    with pytest.raises(BackendError, match="bad.json"):
        load_facts(bad_json)

    bad_yaml = _write(tmp_path, "bad.yaml", "a: b: c: :::")
    with pytest.raises(BackendError, match="bad.yaml"):
        load_facts(bad_yaml)

    nan_json = _write(tmp_path, "nan.json", '{"x": NaN}')
    with pytest.raises(BackendError):
        load_facts(nan_json)


def test_load_facts_invalid_utf8_names_the_file(tmp_path):
    bad_json = tmp_path / "bad_utf8.json"
    bad_json.write_bytes(b"\xff\xfe{}")
    with pytest.raises(BackendError, match="bad_utf8.json"):
        load_facts(str(bad_json))

    bad_yaml = tmp_path / "bad_utf8.yaml"
    bad_yaml.write_bytes(b"\xff\xfek: v\n")
    with pytest.raises(BackendError, match="bad_utf8.yaml"):
        load_facts(str(bad_yaml))


def test_load_facts_rejects_yaml_date_time_symbol(tmp_path):
    date_path = _write(tmp_path, "d.yaml", "d: 2024-01-01\n")
    with pytest.raises(BackendError, match="unspecified class: Date"):
        load_facts(date_path)

    time_path = _write(tmp_path, "t.yaml", "t: 2024-01-01 10:00:00\n")
    with pytest.raises(BackendError, match="unspecified class: Time"):
        load_facts(time_path)

    sym_path = _write(tmp_path, "s.yaml", ":sym: 1\n")
    with pytest.raises(BackendError, match="unspecified class: Symbol"):
        load_facts(sym_path)


def test_load_facts_other_extension_with_date_is_incorrectly_formatted(tmp_path):
    # A date is neither valid JSON nor an acceptable YAML fact; the lenient
    # any-extension path swallows both failures (including the disallowed
    # YAML class), landing on "no result" -> "Incorrectly formatted data".
    path = _write(tmp_path, "facts.dat", "d: 2024-01-01\n")
    with pytest.raises(BackendError, match="Incorrectly formatted data"):
        load_facts(path)


def test_load_facts_trusted_facts_all_or_none(tmp_path):
    partial = _write(tmp_path, "partial.yaml", "fqdn: web01.example.com\n")
    with pytest.raises(BackendError, match="must all be overridden"):
        load_facts(partial)

    full = _write(
        tmp_path,
        "full.yaml",
        "hostname: web01\ndomain: example.com\nfqdn: web01.example.com\n"
        "clientcert: web01.example.com\n",
    )
    facts = load_facts(full)
    assert facts["clientcert"] == "web01.example.com"


# facts_from_facter runs a real program: each test below puts a fake
# `facter` first on PATH (tests/conftest.py `fake_program`).

_FACTER_OK = """
import json, sys
print(json.dumps({"argv": sys.argv[1:]}))
"""


def test_facts_from_facter_runs_bare_facter_j(fake_program):
    # On Windows the fake is a facter.bat at an absolute path, which is allowed:
    # only a relative resolution is refused.
    fake_program("facter", _FACTER_OK)
    assert facts_from_facter(timeout=30) == {"argv": ["-j"]}


def test_facts_from_facter_missing_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError, match="facter executable not found"):
        facts_from_facter()


def test_facts_from_facter_nonzero_exit_keeps_a_bounded_stderr_tail(fake_program):
    fake_program(
        "facter",
        """
        import sys
        sys.stdout.write("STDOUT-PAYLOAD")
        sys.stderr.write("E" * 200000 + "the-last-line")
        sys.exit(3)
        """,
    )
    with pytest.raises(BackendError, match="facter failed \\(exit 3\\)") as excinfo:
        facts_from_facter()
    message = str(excinfo.value)
    assert message.endswith("the-last-line")
    assert len(message) < 2100
    assert "STDOUT-PAYLOAD" not in message


def test_facts_from_facter_timeout_kills_the_whole_process_group(
    fake_program, process_tree
):
    fake_program("facter", process_tree.source)
    # Long enough that the fake has started its grandchild before the kill.
    with pytest.raises(BackendTimeoutError, match="facter timed out after 6s") as e:
        facts_from_facter(timeout=6)
    assert isinstance(e.value, TimeoutError)
    assert isinstance(e.value, BackendError)
    assert e.value.__context__ is None
    process_tree.assert_killed()


@pytest.mark.parametrize(
    "emit, message",
    [
        ('sys.stdout.buffer.write(b"\\xff\\xfe")', "not valid UTF-8"),
        ('sys.stdout.write("{not json")', "not valid JSON"),
        ("sys.stdout.write('{\"a\": NaN}')", "not valid JSON"),
        ('sys.stdout.write("[1, 2, 3]")', "not a JSON object"),
    ],
)
def test_facts_from_facter_rejects_bad_output(fake_program, emit, message):
    fake_program("facter", "import sys\n" + emit + "\n")
    with pytest.raises(BackendError, match=message):
        facts_from_facter()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shebang")
def test_facts_from_facter_start_failure_wraps_oserror(tmp_path, monkeypatch):
    # Found on PATH, but its interpreter does not exist, so it cannot start.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    program = bindir / "facter"
    program.write_bytes(b"#!/nonexistent/interpreter\n")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    with pytest.raises(BackendError, match="Failed to run facter"):
        facts_from_facter()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="redirecting file descriptor 0 does not change the Windows standard handle",
)
def test_facts_from_facter_stdin_is_the_null_device(fake_program):
    fake_program(
        "facter",
        """
        import json, sys
        print(json.dumps({"stdin": sys.stdin.buffer.read().decode()}))
        """,
    )
    read_end, write_end = os.pipe()
    os.write(write_end, b"caller-input")
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    try:
        facts = facts_from_facter()
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(read_end)
    assert facts == {"stdin": ""}


def test_facts_from_facter_refuses_a_relative_path_entry(
    fake_program, tmp_path, monkeypatch
):
    # A relative PATH entry resolves to a path relative to the current directory.
    fake_program("facter", _FACTER_OK, directory=tmp_path / "rel", on_path=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "rel")
    with pytest.raises(BackendError, match="relative"):
        facts_from_facter()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows searches the current directory"
)
def test_facts_from_facter_never_runs_a_batch_file_from_the_current_directory(
    tmp_path, monkeypatch
):
    # Python 3.9 to 3.11 resolve a facter.bat in the cwd to `.\facter.BAT`.
    marker = tmp_path / "planted-ran"
    (tmp_path / "facter.bat").write_bytes(
        '@echo off\r\necho x> "{}"\r\necho {{"planted": "yes"}}\r\n'.format(
            marker
        ).encode()
    )
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError):
        facts_from_facter()
    assert not marker.exists()


@pytest.mark.skipif(shutil.which("facter") is None, reason="facter is not installed")
def test_facts_from_facter_real_facter():
    facts = facts_from_facter(timeout=120)
    assert isinstance(facts, dict) and facts
    assert "os" in facts


def _interrupt_main():
    if sys.platform == "win32":
        signal.raise_signal(signal.SIGINT)
    else:
        # A real signal, so the blocked wait in the main thread is woken.
        os.kill(os.getpid(), signal.SIGINT)


@pytest.mark.skipif(
    sys.platform == "win32" and sys.version_info < (3, 10),
    reason="an interrupt does not wake a blocked wait on Windows before Python 3.10",
)
def test_facts_from_facter_interrupt_kills_the_child_and_its_children(
    fake_program, process_tree
):
    fake_program("facter", process_tree.source)

    def fire():
        process_tree.wait_ready()
        _interrupt_main()

    threading.Thread(target=fire, daemon=True).start()
    with pytest.raises(KeyboardInterrupt) as excinfo:
        facts_from_facter(timeout=120)
    assert excinfo.value.__context__ is None
    process_tree.assert_killed()


def _chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        yield exc
        exc = exc.__cause__ or exc.__context__


@pytest.mark.parametrize(
    "name,raw",
    [
        ("bad.json", b'{"HUNTER2SECRET": [1, 2'),
        ("bad.yaml", b"HUNTER2SECRET: [1, 2\n"),
        ("latin1.json", b'{"HUNTER2SECRET": "caf\xe9"}'),
        ("latin1.yaml", b"HUNTER2SECRET: caf\xe9\n"),
    ],
)
def test_facts_errors_carry_no_document_in_the_chain(tmp_path, name, raw):
    path = tmp_path / name
    path.write_bytes(raw)
    with pytest.raises(BackendError) as excinfo:
        load_facts(path)
    for exc in _chain(excinfo.value):
        assert "HUNTER2SECRET" not in repr(vars(exc))
        assert "HUNTER2SECRET" not in repr(exc.args)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


@pytest.mark.parametrize("name", ["facts.json", "facts.yaml"])
def test_deeply_nested_facts_file_raises_backend_error(tmp_path, name):
    nest = "[" * 5000 + "]" * 5000
    text = '{"k": ' + nest + "}" if name.endswith(".json") else "k: " + nest
    path = _write(tmp_path, name, text)
    with pytest.raises(BackendError, match="nested too deeply"):
        load_facts(path)


def test_json_facts_accept_integers_over_the_interpreters_digit_limit(tmp_path):
    path = _write(tmp_path, "facts.json", '{"n": ' + "9" * 5000 + "}")
    assert load_facts(path)["n"] == 10**5000 - 1
