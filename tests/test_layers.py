"""Global/environment/module layer discovery and per-layer config rules.

Covers the parts of Puppet's ``lookup_adapter.rb``/``environment_data_
provider.rb``/``module_data_provider.rb`` this adds to the engine: the three
layer-discovery keywords, environment/module directory discovery, per-layer
config versions, and ``hiera3_backend``'s global-only rule. Cross-layer
``lookup_options`` and ``default_hierarchy`` semantics have their own tests,
added alongside those features.
"""

import copy
import logging
import os

import pytest

from hyera import ConfigError, Hiera, HieraLookupError, KeyNotFoundError, Scope


def _global(make_tree):
    """A minimal, valid global config: one level, key ``g`` -> ``1``."""
    return make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "g: 1\n"},
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


_LEVEL = "version: 5\nhierarchy:\n  - {name: c, path: c.yaml}\n"


@pytest.mark.parametrize("kind", ["pathsep-string", "list-of-str", "single-path"])
def test_layer_path_forms(tmp_path, make_tree, kind):
    base = _global(make_tree)
    envs1 = tmp_path / "envs1"
    envs2 = tmp_path / "envs2"
    envs1.mkdir()
    _write(envs2 / "target" / "hiera.yaml", _LEVEL)
    _write(envs2 / "target" / "data" / "c.yaml", "k: v\n")

    if kind == "pathsep-string":
        environmentpath = str(envs1) + os.pathsep + str(envs2)
    elif kind == "list-of-str":
        environmentpath = [str(envs1), str(envs2)]
    else:
        environmentpath = envs2

    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath=environmentpath,
        scope=Scope(environment="target"),
    )
    assert h.lookup("k") == "v"


def test_layer_path_type_error(make_tree):
    base = _global(make_tree)
    with pytest.raises(TypeError):
        Hiera(str(base / "hiera.yaml"), environmentpath=5)


def test_first_environmentpath_entry_wins(tmp_path, make_tree):
    base = _global(make_tree)
    envs1 = tmp_path / "envs1"
    envs2 = tmp_path / "envs2"
    _write(envs1 / "target" / "hiera.yaml", _LEVEL)
    _write(envs1 / "target" / "data" / "c.yaml", "k: first\n")
    _write(envs2 / "target" / "hiera.yaml", _LEVEL)
    _write(envs2 / "target" / "data" / "c.yaml", "k: second\n")

    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath=[envs1, envs2],
        scope=Scope(environment="target"),
    )
    assert h.lookup("k") == "first"


def test_environmentpath_entry_whose_match_is_a_file_is_skipped(tmp_path, make_tree):
    # A directory LISTING is matched (the name appears at all), never
    # (entry / name).is_dir() alone -- a *file* named exactly "target" in
    # the first entry must not stop the search; the second entry's real
    # "target" directory is still found.
    base = _global(make_tree)
    envs1 = tmp_path / "envs1"
    envs2 = tmp_path / "envs2"
    envs1.mkdir(parents=True)
    (envs1 / "target").write_bytes(b"not a directory\n")
    _write(envs2 / "target" / "hiera.yaml", _LEVEL)
    _write(envs2 / "target" / "data" / "c.yaml", "k: second\n")

    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath=[envs1, envs2],
        scope=Scope(environment="target"),
    )
    assert h.lookup("k") == "second"


