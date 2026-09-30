"""``hiera.yaml`` version 3 and 4 (Hiera 1/2 are unversioned v3): version
dispatch, Puppet's v3/v4 schema validation, the non-mapping-file fallback,
the deprecation warning, the backend-major (v3) and per-entry (v4) provider
builds, per-layer version rules, and version 3 global-only sub-lookups
(``_hiera_config.py``'s ``_config_version``/``_fill_v3_defaults``/
``_validate_v3``/``_read_v3``/``_v3_level_specs``/``_v3_backend_class``/
``_find_line_matching``/``_default_codedir``/``_fill_v4_defaults``/
``_validate_v4``/``_read_v4``; ``core.py``'s ``_usable``/``_global_only_for``;
``_invocation.Invocation.global_only``).

Ports ``HieraConfig.create``'s dispatch, ``HieraConfigV3``/``HieraConfigV4``'s
``validate_config``/``create_configured_data_providers``, and
``resolve_paths``' extension rule, plus Puppet's mismatch-describer order
(``pops/types/type_mismatch_describer.rb``) and ``invocation.rb``'s
``global_only`` flag.
"""

import copy
import io
import logging
import re

import pytest
from pathlib_next import Path

from hyera import Backend, ConfigError, Hiera, KeyNotFoundError, Scope
from hyera._config.hiera_config import (
    V3_DEFAULT_CONFIG_HASH,
    _ConfigSource,
    _default_codedir,
    _fill_v3_defaults,
    _fill_v4_defaults,
    _find_line_matching,
    _v3_level_specs,
    _validate_v4,
)
from hyera._lookup.invocation import Invocation
from hyera.backends._yaml_loader import RubySymbol


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


def test_v3_extension_rules(make_tree, monkeypatch):
    # No :extension: override -- the default is "." + the backend name.
    root_default = make_tree(
        ":backends: [yaml]\n:yaml:\n  :datadir: data\n:hierarchy: [common]\n",
        files={"data/common.yaml": "k: default_ext\n"},
        raw=True,
        root="default",
    )
    monkeypatch.chdir(root_default)
    assert Hiera(str(root_default / "hiera.yaml")).lookup("k") == "default_ext"

    # :extension: yml overrides it; an entry already ending with it is not
    # doubled, whether declared that way literally or produced by
    # interpolation.
    root = make_tree(
        ":backends: [yaml]\n"
        ":yaml:\n"
        "  :datadir: data\n"
        "  :extension: yml\n"
        ':hierarchy: ["first", "already.yml", "%{::f}"]\n',
        files={
            "data/first.yml": "k: first_yml\n",
            "data/already.yml": "a: not_doubled_static\n",
            "data/dyn.yml": "d: not_doubled_dynamic\n",
        },
        raw=True,
        root="override",
    )
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"f": "dyn.yml"}))
    assert h.lookup("k") == "first_yml"
    assert h.lookup("a") == "not_doubled_static"
    assert h.lookup("d") == "not_doubled_dynamic"


def test_v3_hocon_backend_default_extension_and_dispatch(make_tree, monkeypatch):
    # backends: [hocon] is a data_hash/hocon_data level, and its default
    # extension is ".conf" (distinct from yaml/json/eyaml's "." + name
    # rule, and from the "." + b fallback for a third-party name).
    pytest.importorskip("pyhocon")
    root = make_tree(
        ":backends: [hocon]\n:hocon:\n  :datadir: data\n:hierarchy: [common]\n",
        files={"data/common.conf": "k = v\n"},
        raw=True,
    )
    monkeypatch.chdir(root)
    assert Hiera(str(root / "hiera.yaml")).lookup("k") == "v"


