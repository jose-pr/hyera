"""Backend behavior, including the hardened sops backend and error paths."""

import json

import pytest

from hiera import BackendError, ConfigError, Hiera
from hiera.backends import (
    JSONBackend,
    SopsYAMLBackend,
    YAMLBackend,
)


def test_yaml_safeload_rejects_python_objects():
    # SafeLoader must not construct arbitrary Python objects.
    with pytest.raises(BackendError):
        YAMLBackend().load(b"!!python/object/apply:os.system ['echo hi']")


def test_yaml_parse_error_is_backend_error():
    with pytest.raises(BackendError):
        YAMLBackend().load(b"a: b: c: :::")


def test_json_backend_names():
    assert "json_data" in JSONBackend.NAMES
    assert "json" in JSONBackend.NAMES


def test_json_parse_error_is_backend_error():
    with pytest.raises(BackendError):
        JSONBackend().load(b"{not json}")


def test_json_backend_loads(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.json").write_text(json.dumps({"k": "v"}))
    (tmp_path / "hiera.yaml").write_text(
        "defaults:\n  data_hash: json_data\n  data_dir: data\n"
        "hierarchy:\n  - name: c\n    path: common.json\n"
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("k") == "v"


def test_json_backend_via_alias(tmp_path):
    # The short `json` data_hash alias resolves the same backend.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.json").write_text(json.dumps({"k": "aliased"}))
    (tmp_path / "hiera.yaml").write_text(
        "defaults:\n  data_hash: json\n  data_dir: data\n"
        "hierarchy:\n  - name: c\n    path: common.json\n"
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("k") == "aliased"


def test_datadir_key_accepts_both_spellings():
    assert YAMLBackend({"datadir": "d"}).datadir == "d"
    assert YAMLBackend({"data_dir": "d"}).datadir == "d"
    assert YAMLBackend({}).datadir == ""


def test_sops_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr("hiera.backends.shutil.which", lambda _n: None)
    backend = SopsYAMLBackend({})
    with pytest.raises(BackendError, match="sops executable not found"):
        backend.read_file(tmp_path / "secret.yaml")


def test_sops_nonzero_exit_surfaces_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr("hiera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"decryption failed: no key"

    monkeypatch.setattr("hiera.backends.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="decryption failed: no key"):
        SopsYAMLBackend({}).read_file(tmp_path / "secret.yaml")


def test_sops_timeout(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr("hiera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sops", timeout=30)

    monkeypatch.setattr("hiera.backends.subprocess.run", _raise)
    with pytest.raises(BackendError, match="timed out"):
        SopsYAMLBackend({}).read_file(tmp_path / "secret.yaml")


def test_hocon_backend(tmp_path):
    pytest.importorskip("pyhocon")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.conf").write_text("k = v\nn { a = 1 }\n")
    (tmp_path / "hiera.yaml").write_text(
        "version: 5\n"
        "defaults:\n  data_hash: hocon_data\n  data_dir: data\n"
        "hierarchy:\n  - name: c\n    path: common.conf\n"
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("k") == "v"
    assert h.get("n.a") == 1


def test_hocon_backend_missing_dep_errors():
    from hiera.backends import HOCONBackend, has_hocon

    if has_hocon():
        pytest.skip("pyhocon is installed")
    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend().load(b"k = v")


def test_unknown_backend_raises_config_error(tmp_path):
    (tmp_path / "hiera.yaml").write_text(
        "hierarchy:\n  - name: c\n    data_hash: nonsense\n    path: common.yaml\n"
    )
    with pytest.raises(ConfigError, match="Unknown backend"):
        Hiera(str(tmp_path / "hiera.yaml"))


def test_missing_hierarchy_raises_config_error(tmp_path):
    (tmp_path / "hiera.yaml").write_text("defaults:\n  data_hash: yaml_data\n")
    with pytest.raises(ConfigError, match="hierarchy"):
        Hiera(str(tmp_path / "hiera.yaml"))
