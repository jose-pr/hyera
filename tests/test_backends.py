"""Backend behavior, including the hardened sops backend and error paths."""

import copy
import json
import subprocess
import sys

import pytest

from hyera import BackendError, ConfigError, Hiera, default_backends
from hyera.backends import (
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


def test_sops_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setattr("hyera.backends.shutil.which", lambda _n: None)
    backend = SopsBackend({})
    with pytest.raises(BackendError, match="sops executable not found"):
        backend.data_hash(tmp_path / "secret.yaml", {})


def test_sops_nonzero_exit_surfaces_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr("hyera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    class _Proc:
        returncode = 1
        stdout = b""
        stderr = b"decryption failed: no key"

    monkeypatch.setattr("hyera.backends.subprocess.run", lambda *a, **k: _Proc())
    with pytest.raises(BackendError, match="decryption failed: no key"):
        SopsBackend({}).data_hash(tmp_path / "secret.yaml", {})


def test_sops_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr("hyera.backends.shutil.which", lambda _n: "/usr/bin/sops")

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sops", timeout=30)

    monkeypatch.setattr("hyera.backends.subprocess.run", _raise)
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
    # HOCONBackend is *always* registered -- the failure is
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


def test_missing_hierarchy_uses_puppet_default(make_tree):
    # `make_tree` only fills in `defaults`/`version`; it never invents a
    # `hierarchy` key, so a missing `hierarchy` gets Puppet's own default
    # (a single `Common` level at `common.yaml`) instead of raising.
    root = make_tree(
        {"defaults": {"data_hash": "yaml_data"}},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "v"


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


# ---------------------------------------------------------------------------
# JSON: Ruby's json-gem dialect (comments in; NaN/Infinity, unpaired
# surrogates, and a leading BOM out -- the BOM case is covered above).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"a": 1 /* c */}', {"a": 1}),
        ('{"a": 1 // c\n}', {"a": 1}),
        ('"x"', "x"),
        ("null", None),
        ("123456789012345678901234567890", 123456789012345678901234567890),
        ("1e400", float("inf")),
        ("1E2", 100.0),
        ("-0", 0),
    ],
)
def test_json_accepted_rows(text, expected):
    assert JSONBackend().loads(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        '{"a": 1,}',
        '{"a": "\x01"}',
        "01",
        "",
    ],
)
def test_json_rejected_rows(text):
    with pytest.raises(BackendError):
        JSONBackend().loads(text)


def test_json_duplicate_key_last_wins():
    assert JSONBackend().loads('{"a": 1, "a": 2}') == {"a": 2}


def test_json_comment_like_text_inside_strings_is_kept():
    result = JSONBackend().loads('{"a": "x/*y*/z", "b": "u//v"}')
    assert result == {"a": "x/*y*/z", "b": "u//v"}


def test_json_double_slash_on_last_line_without_newline():
    assert JSONBackend().loads('{"a": 1} // trailing, no newline') == {"a": 1}


def test_json_unterminated_block_comment_errors():
    with pytest.raises(BackendError):
        JSONBackend().loads('{"a": 1 /* never closed')


def test_json_valid_surrogate_pair_combines_to_one_character():
    assert JSONBackend().loads('"\\ud83d\\ude00"') == "\U0001f600"


def test_json_lone_surrogate_in_a_list_or_key_is_rejected():
    with pytest.raises(BackendError, match="incomplete surrogate pair"):
        JSONBackend().loads('["\\ud800"]')
    with pytest.raises(BackendError, match="incomplete surrogate pair"):
        JSONBackend().loads('{"\\ud800": 1}')


# ---------------------------------------------------------------------------
# HOCON: durations stay text; a non-object root errors; the private parser
# copy never touches the shared pyhocon module.
# ---------------------------------------------------------------------------


def test_hocon_durations_stay_text():
    pytest.importorskip("pyhocon")
    result = HOCONBackend().loads(
        "dur = 10s\nx = 10 s\nmix = 10s foo\nd2 = 5 minutes\nn = 10\nf = 1.5\n"
        "sz = 10MB\nnested { a = 3 weeks }\nlst = [1s, 2]\n"
    )
    assert result == {
        "dur": "10s",
        "x": "10 s",
        "mix": "10s foo",
        "d2": "5 minutes",
        "n": 10,
        "f": 1.5,
        "sz": "10MB",
        "nested": {"a": "3 weeks"},
        "lst": ["1s", 2],
    }


def test_hocon_empty_file_is_empty_object():
    pytest.importorskip("pyhocon")
    assert HOCONBackend().loads("") == {}


def test_hocon_non_object_root_raises():
    pytest.importorskip("pyhocon")
    with pytest.raises(BackendError, match="rather than object at file root"):
        HOCONBackend().loads("[1, 2]")


def test_hocon_private_parser_copy_leaves_shared_module_alone():
    pytest.importorskip("pyhocon")
    import datetime

    import pyhocon

    HOCONBackend().loads("d = 10s")
    assert isinstance(
        pyhocon.ConfigFactory.parse_string("d = 10s")["d"], datetime.timedelta
    )


def test_hocon_missing_dependency_names_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyhocon", None)
    with pytest.raises(BackendError, match="hyera\\[hocon\\]"):
        HOCONBackend().loads("k = v")


# ---------------------------------------------------------------------------
# DotenvBackend: reachable only through SopsBackend (see tests/test_sops.py
# for the recorded-pair tests against real sops output); this covers its
# error path directly.
#
# There is no IniBackend: a security review found go-ini's own
# `"""..."""` writer output ambiguous -- a decrypted
# value can inject a key or replace a whole other section, and no ini-text
# parser can tell those bytes apart from a genuine file -- so
# SopsBackend.data_hash always decrypts `ini` as sops's own `--output-type
# json` view and parses it with JSONBackend instead. `ini` was never a
# Puppet data_hash/format name outside this backend, so nothing else
# depended on it; it and its tests were removed rather than kept unused.
# ---------------------------------------------------------------------------


def test_dotenv_backend_no_equals_sign_raises():
    from hyera.backends import DotenvBackend

    with pytest.raises(BackendError, match="invalid dotenv line 1"):
        DotenvBackend().loads("no equals here\n")


def test_dotenv_registered_only_in_format_namespace():
    from hyera.backends import DotenvBackend

    assert "dotenv" in Backend.names("format")
    assert "ini" not in Backend.names("format")
    assert Backend.find("dotenv", kind="function") is None
    assert Backend.find("dotenv", kind="format") is DotenvBackend
    assert Backend.find("ini", kind="format") is None
