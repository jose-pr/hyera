"""``hiera.yaml`` reading: ``HieraConfig.create`` (``_hiera_config.py``).

Copying and absolutizing the base config, Puppet's version dispatch, the
empty/non-mapping-file fallback, Puppet's `defaults`/`hierarchy`/`datadir`
fallbacks, and Puppet's version 5 schema validation. Per-level function
kind selection is a later addition here, once that phase lands.
"""

import copy
import io
from pathlib import Path

import pytest

from hyera import ConfigError, Hiera, Scope


def test_dict_config_is_not_mutated(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text("k: v\n", encoding="utf-8")
    cfg = {
        "version": 5,
        "defaults": {"datadir": "data", "data_hash": "yaml_data"},
        "hierarchy": [{"name": "common", "path": "common.yaml"}],
    }
    snapshot = copy.deepcopy(cfg)

    h = Hiera(cfg, base_path=str(tmp_path))

    assert h.lookup("k") == "v"
    assert cfg == snapshot


def test_relative_config_path_survives_chdir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel" / "data").mkdir(parents=True)
    (tmp_path / "rel" / "hiera.yaml").write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: yaml_data}\n"
        b"hierarchy:\n"
        b'  - {name: node, path: "nodes/%{certname}.yaml"}\n'
        b"  - {name: common, path: common.yaml}\n"
    )
    (tmp_path / "rel" / "data" / "common.yaml").write_bytes(b"k: common\n")

    h = Hiera("rel/hiera.yaml")
    assert h._base_path.is_absolute()

    # A cwd that no longer has any relation to the config: `base_path` was
    # made absolute at construction, so the lookup below must not re-derive
    # anything from the current cwd.
    monkeypatch.chdir(tmp_path / "rel" / "data")
    assert h.lookup("k") == "common"


def test_none_config_uses_puppet_default(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: common\n"},
    )
    (root / "hiera.yaml").unlink()

    h = Hiera(None, base_path=str(root))

    assert h.lookup("k") == "common"


def test_missing_config_file_raises(tmp_path):
    missing = tmp_path / "hiera.yaml"

    with pytest.raises(ConfigError) as exc:
        Hiera(str(missing))

    assert str(exc.value.path).endswith("hiera.yaml")


@pytest.mark.parametrize(
    "version, expected",
    [
        (None, "entry 'hierarchy' variant 0 expects a String value, got Tuple"),
        (3, "entry 'hierarchy' variant 0 expects a String value, got Tuple"),
        (4, "entry 'hierarchy' index 0 expects a value for key 'backend'"),
        (6, "does not support hiera.yaml version 6"),
        ("abc", "does not support hiera.yaml version 0"),
        (True, "got Boolean"),
        (5.0, "got Float"),
    ],
    ids=["absent", "v3", "v4", "v6", "str-abc", "bool-true", "float-5.0"],
)
def test_version_dispatch(version, expected):
    cfg = {
        "defaults": {"datadir": "data", "data_hash": "yaml_data"},
        "hierarchy": [{"name": "common", "path": "common.yaml"}],
    }
    if version is not None:
        cfg["version"] = version

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert expected in str(exc.value)


@pytest.mark.parametrize(
    "defaults_yaml, expected_dir",
    [
        ("{data_hash: yaml_data}", "data"),
        ("{data_hash: yaml_data, datadir: other}", "other"),
    ],
    ids=["no-datadir-anywhere", "defaults-datadir"],
)
def test_entry_datadir_fallback(make_tree, defaults_yaml, expected_dir):
    # Written as raw YAML, not through `make_tree`'s dict mode -- that mode
    # always fills in a `datadir`, which would hide the very fallback this
    # test exists to exercise.
    root = make_tree(
        "version: 5\n"
        "defaults: {}\n"
        "hierarchy:\n"
        "  - {{name: common, path: common.yaml}}\n".format(defaults_yaml),
        files={"{}/common.yaml".format(expected_dir): "k: v\n"},
    )

    h = Hiera(str(root / "hiera.yaml"))

    assert h.lookup("k") == "v"


_BASE_V5 = {
    "version": 5,
    "defaults": {"datadir": "data", "data_hash": "yaml_data"},
}


@pytest.mark.parametrize(
    "cfg, expected",
    [
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {
                        "name": "one",
                        "data_hash": "yaml_data",
                        "mapped_paths": ["a", "b"],
                    }
                ],
            },
            "expects size to be 3, got 2",
        ),
        (
            {**_BASE_V5, "hierarchy": {"a": 1}},
            "expects an Array value, got Struct",
        ),
        (
            {**_BASE_V5, "hierarchy": ["oops"]},
            "index 0 expects a Struct value, got String",
        ),
        (
            {
                "version": 5,
                "defaults": ["a"],
                "hierarchy": [
                    {"name": "one", "path": "one.yaml", "data_hash": "yaml_data"}
                ],
            },
            "expects a Struct value, got Tuple",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {"name": "one", "path": "one.yaml", "data_hash": ["yaml_data"]}
                ],
            },
            "entry 'data_hash' expects a String value, got Tuple",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {"name": "one", "paths": "one.yaml", "data_hash": "yaml_data"}
                ],
            },
            "expects an Array value, got String",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [{"path": "one.yaml", "data_hash": "yaml_data"}],
            },
            "expects a value for key 'name'",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [{"name": "one", "paths": [], "data_hash": "yaml_data"}],
            },
            "at least 1, got 0",
        ),
    ],
    ids=[
        "mapped-paths-2-tuple",
        "hierarchy-dict",
        "hierarchy-string-entry",
        "defaults-list",
        "data-hash-list",
        "paths-string",
        "missing-name",
        "paths-empty",
    ],
)
def test_malformed_config_raises_config_error(cfg, expected):
    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert expected in str(exc.value)