def test_modulepath_entry_with_a_file_and_a_real_module(tmp_path, make_tree):
    # module_dirs walks every name in one entry's directory listing; a
    # file matching the module-name pattern must not stop it from also
    # finding a real module directory listed alongside it.
    base = _global(make_tree)
    modules = tmp_path / "modules"
    modules.mkdir(parents=True)
    (modules / "moda").write_bytes(b"not a directory\n")
    _write(modules / "modb" / "hiera.yaml", _LEVEL)
    _write(modules / "modb" / "data" / "c.yaml", "modb::k: v\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    assert h.lookup("modb::k") == "v"


def test_missing_production_environment_uses_basemodulepath(tmp_path, make_tree):
    base = _global(make_tree)
    envs = tmp_path / "envs"
    envs.mkdir()  # no "production" directory in it
    modules = tmp_path / "basemods"
    _write(modules / "m" / "hiera.yaml", _LEVEL)
    _write(modules / "m" / "data" / "c.yaml", "m::k: v\n")

    h = Hiera(
        str(base / "hiera.yaml"), environmentpath=[envs], basemodulepath=[modules]
    )
    assert h.lookup("m::k") == "v"


@pytest.mark.parametrize(
    "envname, make_dir",
    [
        ("staging", False),
        ("Bad-Name", True),
    ],
    ids=["absent", "invalid-name"],
)
def test_missing_or_invalid_environment_raises(tmp_path, make_tree, envname, make_dir):
    base = _global(make_tree)
    envs = tmp_path / "envs"
    envs.mkdir()
    if make_dir:
        (envs / envname).mkdir()
    with pytest.raises(ConfigError, match="Could not find a directory environment"):
        Hiera(
            str(base / "hiera.yaml"),
            environmentpath=[envs],
            scope=Scope(environment=envname),
        )


def test_environmentpath_none_accepts_any_environment(make_tree):
    base = _global(make_tree)
    h = Hiera(str(base / "hiera.yaml"), scope=Scope(environment="staging"))
    assert h.lookup("g") == 1


def test_scoped_view_uses_its_environment(tmp_path, make_tree):
    base = _global(make_tree)
    envs = tmp_path / "envs"
    _write(envs / "production" / "hiera.yaml", _LEVEL)
    _write(envs / "production" / "data" / "c.yaml", "env_only: prod\n")
    _write(envs / "staging" / "hiera.yaml", _LEVEL)
    _write(envs / "staging" / "data" / "c.yaml", "env_only: stage\n")

    h = Hiera(str(base / "hiera.yaml"), environmentpath=[envs])
    view = h.scoped(environment="staging")
    assert h.lookup("env_only") == "prod"
    assert view.lookup("env_only") == "stage"


def test_module_directory_name_matched_exactly(tmp_path, make_tree):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(modules / "Mymod" / "hiera.yaml", _LEVEL)
    _write(modules / "Mymod" / "data" / "c.yaml", "mymod::k: v\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    with pytest.raises(KeyNotFoundError):
        h.lookup("mymod::k")


def test_known_module_with_no_data_provider(tmp_path, make_tree):
    # A module directory that exists (known to modulepath) but has no
    # hiera.yaml of its own and no default data file: a known module with
    # no usable provider at all -- report_module_provider_not_found fires
    # (both for the lookup_options gather and the real key), never a
    # crash.
    base = _global(make_tree)
    modules = tmp_path / "modules"
    (modules / "m").mkdir(parents=True)

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    with pytest.raises(KeyNotFoundError):
        h.lookup("m::k")
    assert 'Module data provider for module "m" not found' in h.explain("m::k").text()


def test_unqualified_module_keys_dropped_with_one_warning(tmp_path, make_tree, caplog):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(modules / "m" / "hiera.yaml", _LEVEL)
    _write(modules / "m" / "data" / "c.yaml", "m::k: v\nunq: bad\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    with caplog.at_level(logging.WARNING):
        assert h.lookup("m::k") == "v"
        with pytest.raises(KeyNotFoundError):
            h.lookup("unq")

    messages = [
        r.message for r in caplog.records if "must use keys qualified" in r.message
    ]
    assert len(messages) == 1


def test_module_pruning_does_not_reach_global_layer(tmp_path, make_tree):
    base = make_tree(
        {
            "hierarchy": [
                {"name": "shared", "datadir": "modules/m/data", "path": "c.yaml"}
            ]
        }
    )
    _write(base / "modules" / "m" / "hiera.yaml", _LEVEL)
    _write(base / "modules" / "m" / "data" / "c.yaml", "m::k: v\nunq: shared_value\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[base / "modules"])
    assert h.lookup("m::k") == "v"
    assert h.lookup("unq") == "shared_value"


@pytest.mark.parametrize("place", ["environment", "module"])
@pytest.mark.parametrize("strict", ["warning", "error"])
def test_version_3_layer_config(tmp_path, make_tree, place, strict, caplog):
    base = _global(make_tree)
    v3_text = ":backends: [yaml]\n:hierarchy: [common]\n"
    if place == "environment":
        _write(tmp_path / "envs" / "v3env" / "hiera.yaml", v3_text)
        kwargs = {"environmentpath": [tmp_path / "envs"]}
        scope = Scope(environment="v3env", strict=strict)
        noun = "an environment"
        key = "nope"
    else:
        _write(tmp_path / "modules" / "v3mod" / "hiera.yaml", v3_text)
        kwargs = {"basemodulepath": [tmp_path / "modules"]}
        scope = Scope(strict=strict)
        noun = "a module"
        key = "v3mod::k"

    h = Hiera(str(base / "hiera.yaml"), scope=scope, **kwargs)
    if strict == "error":
        with pytest.raises(ConfigError, match="cannot be used in {}".format(noun)):
            h.lookup(key)
    else:
        with caplog.at_level(logging.WARNING):
            with pytest.raises(KeyNotFoundError):
                h.lookup(key)
        assert any("was ignored" in r.message for r in caplog.records)


def test_empty_environment_config_is_ignored(tmp_path, make_tree, caplog):
    base = _global(make_tree)
    (tmp_path / "envs" / "empty").mkdir(parents=True)
    (tmp_path / "envs" / "empty" / "hiera.yaml").write_bytes(b"")

    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath=[tmp_path / "envs"],
        scope=Scope(environment="empty"),
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyNotFoundError):
            h.lookup("nope")

    messages = [r.message for r in caplog.records]
    assert any("does not contain a valid YAML hash" in m for m in messages)
    assert any("was ignored" in m for m in messages)


@pytest.mark.parametrize("place", ["environment", "module"])
def test_hiera3_backend_only_in_global_layer(tmp_path, make_tree, place):
    base = _global(make_tree)
    text = (
        "version: 5\nhierarchy:\n"
        "  - {name: common, path: common.yaml, hiera3_backend: foo}\n"
    )
    if place == "environment":
        _write(tmp_path / "envs" / "e1" / "hiera.yaml", text)
        with pytest.raises(
            ConfigError, match="'hiera3_backend' is only allowed in the global layer"
        ) as exc_info:
            Hiera(
                str(base / "hiera.yaml"),
                environmentpath=[tmp_path / "envs"],
                scope=Scope(environment="e1"),
            )
    else:
        _write(tmp_path / "modules" / "hb" / "hiera.yaml", text)
        h = Hiera(str(base / "hiera.yaml"), basemodulepath=[tmp_path / "modules"])
        with pytest.raises(
            ConfigError, match="'hiera3_backend' is only allowed in the global layer"
        ) as exc_info:
            h.lookup("hb::k")
    assert exc_info.value.line == 3


def test_version_4_environment_layer_resolves(tmp_path, make_tree):
    base = _global(make_tree)
    _write(
        tmp_path / "envs" / "v4" / "hiera.yaml",
        "version: 4\ndatadir: data\nhierarchy:\n  - name: common\n    backend: yaml\n",
    )
    _write(tmp_path / "envs" / "v4" / "data" / "common.yaml", "envkey: fromv4\n")
    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath=[tmp_path / "envs"],
        scope=Scope(environment="v4"),
    )
    assert h.lookup("envkey") == "fromv4"


def test_modulepath_replaces_default_modulepath(tmp_path, make_tree):
    base = _global(make_tree)
    envs = tmp_path / "envs"
    _write(envs / "production" / "hiera.yaml", _LEVEL)
    _write(envs / "production" / "data" / "c.yaml", "top: e\n")
    _write(envs / "production" / "modules" / "m" / "hiera.yaml", _LEVEL)
    _write(
        envs / "production" / "modules" / "m" / "data" / "c.yaml", "m::k: env-default\n"
    )

    override = tmp_path / "override_modules"
    _write(override / "m" / "hiera.yaml", _LEVEL)
    _write(override / "m" / "data" / "c.yaml", "m::k: override\n")

    h = Hiera(str(base / "hiera.yaml"), environmentpath=[envs], modulepath=[override])
    assert h.lookup("m::k") == "override"


def test_relative_layer_paths_use_cwd(tmp_path, make_tree, monkeypatch):
    base = _global(make_tree)
    _write(tmp_path / "envs" / "target" / "hiera.yaml", _LEVEL)
    _write(tmp_path / "envs" / "target" / "data" / "c.yaml", "k: v\n")

    monkeypatch.chdir(tmp_path)
    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath="envs",
        scope=Scope(environment="target"),
    )
    assert h.lookup("k") == "v"


@pytest.mark.parametrize(
    "bad_option_key, error_match",
    [
        ("other::x", "all lookup_options keys must start with module name 'm'"),
        (
            "^other::.*",
            "all lookup_options patterns must match a key starting with module name 'm'",
        ),
    ],
    ids=["exact-key", "pattern"],
)
def test_module_lookup_options_must_be_qualified(
    tmp_path, make_tree, bad_option_key, error_match
):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(modules / "m" / "hiera.yaml", _LEVEL)
    _write(
        modules / "m" / "data" / "c.yaml",
        "lookup_options:\n  '{}': {{merge: unique}}\nm::k: v\n".format(bad_option_key),
    )

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    # An unqualified key never reaches the module layer, so the global
    # layer still resolves fine despite the module's own bad options.
    assert h.lookup("g") == 1
    with pytest.raises(HieraLookupError, match=error_match):
        h.lookup("m::k")


def test_lookup_options_layer_precedence(tmp_path, make_tree):
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={
            "data/g.yaml": "lookup_options:\n  mymod::a: {merge: unique}\nmymod::a: [g]\n"
        },
    )
    envs = tmp_path / "envs"
    _write(envs / "production" / "hiera.yaml", _LEVEL)
    _write(
        envs / "production" / "data" / "c.yaml",
        "lookup_options:\n"
        "  mymod::a: {merge: first}\n"
        "  mymod::b: {merge: unique}\n"
        "mymod::a: [e]\n"
        "mymod::b: [e]\n"
        "mymod::c: [e]\n",
    )
    _write(envs / "production" / "modules" / "mymod" / "hiera.yaml", _LEVEL)
    _write(
        envs / "production" / "modules" / "mymod" / "data" / "c.yaml",
        "lookup_options:\n"
        "  mymod::a: {merge: first}\n"
        "  mymod::b: {merge: first}\n"
        "  mymod::c: {merge: unique}\n"
        "mymod::a: [m]\n"
        "mymod::b: [m]\n"
        "mymod::c: [m]\n",
    )

    h = Hiera(str(base / "hiera.yaml"), environmentpath=[envs])
    assert h.lookup("mymod::a") == ["g", "e", "m"]
    assert h.lookup("mymod::b") == ["e", "m"]
    assert h.lookup("mymod::c") == ["e", "m"]


