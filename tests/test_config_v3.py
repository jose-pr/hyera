"""``hiera.yaml`` version 3: schema errors, defaults, deprecation warnings and backend ordering."""

import logging

import pytest

from hyera import ConfigError, Hiera, KeyNotFoundError, Scope
from hyera._config.hiera_config import (
    V3_DEFAULT_CONFIG_HASH,
)
from hyera._config.config_v3 import _fill_v3_defaults
from hyera.backends._psych import RubySymbol


@pytest.mark.parametrize(
    "config, match",
    [
        (
            {"backends": ["yaml"], "hierarchy": ["common"], "foo": "bar"},
            r"unrecognized key 'foo'",
        ),
        (
            {"backends": ["yaml"], "yaml": "data", "hierarchy": ["common"]},
            r"entry 'yaml' expects a Hash value, got String",
        ),
        (
            {
                "backends": ["yaml"],
                "yaml": {"datadir": "data"},
                "hierarchy": ["common"],
                "logger": "console",
                "json": {"datadir": "nope"},
            },
            r"unrecognized key 'json'",
        ),
        (
            {"backends": ["yaml"], "hierarchy": ["common"], "merge_behavior": "array"},
            r"entry 'merge_behavior' expects a match for "
            r"Enum\['deep', 'deeper', 'native'\], got 'array'",
        ),
        (
            {
                "backends": ["yaml"],
                "hierarchy": ["common"],
                "merge_behavior": RubySymbol("deeper"),
            },
            r"entry 'merge_behavior' expects a match for Enum\[.*\], got Runtime",
        ),
        (
            {"backends": ["yaml"], "hierarchy": {"a": "b"}},
            r"entry 'hierarchy' expects a value of type String or Array, got Struct",
        ),
        (
            {"backends": ["yaml"], "hierarchy": ""},
            r"expects a value of type String\[1\] or Array\[String\[1\]\], got String",
        ),
        (
            {"backends": ["yaml"], "hierarchy": [5, "common"]},
            r"entry 'hierarchy' variant 0 expects a String value, got Tuple",
        ),
        (
            {"version": "3", "backends": ["yaml"], "hierarchy": ["common"]},
            r"entry 'version' expects an Integer value, got String",
        ),
        (
            {
                "backends": ["yaml"],
                "hierarchy": ["common"],
                "deep_merge_options": {"knockout_prefix": 5},
            },
            r"entry 'deep_merge_options' entry 'knockout_prefix' expects a value of "
            r"type String or Boolean, got Integer",
        ),
        (
            {"backends": 5, "hierarchy": ["common"]},
            r"entry 'backends' expects a value of type String or Array, got Integer",
        ),
        (
            {"backends": ["yaml"], "hierarchy": ["common"], "logger": ""},
            r"entry 'logger' expects a String\[1\] value, got String",
        ),
        (
            {"backends": ["yaml"], "hierarchy": ["common"], "logger": 5},
            r"entry 'logger' expects a String value, got Integer",
        ),
        (
            {"backends": ["yaml"], "hierarchy": ["", "common"]},
            r"entry 'hierarchy' variant 1 index 0 expects a String\[1\] value, got String",
        ),
        (
            {
                "backends": ["yaml"],
                "hierarchy": ["common"],
                "deep_merge_options": "nope",
            },
            r"entry 'deep_merge_options' expects a Hash value, got String",
        ),
        (
            {
                "backends": ["yaml"],
                "hierarchy": ["common"],
                "deep_merge_options": {5: True},
            },
            r"entry 'deep_merge_options' key of entry '5' expects a String\[1\] "
            r"value, got Integer",
        ),
        (
            {
                "backends": ["yaml"],
                "hierarchy": ["common"],
                "deep_merge_options": {"": True},
            },
            r"entry 'deep_merge_options' key of entry '' expects a String\[1\] "
            r"value, got String",
        ),
        (
            {"backends": ["yaml"], "yaml": {"datadir": 5}, "hierarchy": ["common"]},
            r"entry 'yaml' entry 'datadir' expects a String value, got Integer",
        ),
        (
            {"backends": ["yaml"], "yaml": {5: "x"}, "hierarchy": ["common"]},
            r"entry 'yaml' key of entry '5' expects a String\[1\] value, got Integer",
        ),
        (
            {"backends": ["yaml"], "hierarchy": ["common"], "logger": None},
            r"entry 'logger' expects a String value, got Undef",
        ),
        (
            {"backends": ["yaml"], "hierarchy": {}},
            r"entry 'hierarchy' expects a value of type String or Array, got Hash",
        ),
    ],
    ids=[
        "unknown-top-key",
        "backend-conf-string",
        "unlisted-backend-conf",
        "bad-merge-behavior",
        "symbol-merge-behavior",
        "hierarchy-hash",
        "hierarchy-empty-string",
        "hierarchy-integer-entry",
        "version-string",
        "deep-merge-options-type",
        "backends-integer",
        "logger-empty",
        "logger-integer",
        "hierarchy-empty-item",
        "deep-merge-options-not-a-hash",
        "deep-merge-options-integer-key",
        "deep-merge-options-empty-key",
        "backend-conf-datadir-integer",
        "backend-conf-integer-key",
        "logger-undef",
        "hierarchy-empty-hash",
    ],
)
def test_v3_config_schema_errors(config, match):
    with pytest.raises(ConfigError, match=match):
        Hiera(config)


