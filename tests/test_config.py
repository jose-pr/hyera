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
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {
                        "name": "one",
                        "path": "one.yaml",
                        "data_hash": "yaml_data",
                        "options": "not-a-hash",
                    }
                ],
            },
            "entry 'options' expects a Hash value, got String",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {
                        "name": "one",
                        "path": "one.yaml",
                        "data_hash": "yaml_data",
                        "options": {"bad key!": 1},
                    }
                ],
            },
            "key of entry 'bad key!' expects a match for Pattern",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {
                        "name": "one",
                        "path": "one.yaml",
                        "data_hash": "yaml_data",
                        "options": {"ok": {1: "x"}},
                    }
                ],
            },
            "entry 'ok' expects a Data value, got Hash",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {"name": "", "path": "one.yaml", "data_hash": "yaml_data"}
                ],
            },
            "entry 'name' expects a String[1] value, got String",
        ),
        (
            {
                **_BASE_V5,
                "hierarchy": [
                    {
                        "name": "one",
                        "path": "one.yaml",
                        "data_hash": "yaml_data",
                        # A dict config is never parsed through the YAML
                        # loader, so an arbitrary Python object can reach
                        # here directly -- outside the small set of types
                        # _ruby_type_name/_is_data name explicitly.
                        "options": {"ok": object()},
                    }
                ],
            },
            "entry 'ok' expects a Data value, got object",
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
        "options-not-a-hash",
        "options-bad-key",
        "options-value-not-data",
        "empty-name",
        "options-value-unrepresentable-type",
    ],
)
def test_malformed_config_raises_config_error(cfg, expected):
    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert expected in str(exc.value)


def test_unexpected_exception_during_build_wrapped_as_config_error(monkeypatch):
    # A defensive catch-all: any exception _build_hierarchies raises other
    # than a HieraError (which keeps its own class/text) is wrapped into a
    # ConfigError naming the real exception's class and message, rather
    # than escaping raw.
    import hyera.core as core

    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(core, "_build_hierarchies", boom)
    with pytest.raises(ConfigError, match="is invalid: ValueError: boom"):
        Hiera(_BASE_V5)


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


def test_duplicate_names_dict_config_has_no_first_line():
    # Same duplicate-name message as above, but a dict-configured Hiera has
    # no text to search for either entry's line.
    cfg = {
        **_BASE_V5,
        "hierarchy": [
            {"name": "same", "path": "a.yaml"},
            {"name": "same", "path": "b.yaml"},
        ],
    }

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert "Hierarchy name 'same' defined more than once." in str(exc.value)
    assert "First defined at" not in str(exc.value)
    assert exc.value.line is None


def test_multi_document_config_error_has_no_line(tmp_path):
    # Puppet's own `safe_load` (`YAML.safe_load`, ported here as
    # backends._yaml_loader.safe_load) reads only the first YAML document
    # in a file and ignores whatever a later `---` document contains, so
    # this configuration parses and builds just like a single-document one
    # would. But the line-lookup helper (_config_line) re-parses the raw
    # text with plain `yaml.compose`, which raises ComposerError on a
    # multi-document stream -- so an error here still raises correctly,
    # just without a line number.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: yaml_data}\n"
        b"hierarchy:\n"
        b"  - {name: common, pathz: common.yaml}\n"
        b"---\n"
        b"[unterminated\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert "unrecognized key 'pathz'" in str(exc.value)
    assert exc.value.line is None
    assert "(line:" not in str(exc.value)


def test_hierarchy_false_kind_mismatch_has_no_line(tmp_path):
    # `hierarchy: false` fills in Puppet's own built-in default hierarchy
    # (_fill_v5_defaults' ||= rule), which has no corresponding node in the
    # actual YAML text at all -- a later error naming a position inside
    # that synthesized entry (here, defaults' own data_hash naming a
    # function that doesn't implement data_hash) can't find a line for it.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: eyaml_lookup_key}\n"
        b"hierarchy: false\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert "'eyaml_lookup_key' expects 3 arguments, got 2" in str(exc.value)
    assert exc.value.line is None


def test_inherited_function_kind_mismatch_has_no_line(tmp_path):
    # Unlike the synthesized-default case above, `hierarchy` is written out
    # here and the one entry is real -- it simply inherits its data_hash
    # from `defaults` (ordinary, encouraged Puppet usage), so the entry's
    # own YAML mapping has no "data_hash" key for _config_line to find when
    # that inherited function turns out to be the wrong kind.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: eyaml_lookup_key}\n"
        b"hierarchy:\n"
        b"  - {name: common, path: common.yaml}\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert "'eyaml_lookup_key' expects 3 arguments, got 2" in str(exc.value)
    assert exc.value.line is None


def test_duplicate_top_level_hierarchy_key_last_one_wins(tmp_path):
    # Psych/our own loader keep the *last* of two duplicate top-level
    # mapping keys (`_flatten_mapping_keeping_dupes_last`), so `hierarchy`
    # here is the second (two-entry) definition. But _config_line's own
    # raw-node walk (over plain `yaml.compose`, which does not dedupe
    # duplicate keys) finds the *first* matching "hierarchy" node instead
    # -- a one-entry sequence -- so looking up index 1 inside it is an
    # out-of-range index into a real, but wrong, sequence node.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: yaml_data}\n"
        b"hierarchy:\n"
        b"  - {name: a, path: a.yaml}\n"
        b"hierarchy:\n"
        b"  - {name: common, path: common.yaml}\n"
        b"  - {name: extra, pathz: extra.yaml}\n"
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert "index 1 unrecognized key 'pathz'" in str(exc.value)
    assert exc.value.line is None


def test_duplicate_options_key_in_one_entry_last_one_wins(tmp_path):
    # Same last-wins rule as above, but for a duplicate key *within* one
    # entry's own mapping: the surviving `options` value is the dict (the
    # bad pattern key is correctly found and reported), but the raw node
    # walk picks the *first* "options" occurrence -- the discarded, non-dict
    # scalar -- so there is no mapping there to look the bad key up in.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: yaml_data}\n"
        b"hierarchy:\n"
        b"  - name: common\n"
        b"    path: common.yaml\n"
        b"    options: not-a-dict\n"
        b"    options:\n"
        b'      "bad key!": 1\n'
    )

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert "key of entry 'bad key!' expects a match for Pattern" in str(exc.value)
    assert exc.value.line is None


def test_invalid_utf8_config_raises(tmp_path):
    config = tmp_path / "hiera.yaml"
    config.write_bytes(b"version: 5\n# \xff\xfe invalid utf-8\n")

    with pytest.raises(ConfigError) as exc:
        Hiera(str(config))

    assert str(exc.value.path).endswith("hiera.yaml")


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