def test_environment_lookup_options_apply_to_unqualified_keys(tmp_path, make_tree):
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "k: [g]\n"},
    )
    envs = tmp_path / "envs"
    _write(envs / "production" / "hiera.yaml", _LEVEL)
    _write(
        envs / "production" / "data" / "c.yaml",
        "lookup_options:\n  k: {merge: unique}\nk: [e]\n",
    )

    h = Hiera(str(base / "hiera.yaml"), environmentpath=[envs])
    assert h.lookup("k") == ["g", "e"]


def test_environment_lookup_options_discarded_by_explicit_module_null(
    tmp_path, make_tree
):
    # The environment declares lookup_options for a module-qualified key;
    # the module's own data has an EXPLICIT `lookup_options: ~` (distinct
    # from no lookup_options key at all, which leaves the environment's
    # own options untouched -- see test_lookup_options_layer_precedence's
    # "nothing at all" case) -- Puppet's own if/elsif with no else
    # discards the environment's options outright rather than keeping
    # them, so the lookup falls back to the default first-match merge
    # instead of the environment's "unique".
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "mymod::a: [g]\n"},
    )
    envs = tmp_path / "envs"
    _write(envs / "production" / "hiera.yaml", _LEVEL)
    _write(
        envs / "production" / "data" / "c.yaml",
        "lookup_options:\n  mymod::a: {merge: unique}\nmymod::a: [e]\n",
    )
    _write(envs / "production" / "modules" / "mymod" / "hiera.yaml", _LEVEL)
    _write(
        envs / "production" / "modules" / "mymod" / "data" / "c.yaml",
        "lookup_options: ~\nmymod::a: [m]\n",
    )

    h = Hiera(str(base / "hiera.yaml"), environmentpath=[envs])
    assert h.lookup("mymod::a") == ["g"]


