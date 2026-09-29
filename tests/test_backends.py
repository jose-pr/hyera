"""Backend behavior, including the hardened sops backend and error paths."""

import json
import subprocess
import sys

import pytest

from pyera import BackendError, ConfigError, Hiera, default_backends
from pyera.backends import (
    HOCONBackend,
    JSONBackend,
    SopsYAMLBackend,
    YAMLBackend,
    has_hocon,
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


def test_json_backend_loads(make_tree):
    root = make_tree(
        {
            "defaults": {"data_hash": "json_data"},
            "hierarchy": [{"name": "c", "path": "common.json"}],
        },
        files={"data/common.json": json.dumps({"k": "v"}).encode("utf-8")},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "v"


def test_json_backend_via_alias(make_tree):
    # The short `json` data_hash alias resolves the same backend.
    root = make_tree(
        {
            "defaults": {"data_hash": "json"},
            "hierarchy": [{"name": "c", "path": "common.json"}],
        },
        files={"data/common.json": json.dumps({"k": "aliased"}).encode("utf-8")},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "aliased"


def test_datadir_key_accepts_both_spellings():
    assert YAMLBackend({"datadir": "d"}).datadir == "d"
    assert YAMLBackend({"data_dir": "d"}).datadir == "d"
    assert YAMLBackend({}).datadir == ""


def test_sops_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: None)
    backend = SopsYAMLBackend({})
    with pytest.raises(BackendError, match="sops executable not found"):
        backend.read_file(tmp_path / "secret.yaml")


def test_sops_nonzero_exit_surfaces_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"decryption failed: no key"

    monkeypatch.setattr("pyera.backends.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="decryption failed: no key"):
        SopsYAMLBackend({}).read_file(tmp_path / "secret.yaml")


def test_sops_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sops", timeout=30)

    monkeypatch.setattr("pyera.backends.subprocess.run", _raise)
    with pytest.raises(BackendError, match="timed out"):
        SopsYAMLBackend({}).read_file(tmp_path / "secret.yaml")


def test_hocon_backend(make_tree):
    pytest.importorskip("pyhocon")
    root = make_tree(
        {
            "defaults": {"data_hash": "hocon_data"},
            "hierarchy": [{"name": "c", "path": "common.conf"}],
        },
        files={"data/common.conf": "k = v\nn { a = 1 }\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "v"
    assert h.get("n.a") == 1


def test_hocon_backend_missing_dep_errors(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyhocon", None)
    assert has_hocon() is False
    assert HOCONBackend not in default_backends()
    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend().load(b"k = v")


def test_unknown_backend_raises_config_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "data_hash": "nonsense", "path": "common.yaml"}]}
    )
    with pytest.raises(ConfigError, match="Unknown backend"):
        Hiera(str(root / "hiera.yaml"))


def test_missing_hierarchy_raises_config_error(make_tree):
    # `make_tree` only fills in `defaults`/`version`; it never invents a
    # `hierarchy` key, so this still exercises the missing-hierarchy path.
    root = make_tree({"defaults": {"data_hash": "yaml_data"}})
    with pytest.raises(ConfigError, match="hierarchy"):
        Hiera(str(root / "hiera.yaml"))
