"""``hiera.yaml`` reading: ``HieraConfig.create`` (``_hiera_config.py``).

Copying and absolutizing the base config, Puppet's version dispatch, the
empty/non-mapping-file fallback, Puppet's `defaults`/`hierarchy`/`datadir`
fallbacks, and Puppet's version 5 schema validation. Per-level function
kind selection is a later addition here, once that phase lands.
"""

import copy

import pytest

from hyera import ConfigError, Hiera


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

    assert h.get("k") == "v"
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
    assert h.base_path.is_absolute()

    # A cwd that no longer has any relation to the config: `base_path` was
    # made absolute at construction, so the lookup below must not re-derive
    # anything from the current cwd.
    monkeypatch.chdir(tmp_path / "rel" / "data")
    assert h.get("k") == "common"


def test_none_config_uses_puppet_default(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: common\n"},
    )
    (root / "hiera.yaml").unlink()

    h = Hiera(None, base_path=str(root))

    assert h.get("k") == "common"


def test_missing_config_file_raises(tmp_path):
    missing = tmp_path / "hiera.yaml"

    with pytest.raises(ConfigError) as exc:
        Hiera(str(missing))

    assert str(exc.value.path).endswith("hiera.yaml")


@pytest.mark.parametrize(
    "version, expected",
    [
        (None, "hiera.yaml version 3 is not supported yet"),
        (3, "hiera.yaml version 3 is not supported yet"),
        (4, "cannot be used in the global layer"),
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


@pytest.mark.parametrize("text", ["", "- a\n"], ids=["empty", "non-mapping"])
def test_empty_or_non_mapping_config_file_raises(make_tree, text):
    root = make_tree(text, raw=True)

    with pytest.raises(ConfigError, match="does not contain a valid YAML hash"):
        Hiera(str(root / "hiera.yaml"))


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

    assert h.get("k") == "v"


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
