"""``hiera.yaml`` version 4: schema errors, defaults, paths, backends and use in
the global layer."""

import logging

import pytest
from pathlib_next import Path

from hyera import ConfigError, Hiera
from hyera._config.config_source import _ConfigSource
from hyera._config.config_v4 import _validate_v4

# --- version 4 ---------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected_lines",
    [
        (
            {
                "version": 4,
                "datadir": "data",
                "hierarchy": [{"backend": "yaml", "glob": "*.yaml"}],
            },
            ["expects a value for key 'name'", "unrecognized key 'glob'"],
        ),
        (
            {"version": 4, "datadir": "data", "hierarchy": {"a": "b"}},
            ["expects an Array value, got Struct"],
        ),
        (
            {
                "version": "4",
                "datadir": "data",
                "hierarchy": [{"name": "c", "backend": "yaml"}],
            },
            ["expects an Integer value, got String"],
        ),
        (
            {
                "version": 4,
                "datadir": "data",
                "hierarchy": [
                    {"name": "c", "backend": "yaml", "data_hash": "yaml_data"}
                ],
                "defaults": {},
            },
            ["unrecognized key 'data_hash'", "unrecognized key 'defaults'"],
        ),
        (
            {"version": 4, "datadir": "data", "hierarchy": ["oops"]},
            ["index 0 expects a Struct value, got String"],
        ),
        (
            {
                "version": 4,
                "datadir": 5,
                "hierarchy": [{"name": "c", "backend": "yaml"}],
            },
            ["entry 'datadir' expects a String value, got Integer"],
        ),
        (
            {
                "version": 4,
                "datadir": "data",
                "hierarchy": [{"name": 5, "backend": "yaml"}],
            },
            ["index 0 entry 'name' expects a String value, got Integer"],
        ),
        (
            {
                "version": 4,
                "datadir": "data",
                "hierarchy": [{"name": "c", "backend": "yaml", "paths": "not-a-list"}],
            },
            ["index 0 entry 'paths' expects an Array value, got String"],
        ),
        (
            {
                "version": 4,
                "datadir": "data",
                "hierarchy": [{"name": "c", "backend": "yaml", "paths": [5]}],
            },
            ["index 0 entry 'paths' index 0 expects a String value, got Integer"],
        ),
    ],
    ids=[
        "missing-name-and-extra-key",
        "hierarchy-hash",
        "version-string",
        "nested-before-top-level",
        "entry-is-a-string",
        "datadir-integer",
        "entry-name-integer",
        "paths-not-a-list",
        "paths-item-integer",
    ],
)
def test_v4_schema_errors(data, expected_lines):
    with pytest.raises(ConfigError) as excinfo:
        _validate_v4(data, _ConfigSource("<test>", None, None, Path(".")))
    text = str(excinfo.value)
    for line in expected_lines:
        assert line in text
    if len(expected_lines) > 1:
        # A nested (per-entry) mismatch always precedes a top-level one.
        assert text.index(expected_lines[0]) < text.index(expected_lines[-1])