def test_default_hierarchy_rejected_outside_module_layer_dict():
    with pytest.raises(ConfigError) as exc_info:
        Hiera(
            {
                "version": 5,
                "hierarchy": [{"name": "c", "path": "c.yaml"}],
                "default_hierarchy": [{"name": "d", "path": "d.yaml"}],
            }
        )
    assert (
        str(exc_info.value) == "'default_hierarchy' is only allowed in the module layer"
    )
    assert exc_info.value.path is None
    assert exc_info.value.line is None


def test_default_hierarchy_rejected_outside_module_layer_file(make_tree):
    base = make_tree(
        {
            "hierarchy": [{"name": "c", "path": "c.yaml"}],
            "default_hierarchy": [{"name": "d", "path": "d.yaml"}],
        }
    )
    text = (base / "hiera.yaml").read_text(encoding="utf-8")
    expected_line = next(
        i + 1
        for i, line in enumerate(text.splitlines())
        if line.startswith("default_hierarchy:")
    )
    with pytest.raises(
        ConfigError, match="'default_hierarchy' is only allowed in the module layer"
    ) as exc_info:
        Hiera(str(base / "hiera.yaml"))
    assert exc_info.value.line == expected_line


def test_default_hierarchy_rejected_outside_module_layer_environment(
    tmp_path, make_tree
):
    base = _global(make_tree)
    _write(
        tmp_path / "envs" / "e1" / "hiera.yaml",
        "version: 5\nhierarchy:\n  - {name: c, path: c.yaml}\n"
        "default_hierarchy:\n  - {name: d, path: d.yaml}\n",
    )
    with pytest.raises(
        ConfigError, match="'default_hierarchy' is only allowed in the module layer"
    ) as exc_info:
        Hiera(
            str(base / "hiera.yaml"),
            environmentpath=[tmp_path / "envs"],
            scope=Scope(environment="e1"),
        )
    assert exc_info.value.line == 4