def test_v3_relative_datadir_uses_cwd_at_construction(monkeypatch, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "data").mkdir(parents=True)
    (a / "data" / "common.yaml").write_text("k: from_a\n", encoding="utf-8")
    b.mkdir()
    (a / "hiera.yaml").write_text(
        ":backends: [yaml]\n:yaml:\n  :datadir: data\n:hierarchy: [common]\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(a)
    h = Hiera(str(a / "hiera.yaml"))
    monkeypatch.chdir(b)
    assert h.lookup("k") == "from_a"


def test_v3_default_datadir_uses_codedir_and_environment(make_tree, tmp_path):
    root = make_tree(":backends: [yaml]\n:hierarchy: [common]\n", raw=True)
    codedir = tmp_path / "code"
    hieradata = codedir / "environments" / "staging" / "hieradata"
    hieradata.mkdir(parents=True)
    (hieradata / "common.yaml").write_text("k: staged\n", encoding="utf-8")
    h = Hiera(
        str(root / "hiera.yaml"),
        codedir=str(codedir),
        scope=Scope(environment="staging"),
    )
    assert h.lookup("k") == "staged"


def test_default_codedir():
    import os

    expected = (
        Path(os.environ.get("ALLUSERSPROFILE", r"C:\ProgramData"))
        / "PuppetLabs"
        / "code"
        if os.name == "nt"
        else Path("/etc/puppetlabs/code")
    )
    assert _default_codedir() == expected


def test_v3_merge_behavior_is_not_applied(make_tree, monkeypatch):
    root = make_tree(
        ":backends: [yaml]\n"
        ":yaml:\n"
        "  :datadir: data\n"
        ':hierarchy: ["%{::osfamily}", common]\n'
        ":merge_behavior: deeper\n",
        files={
            "data/common.yaml": "h: {a: 1, b: {c: 1}}\n",
            "data/RedHat.yaml": "h: {b: {d: 2}}\n",
        },
        raw=True,
    )
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"osfamily": "RedHat"}))
    assert h.lookup("h", merge="hash") == {"a": 1, "b": {"d": 2}}


@pytest.mark.parametrize("name", ["foo", "sops"])
def test_v3_unknown_backend_raises(make_tree, name):
    # `sops` gets no v3 name: it is not a Puppet v3 backend, so it fails
    # exactly like any other unregistered name, never the sops_data
    # extension.
    root = make_tree(
        ":backends: [{0}]\n:{0}:\n  :datadir: data\n:hierarchy: [common]\n".format(
            name
        ),
        files={"data/common.{}".format(name): "k: v\n"},
        raw=True,
    )
    with pytest.raises(
        ConfigError, match="Hiera 3 backend '{}' is not available".format(name)
    ):
        Hiera(str(root / "hiera.yaml"))


def test_v3_unknown_backend_via_file_like_source_has_line_but_no_path():
    # _config_error's own "line but no path" branch (distinct from
    # _type_error's, which v5 validation uses instead): a file-like source
    # has readable text (so a line is still found via _find_line_matching)
    # but no filesystem path of its own.
    stream = io.StringIO("backends: unknown_v3_backend\nhierarchy:\n  - common\n")
    with pytest.raises(ConfigError) as exc:
        Hiera(stream)
    assert exc.value.path is None
    assert exc.value.line == 1
    assert str(exc.value).endswith("(line: 1)")


def test_v3_registered_python_backend(make_tree, monkeypatch):
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))

    class _KV3Backend(Backend):
        NAMES = {"v3": ("kv3",)}

        def loads(self, text):
            return {"k": text.strip()}

    root = make_tree(
        ":backends: [kv3]\n:kv3:\n  :datadir: data\n:hierarchy: [common]\n",
        files={"data/common.kv3": "hello\n"},
        raw=True,
    )
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "hello"


def test_v3_registered_backend_without_data_hash_raises(make_tree, monkeypatch):
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))

    class _NoDataHashBackend(Backend):
        NAMES = {"v3": ("nodh",)}

    root = make_tree(
        ":backends: [nodh]\n:nodh:\n  :datadir: data\n:hierarchy: [common]\n",
        files={"data/common.nodh": "x\n"},
        raw=True,
    )
    with pytest.raises(ConfigError, match="must implement data_hash"):
        Hiera(str(root / "hiera.yaml"))


