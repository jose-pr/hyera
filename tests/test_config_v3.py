"""``hiera.yaml`` version 3 (and Hiera 1/2, which are unversioned v3):
version dispatch, Puppet's v3 schema validation, the non-mapping-file
fallback, and the deprecation warning (``_hiera_config.py``'s
``_config_version``/``_fill_v3_defaults``/``_validate_v3``/``_read_v3``).

Ports ``HieraConfig.create``'s dispatch and ``HieraConfigV3#validate_config``
plus Puppet's mismatch-describer order (``pops/types/
type_mismatch_describer.rb``).
"""

import logging

import pytest

from hyera import ConfigError, Hiera, Scope
from hyera._hiera_config import V3_DEFAULT_CONFIG_HASH, _fill_v3_defaults
from hyera._yaml_loader import RubySymbol


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
    ],
)
def test_v3_config_schema_errors(config, match):
    with pytest.raises(ConfigError, match=match):
        Hiera(config)


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
def test_non_mapping_file_falls_back_to_v3_default(make_tree, caplog, text):
    root = make_tree(text, raw=True)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(
            ConfigError, match="hiera.yaml version 3 hierarchies are not supported yet"
        ):
            Hiera(str(root / "hiera.yaml"))
    assert any(
        "does not contain a valid YAML hash" in r.message for r in caplog.records
    )
