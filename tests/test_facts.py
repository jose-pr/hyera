"""``load_facts`` (Puppet's ``--facts`` file rules) and ``facts_from_facter``."""

import subprocess

import pytest

from hyera import BackendError, facts_from_facter, load_facts


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


def test_facts_from_facter_runs_bare_facter_j(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")
    calls = []

    class _Proc:
        returncode = 0
        stdout = b'{"os": {"family": "Debian"}}'
        stderr = b""

    def _run(*args, **kwargs):
        calls.append((args, kwargs))
        return _Proc()

    monkeypatch.setattr("hyera._facts.subprocess.run", _run)
    result = facts_from_facter(timeout=5)

    assert result == {"os": {"family": "Debian"}}
    ((args, kwargs),) = calls
    assert args[0] == ["/usr/bin/facter", "-j"]
    assert kwargs["timeout"] == 5


def test_facts_from_facter_missing_binary(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: None)
    with pytest.raises(BackendError, match="facter executable not found"):
        facts_from_facter()


def test_facts_from_facter_nonzero_exit_surfaces_stderr(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"no facts collected"

    monkeypatch.setattr("hyera._facts.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="no facts collected"):
        facts_from_facter()


def test_facts_from_facter_timeout(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="facter", timeout=30)

    monkeypatch.setattr("hyera._facts.subprocess.run", _raise)
    with pytest.raises(BackendError, match="timed out"):
        facts_from_facter()


def test_facts_from_facter_oserror(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    def _raise(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr("hyera._facts.subprocess.run", _raise)
    with pytest.raises(BackendError, match="Failed to run facter: boom"):
        facts_from_facter()


def test_facts_from_facter_bad_utf8_output(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    class _Proc:
        returncode = 0
        stdout = b"\xff\xfe"
        stderr = b""

    monkeypatch.setattr("hyera._facts.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="facter output is not valid UTF-8"):
        facts_from_facter()


def test_facts_from_facter_bad_json_output(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    class _Proc:
        returncode = 0
        stdout = b"{not json"
        stderr = b""

    monkeypatch.setattr("hyera._facts.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="facter output is not valid JSON"):
        facts_from_facter()


def test_facts_from_facter_nan_constant_output(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    class _Proc:
        returncode = 0
        stdout = b'{"a": NaN}'
        stderr = b""

    monkeypatch.setattr("hyera._facts.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="facter output is not valid JSON"):
        facts_from_facter()


def test_facts_from_facter_non_object_output(monkeypatch):
    monkeypatch.setattr("hyera._facts.shutil.which", lambda _n: "/usr/bin/facter")

    class _Proc:
        returncode = 0
        stdout = b"[1, 2, 3]"
        stderr = b""

    monkeypatch.setattr("hyera._facts.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="not a JSON object"):
        facts_from_facter()