def test_v3_eyaml_maps_to_eyaml_lookup_key():
    data = {
        "backends": ["eyaml"],
        "hierarchy": ["common"],
        "eyaml": {"datadir": "data", "pkcs7_private_key": "keys/private_key.pkcs7.pem"},
    }
    source = _ConfigSource("<test>", None, None, Path("."))
    specs = _v3_level_specs(data, source, Path("/codedir"))
    assert len(specs) == 1
    spec = specs[0]
    assert spec["kind"] == "lookup_key"
    assert spec["function"] == "eyaml_lookup_key"
    assert spec["extension"] == ".eyaml"
    assert spec["options"] == {"pkcs7_private_key": "keys/private_key.pkcs7.pem"}
    assert spec["backend_cls"] is None


def test_hiera3_backend_resolves_registered_v3_backend(make_tree, monkeypatch):
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))

    class _KV3Backend(Backend):
        NAMES = {"v3": ("kv3",)}

        def loads(self, text):
            return {"k": text.strip()}

    root = make_tree(
        {"hierarchy": [{"name": "x", "hiera3_backend": "kv3", "path": "common.kv3"}]},
        files={"data/common.kv3": "appended_once\n"},
    )
    assert Hiera(str(root / "hiera.yaml")).lookup("k") == "appended_once"

    # A declared path without the extension gets it appended once; one
    # already carrying it is never doubled -- both read the same file.
    root2 = make_tree(
        {"hierarchy": [{"name": "x", "hiera3_backend": "kv3", "path": "common"}]},
        files={"data/common.kv3": "appended_once\n"},
        root="second",
    )
    assert Hiera(str(root2 / "hiera.yaml")).lookup("k") == "appended_once"


def test_v3_duplicate_backend_reports_lines(make_tree):
    root = make_tree(
        "---\n:backends:\n  - yaml\n  - yaml\n:yaml:\n  :datadir: data\n"
        ":hierarchy: [common]\n",
        files={"data/common.yaml": "k: v\n"},
        raw=True,
    )
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(root / "hiera.yaml"))
    assert "First defined at (line: 3)" in str(excinfo.value)
    assert excinfo.value.line == 4


def test_find_line_matching():
    text = "first\n" "b # comment cuts here\n" 'c = "keeps # inside quotes"\n' "last\n"
    # a '#' outside quotes starts a comment -- "cuts" is stripped away
    assert _find_line_matching(text, r"cuts") is None
    # a '#' inside a quoted string is not a comment -- "inside" is found
    assert _find_line_matching(text, r"inside") == 3
    # start_line skips earlier lines
    assert _find_line_matching(text, r"^first$") == 1
    assert _find_line_matching(text, r"^first$", start_line=2) is None
    assert _find_line_matching(text, r"^last$", start_line=3) == 4


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


def test_v4_hocon_backend_default_extension_and_dispatch(make_tree, caplog):
    # backend: hocon is a data_hash/hocon_data level, and its default
    # extension is ".conf" (distinct from yaml/json's "." + name rule) --
    # proven by the module-prefix warning naming both 'hocon_data' and the
    # '.conf' path it read (pyhocon itself keeps the surrounding quotes on
    # a quoted "mymod::k"-shaped key literally, so the value can never
    # actually round-trip through the module-prefix check to be looked up).
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
    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyNotFoundError):
            h.lookup("mymod::k")
    assert "hocon_data" in caplog.text
    assert "c.conf" in caplog.text


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


# --- per-layer version rules and global-only sub-lookups ---------------