def test_module_default_hierarchy_ignores_caller_merge(tmp_path, make_tree):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(
        modules / "m" / "hiera.yaml",
        "version: 5\nhierarchy:\n  - {name: c, path: c.yaml}\n"
        "default_hierarchy:\n  - {name: d1, path: d1.yaml}\n  - {name: d2, path: d2.yaml}\n",
    )
    _write(modules / "m" / "data" / "c.yaml", "m::other: x\n")
    _write(modules / "m" / "data" / "d1.yaml", "m::k: {a: 1}\n")
    _write(modules / "m" / "data" / "d2.yaml", "m::k: {b: 2}\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    # The caller's merge="deep" never reaches the default hierarchy walk;
    # with no lookup_options of its own, it defaults to first-match, so
    # only d1's (higher-priority) value is returned.
    assert h.lookup("m::k", merge="deep") == {"a": 1}
    # A dotted sub-key digs into the same default-hierarchy result.
    assert h.lookup("m::k.a") == 1


def test_default_hierarchy_miss_is_not_found(tmp_path, make_tree):
    # Neither the module's main hierarchy nor its default_hierarchy has
    # the key at all -- a miss reported from the default_hierarchy walk
    # itself, not from the main one.
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(
        modules / "m" / "hiera.yaml",
        "version: 5\nhierarchy:\n  - {name: c, path: c.yaml}\n"
        "default_hierarchy:\n  - {name: d, path: d.yaml}\n",
    )
    _write(modules / "m" / "data" / "c.yaml", "m::other: x\n")
    _write(modules / "m" / "data" / "d.yaml", "m::also_other: y\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    with pytest.raises(KeyNotFoundError):
        h.lookup("m::k")


def test_default_hierarchy_lookup_options_must_be_qualified(tmp_path, make_tree):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(
        modules / "m" / "hiera.yaml",
        "version: 5\nhierarchy:\n  - {name: c, path: c.yaml}\n"
        "default_hierarchy:\n  - {name: d, path: d.yaml}\n",
    )
    _write(modules / "m" / "data" / "c.yaml", "m::main: v\n")
    _write(
        modules / "m" / "data" / "d.yaml",
        "lookup_options:\n  other::x: {merge: unique}\nm::only_default: v\n",
    )

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    # A key found in the main hierarchy never reaches the default
    # hierarchy's own (unqualified, invalid) lookup_options at all.
    assert h.lookup("m::main") == "v"
    with pytest.raises(HieraLookupError):
        h.lookup("m::only_default")


def test_default_hierarchy_only_for_qualified_keys(tmp_path, make_tree):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(
        modules / "m" / "hiera.yaml",
        "version: 5\nhierarchy:\n  - {name: c, path: c.yaml}\n"
        "default_hierarchy:\n  - {name: d, path: d.yaml}\n",
    )
    _write(modules / "m" / "data" / "c.yaml", "m::k: v\n")
    _write(modules / "m" / "data" / "d.yaml", "unq: only-in-default\n")

    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    with pytest.raises(KeyNotFoundError):
        h.lookup("unq")


def _module_tree(tmp_path, make_tree, files):
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(modules / "m" / "hiera.yaml", _LEVEL)
    for rel, text in files.items():
        _write(modules / "m" / "data" / rel, text)
    return base, modules


@pytest.mark.parametrize("revalidate", [True, False])
def test_module_data_edit_is_seen_when_revalidating(tmp_path, make_tree, revalidate):
    base, modules = _module_tree(tmp_path, make_tree, {"c.yaml": "m::k: v1\n"})
    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules], revalidate=revalidate)
    assert h.lookup("m::k") == "v1"

    _write(modules / "m" / "data" / "c.yaml", "m::k: v2-longer\nm::new: added\n")
    if revalidate:
        assert h.lookup("m::k") == "v2-longer"
        assert h.lookup("m::new") == "added"
        assert h.scoped(variables={"x": 1}).lookup("m::k") == "v2-longer"
    else:
        assert h.lookup("m::k") == "v1"
        h.clear_cache()
        assert h.lookup("m::k") == "v2-longer"


