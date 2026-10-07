"""``hiera.yaml`` version 3 backends: extensions, datadir, registered and unknown
backends, duplicate reports."""

import copy
import io

import pytest
from pathlib_next import Path

from hyera import Backend, ConfigError, Hiera, Scope
from hyera._config.config_source import _ConfigSource
from hyera._config.config_v3 import (
    _default_codedir,
    _find_line_matching,
    _v3_level_specs,
)


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


def test_default_codedir_posix(monkeypatch):
    # The "nt" branch above is the only one a Windows CI run ever takes on
    # its own -- force the non-Windows side deterministically rather than
    # leaving it platform-gated.
    import os

    monkeypatch.setattr(os, "name", "posix")
    assert _default_codedir() == Path("/etc/puppetlabs/code")


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
    # _config_error's "line but no path" branch: a file-like source has readable
    # text, so _find_line_matching finds a line, but no filesystem path.
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


def test_v3_eyaml_backend_options_reach_the_built_level(make_tree, monkeypatch):
    # Unlike test_v3_eyaml_maps_to_eyaml_lookup_key above (spec dict only), this builds
    # a real HieraLevel through _v3_levels: per-backend options (all but `datadir`)
    # must be threaded onto the level's conf.
    pytest.importorskip("cryptography")
    key_path = (
        Path(__file__).resolve().parent
        / "conformance"
        / "cases"
        / "backend-eyaml-pkcs7"
        / "keys"
        / "private_key.pkcs7.pem"
    ).as_posix()
    root = make_tree(
        "backends: [eyaml]\n"
        "hierarchy: [common]\n"
        "eyaml:\n"
        "  datadir: data\n"
        "  pkcs7_private_key: {}\n".format(key_path),
        files={
            "data/common.eyaml": (
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
        raw=True,
    )
    # A v3 level's relative `datadir` follows the process cwd at construction, never
    # the hiera.yaml directory (see HieraLevel's datadir_base): chdir into root first,
    # as the other v3 relative-datadir tests do.
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    assert h.lookup("plain") == "s3cr3t RedHat"


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


def test_v3_duplicate_backend_dict_config_has_no_lines():
    # A dict-configured Hiera (no text to search for a line) hits the same
    # duplicate-backend message with neither a first nor a second line, unlike the
    # file-backed case above.
    with pytest.raises(ConfigError) as excinfo:
        Hiera({"backends": ["yaml", "yaml"], "hierarchy": ["common"]})
    assert "Backend 'yaml' is defined more than once." in str(excinfo.value)
    assert "First defined at" not in str(excinfo.value)
    assert excinfo.value.line is None


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


def test_find_line_matching_single_quote():
    # A '#' inside a *single*-quoted string is just as much not a comment
    # as one inside a double-quoted string (test_find_line_matching above)
    # -- the single-quote toggle has its own branch in the scanner.
    text = "d = 'keeps # inside single quotes'\n"
    assert _find_line_matching(text, r"inside") == 1