@pytest.mark.parametrize(
    "place, version, strict, expect",
    [
        ("environments", 3, "warning", "ignored"),
        (
            "environments",
            3,
            "error",
            "hiera.yaml version 3 cannot be used in an environment",
        ),
        ("modules", 3, "warning", "ignored"),
        ("modules", 3, "error", "hiera.yaml version 3 cannot be used in a module"),
        ("environments", 4, "warning", "used"),
        ("modules", 4, "warning", "used"),
    ],
    ids=[
        "environment-v3-warning",
        "environment-v3-error",
        "module-v3-warning",
        "module-v3-error",
        "environment-v4-used",
        "module-v4-used",
    ],
)
def test_layer_version_rules(make_tree, caplog, place, version, strict, expect):
    is_env = place == "environments"
    name_component = "production" if is_env else "mymod"
    key = "k" if is_env else "mymod::k"
    if version == 3:
        layer_text = (
            ":backends: [yaml]\n:yaml:\n  :datadir: data\n:hierarchy: [common]\n"
        )
        data_content = "k: v\n" if is_env else "mymod::k: v\n"
    else:
        layer_text = "version: 4\ndatadir: data\nhierarchy:\n  - name: common\n    backend: yaml\n"
        data_content = "k: v\n" if is_env else "mymod::k: v\n"
    root = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={
            "data/g.yaml": "g: 1\n",
            "{}/{}/hiera.yaml".format(place, name_component): layer_text,
            "{}/{}/data/common.yaml".format(place, name_component): data_content,
        },
    )
    kwargs = (
        {"environmentpath": [root / "environments"]}
        if is_env
        else {"modulepath": [root / "modules"]}
    )
    scope = Scope(strict=strict) if strict != "warning" else None
    h = Hiera(str(root / "hiera.yaml"), scope=scope, **kwargs)
    with caplog.at_level(logging.WARNING):
        if expect.startswith("hiera.yaml"):
            with pytest.raises(ConfigError, match=re.escape(expect)):
                h.lookup(key)
        elif expect == "ignored":
            with pytest.raises(KeyNotFoundError):
                h.lookup(key)
            assert any("was ignored" in r.message for r in caplog.records)
        else:
            assert h.lookup(key) == "v"


def test_hiera3_backend_only_in_global_layer(make_tree):
    root = make_tree(
        {"hierarchy": []},
        files={
            "environments/production/hiera.yaml": (
                "version: 5\nhierarchy:\n  - name: x\n"
                "    hiera3_backend: foo\n    path: common\n"
            ),
        },
    )
    with pytest.raises(
        ConfigError, match="'hiera3_backend' is only allowed in the global layer"
    ) as excinfo:
        Hiera(str(root / "hiera.yaml"), environmentpath=[root / "environments"])
    assert excinfo.value.line == 4


def test_invocation_global_only_is_inherited():
    scope = Scope()
    inv = Invocation(scope, lambda k, i: None, global_only=True)
    derived = inv.derive(lambda k, i: None)
    assert derived.global_only is True


def test_v3_global_sub_lookups_stay_global(make_tree, monkeypatch):
    root = make_tree(
        ":backends: [yaml]\n:yaml:\n  :datadir: data\n:hierarchy: [common]\n",
        files={
            "data/common.yaml": (
                "x: \"a%{lookup('mymod::k')}b\"\n"
                "g: gval\n"
                "gg: \"a%{lookup('g')}b\"\n"
            ),
            "modules/mymod/hiera.yaml": (
                "version: 5\ndefaults: {datadir: data, data_hash: yaml_data}\n"
                "hierarchy:\n  - name: common\n    path: common.yaml\n"
            ),
            "modules/mymod/data/common.yaml": "mymod::k: modval\n",
        },
        raw=True,
    )
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"), modulepath=[root / "modules"])
    # No environment hiera.yaml at all: the global layer's own data stays
    # confined to the global layer, so the qualified sub-lookup misses even
    # though the module itself resolves the key directly.
    assert h.lookup("x") == "ab"
    assert h.lookup("gg") == "agvalb"
    assert h.lookup("mymod::k") == "modval"
