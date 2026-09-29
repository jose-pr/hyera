"""Backend behavior, including the hardened sops backend and error paths."""

import copy
import json
import subprocess
import sys

import pytest

from pyera import BackendError, ConfigError, Hiera, default_backends
from pyera.backends import (
    Backend,
    HOCONBackend,
    JSONBackend,
    SopsBackend,
    YAMLBackend,
    has_hocon,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test in this module gets its own copy of the process-global
    backend registry, so a throwaway registration in one test can never
    collide with another test or with the real built-ins."""
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))


def test_yaml_safeload_never_constructs_python_objects():
    # Psych (unlike plain PyYAML) does not error on an unrecognized tag --
    # it tokenizes the tagged node's own content as if untagged, per
    # to_ruby.rb's default case. Nothing but a plain list of strings is
    # ever constructed either way.
    result = YAMLBackend().loads("!!python/object/apply:os.system ['echo hi']")
    assert result == ["echo hi"]


def test_yaml_parse_error_is_backend_error():
    with pytest.raises(BackendError):
        YAMLBackend().loads("a: b: c: :::")


def test_json_backend_names():
    assert JSONBackend.NAMES["function"] == ("json_data",)
    assert JSONBackend.NAMES["format"] == ("json",)
    assert JSONBackend.NAMES["render"] == ("json",)
    assert Backend.find("json_data") is JSONBackend
    assert Backend.find("json", kind="format") is JSONBackend
    assert Backend.find("json", kind="render") is JSONBackend


def test_json_parse_error_is_backend_error():
    with pytest.raises(BackendError):
        JSONBackend().loads("{not json}")


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


@pytest.mark.parametrize("name", ["yaml", "json", "hocon", "yaml.enc"])
def test_non_puppet_data_hash_names_are_rejected(make_tree, name):
    # Strict Puppet only. These short/legacy names never resolved to a
    # backend registered in the "function" namespace.
    root = make_tree(
        {"hierarchy": [{"name": "c", "data_hash": name, "path": "common.yaml"}]}
    )
    with pytest.raises(ConfigError, match="Unable to find 'data_hash' function"):
        Hiera(str(root / "hiera.yaml"))


def test_backend_data_dir_extension():
    # Non-Puppet extension; this test goes with the feature: accepting the
    # legacy `data_dir` spelling alongside `datadir`.
    assert YAMLBackend({"datadir": "d"}).datadir == "d"
    assert YAMLBackend({"data_dir": "d"}).datadir == "d"
    assert YAMLBackend({}).datadir == ""


def test_sops_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: None)
    backend = SopsBackend({})
    with pytest.raises(BackendError, match="sops executable not found"):
        backend.data_hash(tmp_path / "secret.yaml", {})


def test_sops_nonzero_exit_surfaces_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"decryption failed: no key"

    monkeypatch.setattr("pyera.backends.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="decryption failed: no key"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})


def test_sops_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr("pyera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sops", timeout=30)

    monkeypatch.setattr("pyera.backends.subprocess.run", _raise)
    with pytest.raises(BackendError, match="timed out"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})


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
    # Design Q5: HOCONBackend is *always* registered -- the failure is
    # reachable at level-build (check_available/new) and parse time, not by
    # silently vanishing from default_backends().
    assert HOCONBackend in default_backends()
    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend.check_available()
    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend().loads("k = v")
    with pytest.raises(BackendError, match="pyhocon"):
        Backend.new("hocon_data", {})


def test_unknown_backend_raises_config_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "data_hash": "nonsense", "path": "common.yaml"}]}
    )
    with pytest.raises(ConfigError, match="Unable to find 'data_hash' function"):
        Hiera(str(root / "hiera.yaml"))


def test_missing_hierarchy_raises_config_error(make_tree):
    # `make_tree` only fills in `defaults`/`version`; it never invents a
    # `hierarchy` key, so this still exercises the missing-hierarchy path.
    root = make_tree({"defaults": {"data_hash": "yaml_data"}})
    with pytest.raises(ConfigError, match="hierarchy"):
        Hiera(str(root / "hiera.yaml"))


@pytest.mark.parametrize(
    "top,label",
    [
        ([1, 2], "Tuple"),
        ([], "Array"),
        (None, "Undef"),
        ("x", "String"),
        (1, "Integer"),
        (1.5, "Float"),
        (True, "Boolean"),
    ],
)
def test_json_non_hash_raises_backend_error(make_tree, top, label):
    root = make_tree(
        {"hierarchy": [{"name": "j", "path": "first.json", "data_hash": "json_data"}]},
        files={"data/first.json": json.dumps(top).encode("utf-8")},
    )
    with pytest.raises(
        BackendError, match="expects a Hash value, got {}".format(label)
    ):
        Hiera(str(root / "hiera.yaml"))


def test_yaml_non_hash_warns_and_falls_through(make_tree, caplog):
    import logging

    root = make_tree(
        {"hierarchy": [{"name": "y", "path": "first.yaml"}]},
        files={"data/first.yaml": "- a\n- b\n"},
    )
    with caplog.at_level(logging.WARNING):
        h = Hiera(str(root / "hiera.yaml"))
    assert h.get("anything", default="dflt") == "dflt"
    assert any(
        "does not contain a valid yaml hash" in r.message for r in caplog.records
    )


def test_yaml_none_or_false_never_raises_even_under_strict_error(make_tree):
    for content in ("", "false\n"):
        root = make_tree(
            {"hierarchy": [{"name": "y", "path": "first.yaml"}]},
            files={"data/first.yaml": content},
        )
        backend = YAMLBackend(strict="error")
        assert backend._as_data_hash(YAMLBackend().loads(content), "p") == {}


def test_yaml_non_hash_raises_under_strict_error():
    backend = YAMLBackend(strict="error")
    with pytest.raises(BackendError, match="does not contain a valid yaml hash"):
        backend._as_data_hash(["a"], "p")


@pytest.mark.parametrize(
    "content,expected_err",
    [
        ("﻿".encode("utf-16-le"), "can't decode"),
        (b"\xff\xfe" + "a: b".encode("utf-16-le"), "can't decode"),
    ],
    ids=["utf16-no-bom", "utf16-bom"],
)
def test_yaml_strict_utf8_errors(tmp_path, content, expected_err):
    path = tmp_path / "bad.yaml"
    path.write_bytes(content)
    with pytest.raises(BackendError):
        YAMLBackend().load(path)


def test_json_strict_utf8_errors(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
    with pytest.raises(BackendError):
        JSONBackend().load(path)