def test_v3_bare_string_backends_and_hierarchy_are_valid():
    # `backends`/`hierarchy` as a bare (non-empty) string, not a list, is
    # Puppet's own Variant[String[1], Array[String[1]]] shorthand -- no
    # mismatch at all, and `_v3_backend_names` treats it as one name.
    with pytest.raises(KeyNotFoundError):
        Hiera({"backends": "yaml", "hierarchy": "common"}).lookup("k")


def test_v3_mismatches_in_puppet_order():
    # A v5-shaped file with no `version:` key is read as v3 (Hiera 1/2/3's
    # own dialect); every mismatch against the v3 schema is reported, not
    # just the first (unlike v5's own `_validate_v5`).
    config = {
        "defaults": {"datadir": "data", "data_hash": "yaml_data"},
        "hierarchy": [{"name": "common", "path": "common.yaml"}],
    }
    with pytest.raises(ConfigError) as excinfo:
        Hiera(config)
    lines = str(excinfo.value).splitlines()
    assert lines[-3].endswith("variant 0 expects a String value, got Tuple")
    assert lines[-2].endswith("variant 1 index 0 expects a String value, got Struct")
    assert lines[-1].endswith("unrecognized key 'defaults'")


def test_fill_v3_defaults():
    data = {}
    _fill_v3_defaults(data)
    assert data == {
        "version": 3,
        "backends": list(V3_DEFAULT_CONFIG_HASH["backends"]),
        "hierarchy": list(V3_DEFAULT_CONFIG_HASH["hierarchy"]),
        "merge_behavior": V3_DEFAULT_CONFIG_HASH["merge_behavior"],
        "deep_merge_options": {},
    }

    data = {"backends": False}
    _fill_v3_defaults(data)
    assert data["version"] == 3
    assert data["backends"] == list(V3_DEFAULT_CONFIG_HASH["backends"])
    assert data["hierarchy"] == list(V3_DEFAULT_CONFIG_HASH["hierarchy"])
    assert data["merge_behavior"] == V3_DEFAULT_CONFIG_HASH["merge_behavior"]
    assert data["deep_merge_options"] == {}


_INVALID_V3_TEXT = (
    ":backends: [yaml]\n:yaml:\n  :datadir: data\n:hierarchy: [common]\n"
    ":merge_behavior: array\n"
)


def test_v3_symbol_keyed_file_is_validated(make_tree):
    # Proves symbol keys are normalized (`symkeys_to_string`) before v3
    # validation runs, not just for v5.
    root = make_tree(_INVALID_V3_TEXT, raw=True)
    with pytest.raises(ConfigError, match="entry 'merge_behavior'"):
        Hiera(str(root / "hiera.yaml"))


def test_v3_deprecation_warning_precedes_validation_error(make_tree, caplog):
    root = make_tree(_INVALID_V3_TEXT, raw=True)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConfigError, match="entry 'merge_behavior'"):
            Hiera(str(root / "hiera.yaml"))
    assert len(caplog.records) == 1
    assert (
        "Use of 'hiera.yaml' version 3 is deprecated. It should be converted "
        "to version 5" in caplog.records[0].message
    )


def test_v3_deprecation_warning_silent_when_strict_off(make_tree, caplog):
    root = make_tree(_INVALID_V3_TEXT, raw=True)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ConfigError, match="entry 'merge_behavior'"):
            Hiera(str(root / "hiera.yaml"), scope=Scope(strict="off"))
    assert not caplog.records


@pytest.mark.parametrize(
    "text", ["", "- a\n- b\n", "~\n"], ids=["empty", "list", "null"]
)
def test_non_mapping_file_falls_back_to_v3_default(make_tree, caplog, tmp_path, text):
    # V3_DEFAULT_CONFIG_HASH is itself schema-valid, so the fallback now
    # resolves a real lookup through the default codedir-rooted datadir,
    # instead of raising (the version 3 provider build did not exist yet
    # when this fallback could only ever raise "not supported yet").
    root = make_tree(text, raw=True)
    codedir = tmp_path / "code"
    hieradata = codedir / "environments" / "production" / "hieradata"
    hieradata.mkdir(parents=True)
    (hieradata / "common.yaml").write_text("k: fallback\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        h = Hiera(str(root / "hiera.yaml"), codedir=str(codedir))
    assert h.lookup("k") == "fallback"
    assert any(
        "does not contain a valid YAML hash" in r.message for r in caplog.records
    )


def test_v3_backend_major_order(make_tree, monkeypatch):
    root = make_tree(
        ":backends: [json, yaml]\n"
        ":json:\n"
        "  :datadir: data\n"
        ":yaml:\n"
        "  :datadir: data\n"
        ':hierarchy: ["os/%{facts.os.family}", common]\n',
        files={
            "data/common.json": '{"k": "json_common", "u": ["jc"], "h": {"jc": 1}}\n',
            "data/os/RedHat.json": '{"u": ["jo"]}\n',
            "data/os/RedHat.yaml": "k: yaml_os\nu: [yo]\nh: {yo: 1}\n",
            "data/common.yaml": "k: yaml_common\nu: [yc]\nh: {yc: 1}\n",
        },
        raw=True,
    )
    # A relative v3 datadir follows the process cwd, not hiera.yaml's own
    # directory -- unlike a v5 config, which needs no chdir.
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    assert h.lookup("k") == "json_common"
    assert h.lookup("u", merge="unique") == ["jo", "jc", "yo", "yc"]
    assert h.lookup("h", merge="hash") == {"yc": 1, "yo": 1, "jc": 1}