def test_config_error_names_file_and_line(tmp_path):
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: yaml_data}\n"
        b"hierarchy:\n"
        b"  - {name: common, pathz: common.yaml}\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert str(exc.value.path).endswith("hiera.yaml")
    assert exc.value.line == 4
    assert "(line: 4)" in str(exc.value)


def test_dict_config_error_has_no_line():
    cfg = {**_BASE_V5, "hierarchy": {"a": 1}}

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert exc.value.path is None
    assert exc.value.line is None
    assert "<dict>" in str(exc.value)


def test_file_like_config_error_has_line_but_no_path():
    # A file-like source (unlike a real path) has readable text -- so a
    # line number is still found -- but no filesystem path of its own;
    # _config_error's "line but no path" suffix is distinct from either of
    # the two cases above.
    stream = io.StringIO(
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data}\n"
        "hierarchy:\n"
        "  - {name: common, pathz: common.yaml}\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(stream)

    assert exc.value.path is None
    assert exc.value.line == 4
    assert str(exc.value).endswith("(line: 4)")


def test_default_hierarchy_entries_are_validated():
    cfg = {
        **_BASE_V5,
        "hierarchy": [{"name": "common", "path": "common.yaml"}],
        "default_hierarchy": [{"path": "defaults.yaml"}],
    }

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert "entry 'default_hierarchy' index 0 expects a value for key 'name'" in str(
        exc.value
    )


def test_duplicate_names_report_both_lines(tmp_path):
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: yaml_data}\n"
        b"hierarchy:\n"
        b"  - name: same\n"
        b"    path: first.yaml\n"
        b"  - name: other\n"
        b"    path: common.yaml\n"
        b"  - name: same\n"
        b"    path: common.yaml\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert "First defined at (line: 4)" in str(exc.value)
    assert exc.value.line == 8


def test_hiera3_backend_replaced_by_data_hash():
    cfg = {
        "version": 5,
        "defaults": {"datadir": "data"},
        "hierarchy": [
            {"name": "common", "path": "common.yaml", "hiera3_backend": "json"}
        ],
    }

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert 'Use "data_hash: json_data" instead of "hiera3_backend: json"' in str(
        exc.value
    )


def test_lookup_key_entry_does_not_inherit_data_hash(make_tree):
    # A `lookup_key` entry must never fall back to `defaults`' `data_hash`
    # and read its file as plain YAML -- with real eyaml data, that would
    # return ciphertext as the value instead of the decrypted plaintext.
    pytest.importorskip("cryptography")
    key_path = str(
        Path(__file__).resolve().parent
        / "conformance"
        / "cases"
        / "backend-eyaml-pkcs7"
        / "keys"
        / "private_key.pkcs7.pem"
    )
    root = make_tree(
        {
            "defaults": {"data_hash": "yaml_data"},
            "hierarchy": [
                {
                    "name": "secret",
                    "lookup_key": "eyaml_lookup_key",
                    "path": "secret.eyaml",
                    "options": {"pkcs7_private_key": key_path},
                }
            ],
        },
        files={
            "data/secret.eyaml": (
                "plain: ENC[PKCS7,MIIBiQYJKoZIhvcNAQcDoIIBejCCAXYCAQAxggEhMIIB"
                "HQIBADAFMAACAQAwDQYJKoZIhvcNAQEBBQAEggEAUxMeECBRt6S3CUuSBrPq"
                "gJMeVmfTz32pZZDYxT8STIJH/fcJwH8dXBtJXO1+cORUStymhaSFRBon4s2C"
                "U1ivZh/Y7FPGELpv0DgO7p6FbjrBj3KTGRBoLPwLvF7c7g1mKIX+wVfqY5J6"
                "CeNPazoZ7OdymtpIOVtVk6iM+DNoFJiJ1ExiflNj/evx/7LL4p8DxEUn7SBx"
                "4GzVe1Tbixh1HXPOucWZf2gS9Q6oF5AonmesYV81tK7ZVnMWq0L6ofDEw7rn"
                "V4WwOg3jvbZqUVTfW7VmWCNH/WRc9H0q8ALF6iFyLcVzgGR9FzkY6pwuA09D"
                "gIz9K4nALPOkMbDPwz4GADBMBgkqhkiG9w0BBwEwHQYJYIZIAWUDBAEqBBCW"
                "vJ27L0KBIZxF4C0PAWFbgCC+x0smWw5EliuxtFyWGVFTSqtZZV/Ew3zhxy8I"
                "3B1r0Q==]\n"
            )
        },
    )

    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    assert h.lookup("plain") == "s3cr3t RedHat"


@pytest.mark.parametrize(
    "entry, expected",
    [
        (
            {"lookup_key": "yaml_data"},
            "'yaml_data' expects 2 arguments, got 3",
        ),
        (
            {"hiera3_backend": "foo"},
            "Hiera 3 backend 'foo' is not available",
        ),
        ({"v4_data_hash": "x"}, "Unable to find 'v4_data_hash' function named 'x'"),
    ],
    ids=["lookup-key-registered", "hiera3-backend-unmapped", "v4-data-hash"],
)
def test_function_kind_errors(entry, expected):
    cfg = {
        "version": 5,
        "defaults": {"datadir": "data"},
        "hierarchy": [{"name": "one", "path": "one.yaml", **entry}],
    }

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert expected in str(exc.value)