def test_module_data_pruning_is_redone_after_the_file_changes(tmp_path, make_tree):
    base, modules = _module_tree(
        tmp_path, make_tree, {"c.yaml": "m::k: v1\nother: x\n"}
    )
    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    assert h.lookup("m::k") == "v1"
    with pytest.raises(KeyNotFoundError):
        h.lookup("other")

    _write(modules / "m" / "data" / "c.yaml", "m::k: v1\nm::other: now-qualified\n")
    assert h.lookup("m::other") == "now-qualified"


def test_two_locationless_module_levels_serve_their_own_data(
    tmp_path, make_tree, monkeypatch
):
    from hyera.backends import Backend, default_backends

    # Subclassing registers the backend process-wide: keep it to this test.
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))

    class FnA(Backend):
        NAMES = {"function": ("layers_fn_a",)}

        def data_hash(self, path, options):
            return {"m::a": "from-A"}

    class FnB(Backend):
        NAMES = {"function": ("layers_fn_b",)}

        def data_hash(self, path, options):
            return {"m::b": "from-B"}

    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(
        modules / "m" / "hiera.yaml",
        "version: 5\nhierarchy:\n"
        "  - {name: A, data_hash: layers_fn_a}\n"
        "  - {name: B, data_hash: layers_fn_b}\n",
    )
    h = Hiera(
        str(base / "hiera.yaml"),
        backends=list(default_backends()) + [FnA, FnB],
        basemodulepath=[modules],
    )
    assert h.lookup("m::a") == "from-A"
    assert h.lookup("m::b") == "from-B"


def test_modulepath_rejects_a_non_path_entry(make_tree):
    base = _global(make_tree)
    with pytest.raises(TypeError, match="modulepath must be a path"):
        Hiera(str(base / "hiera.yaml"), modulepath=[5])


def test_nonexistent_modulepath_and_environmentpath_entries_are_skipped(
    tmp_path, make_tree
):
    # find_environment/module_dirs's own os.listdir OSError catch: an
    # entry that plain doesn't exist at all (not merely empty, the case
    # test_missing_production_environment_uses_basemodulepath already
    # covers) is silently skipped, not a crash.
    base = _global(make_tree)
    modules = tmp_path / "modules"
    _write(modules / "m" / "hiera.yaml", _LEVEL)
    _write(modules / "m" / "data" / "c.yaml", "m::k: v\n")

    h = Hiera(
        str(base / "hiera.yaml"),
        environmentpath=[tmp_path / "no-such-envs"],
        basemodulepath=[tmp_path / "no-such-mods", modules],
    )
    assert h.lookup("m::k") == "v"