def test_v4_defaults_filled(make_tree):
    root = make_tree(
        {"hierarchy": []},
        files={
            "modules/mymod/hiera.yaml": "version: 4\n",
            "modules/mymod/data/common.yaml": "mymod::k: v\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    assert h.lookup("mymod::k") == "v"


def test_v4_paths_and_extension(make_tree):
    root = make_tree(
        {"hierarchy": []},
        files={
            "modules/mymod/hiera.yaml": (
                "version: 4\n"
                "hierarchy:\n"
                "  - name: byname\n"
                "    backend: yaml\n"
                "  - name: pathswins\n"
                "    backend: json\n"
                "    path: unused\n"
                "    paths: [a, b.json]\n"
                "  - name: owndir\n"
                "    backend: yaml\n"
                "    datadir: other\n"
                "    path: c\n"
            ),
            "modules/mymod/data/byname.yaml": "mymod::k1: from_name\n",
            "modules/mymod/data/a.json": '{"mymod::k2": "from_a"}\n',
            "modules/mymod/data/b.json": '{"mymod::k2x": "from_b"}\n',
            "modules/mymod/other/c.yaml": "mymod::k3: from_owndir\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    # path defaults to name.
    assert h.lookup("mymod::k1") == "from_name"
    # paths wins over path (unused.json is never read); b.json's own
    # extension is not doubled.
    assert h.lookup("mymod::k2") == "from_a"
    # an entry's own datadir overrides the config's.
    assert h.lookup("mymod::k3") == "from_owndir"


def test_v4_hocon_backend_default_extension_and_dispatch(make_tree):
    # backend: hocon is a data_hash/hocon_data level with default extension ".conf"
    # (not yaml/json's "." + name rule); the lookup finds the value in c.conf, written
    # with the quoted "mymod::k" key a HOCON file needs for a module-prefixed name.
    pytest.importorskip("pyhocon")
    root = make_tree(
        {"hierarchy": []},
        files={
            "modules/mymod/hiera.yaml": (
                "version: 4\nhierarchy:\n  - name: c\n    backend: hocon\n"
            ),
            "modules/mymod/data/c.conf": '"mymod::k" = v\n',
        },
    )
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    assert h.lookup("mymod::k") == "v"


def test_v4_datadir_is_literal(make_tree):
    root = make_tree(
        {"hierarchy": []},
        files={
            "modules/mymod/hiera.yaml": (
                "version: 4\n"
                'datadir: "d%{x}"\n'
                "hierarchy:\n  - name: common\n    backend: yaml\n"
            ),
            "modules/mymod/d%{x}/common.yaml": "mymod::k: literal\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    assert h.lookup("mymod::k") == "literal"


@pytest.mark.parametrize("backend", ["sops", "eyaml"])
def test_v4_unknown_backend(make_tree, backend):
    root = make_tree(
        {"hierarchy": []},
        files={
            "modules/mymod/hiera.yaml": (
                "version: 4\nhierarchy:\n  - name: s\n    backend: {}\n".format(backend)
            ),
        },
    )
    # A module-layer provider is loaded lazily, on first use of a
    # qualified key -- not at construction.
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    with pytest.raises(
        ConfigError,
        match="No data provider is registered for backend '{}'".format(backend),
    ) as excinfo:
        h.lookup("mymod::s")
    assert excinfo.value.line == 4


def test_v4_duplicate_name_reports_lines(make_tree):
    root = make_tree(
        {"hierarchy": []},
        files={
            "modules/mymod/hiera.yaml": (
                "version: 4\n"
                "hierarchy:\n"
                "  - name: same\n"
                "    backend: yaml\n"
                "  - name: same\n"
                "    backend: json\n"
            ),
        },
    )
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    with pytest.raises(ConfigError) as excinfo:
        h.lookup("mymod::same")
    assert "First defined at (line: 3)" in str(excinfo.value)
    assert excinfo.value.line == 5


def test_v4_duplicate_name_dict_config_has_no_lines():
    # Same duplicate-name message as above, but a dict-configured Hiera has
    # no text to search, so neither a first nor a second line is found.
    cfg = {
        "version": 4,
        "hierarchy": [
            {"name": "same", "backend": "yaml"},
            {"name": "same", "backend": "json"},
        ],
    }
    with pytest.raises(ConfigError) as excinfo:
        Hiera(cfg)
    assert "Hierarchy name 'same' defined more than once." in str(excinfo.value)
    assert "First defined at" not in str(excinfo.value)
    assert excinfo.value.line is None


def test_v4_in_global_layer_valid_file_gives_layer_error(make_tree, caplog):
    root = make_tree(
        "version: 4\ndatadir: data\nhierarchy:\n  - name: common\n    backend: yaml\n",
        raw=True,
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(
            ConfigError, match="hiera.yaml version 4 cannot be used in the global layer"
        ):
            Hiera(str(root / "hiera.yaml"))
    # The deprecation warning is logged before the layer error is raised.
    assert len(caplog.records) == 1
    assert "is deprecated" in caplog.records[0].message


def test_v4_in_global_layer_invalid_file_gives_schema_error(make_tree):
    root = make_tree('version: 4\nhierarchy: ["oops"]\n', raw=True)
    with pytest.raises(
        ConfigError, match="entry 'hierarchy' index 0 expects a Struct value"
    ):
        Hiera(str(root / "hiera.yaml"))
