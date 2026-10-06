"""Per-layer ``hiera.yaml`` version rules, global-only sub-lookups and strict-mode path errors."""

import logging
import re

import pytest

from hyera import ConfigError, Hiera, HieraError, KeyNotFoundError, Scope
from hyera._lookup.invocation import Invocation

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


def test_v3_extension_is_stringified_interpolated_and_nil_is_the_default(
    make_tree, monkeypatch
):
    def lookup(extension, files, **scope):
        root = make_tree(
            ":backends: [yaml]\n:yaml:\n  :datadir: data\n"
            "  :extension: {}\n:hierarchy: [common]\n".format(extension),
            files=files,
            raw=True,
            root="ext-{}".format(abs(hash(extension))),
        )
        monkeypatch.chdir(root)
        return Hiera(str(root / "hiera.yaml"), scope=Scope(**scope)).lookup("k")

    assert lookup("5", {"data/common.5": "k: five\n"}) == "five"
    assert lookup("~", {"data/common.yaml": "k: default\n"}) == "default"
    assert (
        lookup("'%{facts.x}'", {"data/common.yml": "k: yml\n"}, facts={"x": "yml"})
        == "yml"
    )


def test_v3_nil_datadir_is_the_default_not_an_error(make_tree, monkeypatch):
    root = make_tree(
        ":backends: [yaml]\n:yaml:\n  :datadir: ~\n:hierarchy: [common]\n",
        raw=True,
    )
    monkeypatch.chdir(root)
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k", default_value="miss") == "miss"


def test_v3_hierarchy_path_with_undefined_variable_fails_under_strict_error(
    make_tree, monkeypatch
):
    root = make_tree(
        ":backends: [yaml]\n:yaml:\n  :datadir: data\n"
        ':hierarchy: ["roles/%{::norole}", common]\n',
        files={"data/common.yaml": "k: common\n"},
        raw=True,
    )
    monkeypatch.chdir(root)
    config = str(root / "hiera.yaml")
    assert Hiera(config, scope=Scope(strict="warning")).lookup("k") == "common"
    with pytest.raises(HieraError, match="Undefined variable '::norole'"):
        Hiera(config, scope=Scope(strict="error")).lookup("k")


def test_v4_hierarchy_path_with_undefined_variable_fails_under_strict_error(
    tmp_path, make_tree
):
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "g: 1\n"},
    )
    env = tmp_path / "envs" / "target"
    (env / "data").mkdir(parents=True)
    (env / "data" / "common.yaml").write_bytes(b"k: common\n")
    (env / "hiera.yaml").write_bytes(
        b"version: 4\ndatadir: data\nhierarchy:\n"
        b"  - {name: c, backend: yaml, path: 'roles/%{::norole}'}\n"
        b"  - {name: d, backend: yaml, path: common}\n"
    )

    def hiera(strict):
        return Hiera(
            str(base / "hiera.yaml"),
            environmentpath=tmp_path / "envs",
            scope=Scope(environment="target", strict=strict),
        )

    assert hiera("warning").lookup("k") == "common"
    with pytest.raises(HieraError, match="Undefined variable '::norole'"):
        hiera("error").lookup("k")
