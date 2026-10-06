"""``hiera.yaml`` reading (``HieraConfig.create``): base config copy and version
dispatch, the `defaults`/`hierarchy`/`datadir` fallbacks, version 5 validation.
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

    # The cwd is unrelated to the config: `base_path` is absolute from construction, so
    # the lookup below must not derive anything from the cwd.
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
                        # A dict config skips the YAML loader, so any Python object
                        # can reach here, not only the types _is_data names.
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
    # Any exception from _build_hierarchies other than a HieraError (which keeps its
    # class/text) is wrapped in a ConfigError naming its class and message.
    from hyera._config import data_provider

    def boom(*a, **k):
        raise ValueError("boom")

    monkeypatch.setattr(data_provider, "_build_hierarchies", boom)
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
    # A file-like source has readable text, so a line is found, but no filesystem path:
    # _config_error's "line but no path" suffix differs from the two cases above.
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
    # Puppet's `YAML.safe_load` reads only the first document, so this builds like a
    # single-document file; _config_line's plain `yaml.compose` raises ComposerError on
    # the stream, so an error still raises, without a line number.
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
    # `hierarchy: false` fills in Puppet's default hierarchy, which has no YAML node.
    # The kind-mismatch check is lazy (raised on invocation for an existing location),
    # so this needs a data file and a lookup; no line is looked up, so it is None.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: eyaml_lookup_key}\n"
        b"hierarchy: false\n"
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_bytes(b"k: v\n")

    h = Hiera(str(config))
    with pytest.raises(ConfigError) as exc:
        h.lookup("k")

    assert "'eyaml_lookup_key' expects 3 arguments, got 2" in str(exc.value)
    assert exc.value.line is None


def test_inherited_function_kind_mismatch_has_no_line(tmp_path):
    # `hierarchy` is written out and its entry inherits data_hash from `defaults`. The
    # kind-mismatch check is lazy, so this needs a data file and a lookup; no line is
    # looked up.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults: {datadir: data, data_hash: eyaml_lookup_key}\n"
        b"hierarchy:\n"
        b"  - {name: common, path: common.yaml}\n"
    )
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_bytes(b"k: v\n")

    h = Hiera(str(config))
    with pytest.raises(ConfigError) as exc:
        h.lookup("k")

    assert "'eyaml_lookup_key' expects 3 arguments, got 2" in str(exc.value)
    assert exc.value.line is None


def test_duplicate_top_level_hierarchy_key_last_one_wins(tmp_path):
    # Psych and our loader keep the last duplicate top-level key, so `hierarchy` is the
    # two-entry one; _config_line's plain `yaml.compose` walk finds the first (one
    # entry), so index 1 is out of range.
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
    # Same last-wins rule for a duplicate key within one entry: the surviving `options`
    # is the dict (the bad pattern key is reported), but the raw node walk picks the
    # first `options`, a scalar with no mapping to search.
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


def test_hiera3_backend_in_defaults_is_rejected():
    # hiera3_backend is valid only per hierarchy entry
    # (test_hiera3_backend_replaced_by_data_hash above); Puppet 8.10's `defaults` struct
    # type excludes it (conformance case config-defaults-hiera3-backend-key).
    cfg = {
        "version": 5,
        "defaults": {"datadir": "data", "hiera3_backend": "foo"},
        "hierarchy": [{"name": "common", "path": "common.yaml"}],
    }

    with pytest.raises(ConfigError, match="unrecognized key 'hiera3_backend'"):
        Hiera(cfg)


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
            {"hiera3_backend": "foo"},
            "Hiera 3 backend 'foo' is not available",
        ),
        ({"v4_data_hash": "x"}, "Unable to find 'v4_data_hash' function named 'x'"),
    ],
    ids=["hiera3-backend-unmapped", "v4-data-hash"],
)
def test_function_kind_errors(entry, expected):
    # Name-resolution errors: these stay eager, at config build time (Puppet
    # cannot even resolve what function to call).
    cfg = {
        "version": 5,
        "defaults": {"datadir": "data"},
        "hierarchy": [{"name": "one", "path": "one.yaml", **entry}],
    }

    with pytest.raises(ConfigError) as exc:
        Hiera(cfg)

    assert expected in str(exc.value)


def test_lookup_key_registered_as_data_hash_builds_and_only_errors_on_lookup(
    make_tree,
):
    # A kind mismatch (a data_hash-only function named as data_hash, which Puppet's
    # data_hash_function_provider.rb resolves and later arity-rejects) is raised only
    # when invoked for an existing location, so a missing location must not refuse it.
    cfg = {
        "defaults": {"datadir": "data"},
        "hierarchy": [
            {"name": "one", "path": "missing.yaml", "lookup_key": "yaml_data"},
            {"name": "two", "path": "two.yaml"},
        ],
    }
    root = make_tree(cfg, files={"data/two.yaml": "k: v\n"})

    h = Hiera(str(root / "hiera.yaml"))
    # The first level's location does not exist: Puppet degrades gracefully
    # to the next level, never invoking (or kind-checking) the mismatched
    # function at all.
    assert h.lookup("k") == "v"

    root2 = make_tree(
        {
            "defaults": {"datadir": "data"},
            "hierarchy": [
                {"name": "one", "path": "one.yaml", "lookup_key": "yaml_data"}
            ],
        },
        files={"data/one.yaml": "k: v\n"},
        root="exists",
    )
    h2 = Hiera(str(root2 / "hiera.yaml"))
    with pytest.raises(ConfigError, match="'yaml_data' expects 2 arguments, got 3"):
        h2.lookup("k")


# --- function names are resolved when called ------------------------------


@pytest.mark.parametrize("name", ["YAML_DATA", "Yaml_Data", '"::yaml_data"'])
def test_function_name_ignores_case_and_leading_colons(make_tree, name):
    root = make_tree(
        "version: 5\ndefaults: {datadir: data}\nhierarchy:\n"
        "  - {name: c, data_hash: %s, path: common.yaml}\n" % name,
        files={"data/common.yaml": "k: v\n"},
    )
    assert Hiera(str(root / "hiera.yaml")).lookup("k") == "v"


@pytest.mark.parametrize("key", ["data_hash", "lookup_key", "data_dig"])
def test_unknown_function_fails_only_for_an_existing_location(make_tree, key):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "first", "path": "first.yaml"},
                {"name": "gone", key: "nope_fn", "path": "absent.yaml"},
                {"name": "no-match", key: "nope_fn", "glob": "nothing-*.yaml"},
                {"name": "bad", key: "nope_fn", "path": "second.yaml"},
            ]
        },
        files={"data/first.yaml": "k: first\n", "data/second.yaml": "k: second\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ConfigError, match="Unable to find '%s' function" % key):
        h.lookup("k")


@pytest.mark.parametrize("key", ["data_hash", "lookup_key", "data_dig"])
def test_unknown_function_with_no_existing_location_lets_others_answer(make_tree, key):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "first", "path": "first.yaml"},
                {"name": "gone", key: "nope_fn", "path": "absent.yaml"},
                {"name": "no-match", key: "nope_fn", "glob": "nothing-*.yaml"},
            ]
        },
        files={"data/first.yaml": "k: first\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "first"
    assert h.lookup("other", default_value="miss") == "miss"


@pytest.mark.parametrize("where", ["entry", "defaults"])
def test_empty_datadir_is_rejected(make_tree, where):
    if where == "entry":
        config = {"hierarchy": [{"name": "c", "path": "common.yaml", "datadir": ""}]}
    else:
        config = {
            "defaults": {"datadir": ""},
            "hierarchy": [{"name": "c", "path": "common.yaml"}],
        }
    root = make_tree(config, files={"data/common.yaml": "k: v\n"})
    with pytest.raises(ConfigError, match=r"expects a String\[1\] value"):
        Hiera(str(root / "hiera.yaml"))


def test_interpolated_absolute_datadir_is_joined_under_the_config_root(make_tree):
    root = make_tree(
        {
            "defaults": {"datadir": "%{facts.dd}"},
            "hierarchy": [{"name": "c", "path": "a.yaml"}],
        },
        files={"srv/data/a.yaml": "k: under-root\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"dd": "/srv/data"}))
    assert h.lookup("k") == "under-root"
