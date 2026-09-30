"""Hierarchy location resolution: the Puppet interpolation engine, `datadir`
strictness, mapped_paths scope semantics, and directory locations.
"""

import os

import pytest

from hyera import BackendError, ConfigError, Hiera, InterpolationError, Scope
from hyera._config.hiera_config import HieraLevel
from hyera._config.location_resolver import _no_lookup, _pathname_plus
from hyera.core import _no_option_lookup

# --- _pathname_plus (Ruby Pathname#+) ---------------------------------


@pytest.mark.parametrize(
    "base, rel, expected",
    [
        ("/r/data", "x.yaml", "/r/data/x.yaml"),
        ("/r/data", "./x.yaml", "/r/data/x.yaml"),
        ("/r/data", "../x.yaml", "/r/x.yaml"),
        ("/r/data", "../../../x.yaml", "/x.yaml"),
        ("/r/data", "/abs/x.yaml", "/abs/x.yaml"),
        ("data", "../../x", "../x"),
        ("/r/data/.", "x", "/r/data/x"),
        ("/r/data", "a/../b.yaml", "/r/data/a/../b.yaml"),
    ],
)
def test_pathname_plus(base, rel, expected):
    assert _pathname_plus(base, rel) == expected


def test_pathname_plus_keeps_platform_anchor():
    anchor = "C:/" if os.name == "nt" else "/"
    assert _pathname_plus(anchor + "r", "../../x") == anchor + "x"


def test_pathname_plus_unc_anchor():
    # A "//server/share/..." UNC path's own two-slash anchor, distinct from
    # a plain single-slash root.
    assert _pathname_plus("//server/share/data", "../x.yaml") == "//server/share/x.yaml"


def test_location_and_option_no_lookup_callables_raise():
    # Both _no_lookup (a hierarchy location's own Invocation) and
    # _no_option_lookup (an entry's `options:` Invocation) are unreachable
    # in practice -- interpolation there always runs with
    # allow_methods=False, which rejects every method call
    # (%{hiera()}/%{lookup()}/%{alias()}) before either callable could ever
    # be reached -- exercised directly.
    with pytest.raises(RuntimeError, match="hierarchy locations never"):
        _no_lookup("k", None)
    with pytest.raises(RuntimeError, match="hierarchy options never"):
        _no_option_lookup("k", None)


# --- HieraLevel.new ------------------------------------------------------


def _level(conf):
    from hyera.backends import Backend

    class _StubBackend(Backend):
        def data_hash(self, path, options):
            return {}

    return HieraLevel.new(
        dict(conf, name=conf.get("name", "lvl"), datadir="data"),
        _StubBackend(),
    )


def test_hiera_level_new_path():
    lvl = _level({"path": "nodes/%{trusted.certname}.yaml"})
    assert lvl.location_key == "path"
    assert lvl.locations == ("nodes/%{trusted.certname}.yaml",)


def test_hiera_level_new_paths():
    lvl = _level({"paths": ["a.yaml", "b.yaml"]})
    assert lvl.location_key == "paths"
    assert lvl.locations == ("a.yaml", "b.yaml")


def test_hiera_level_new_glob():
    lvl = _level({"glob": "*.yaml"})
    assert lvl.location_key == "glob"
    assert lvl.locations == ("*.yaml",)


def test_hiera_level_new_globs():
    lvl = _level({"globs": ["a/*.yaml", "b/*.yaml"]})
    assert lvl.location_key == "globs"
    assert lvl.locations == ("a/*.yaml", "b/*.yaml")


def test_hiera_level_new_mapped_paths():
    lvl = _level({"mapped_paths": ["roles", "role", "roles/%{role}.yaml"]})
    assert lvl.location_key == "mapped_paths"
    assert lvl.locations == ("roles", "role", "roles/%{role}.yaml")


def test_hiera_level_new_uri():
    lvl = _level({"uri": "http://example.com/x"})
    assert lvl.location_key == "uri"
    assert lvl.locations == ("http://example.com/x",)


def test_hiera_level_new_no_location():
    lvl = _level({})
    assert lvl.location_key is None
    assert lvl.locations == ()


def test_hiera_level_paths_resolves_locations(tmp_path):
    # Through the public seam (hyera.HieraLevel, hyera.backends.YAMLBackend),
    # not the private _hiera_config/_StubBackend helper above: .paths()
    # interpolates the location template and joins it onto base_path/datadir.
    from hyera import HieraLevel
    from hyera.backends import YAMLBackend

    level = HieraLevel.new(
        {"name": "lvl", "datadir": "data", "path": "%{environment}.yaml"},
        YAMLBackend(),
    )
    paths = level.paths(tmp_path, Scope(environment="production"))
    assert [str(p) for p in paths] == [str(tmp_path / "data" / "production.yaml")]


def test_hiera_level_paths_resolves_a_glob(tmp_path):
    # .paths() -- unlike the main lookup pipeline (which lazily
    # materializes a glob level, see resolve_glob_specs) -- eagerly
    # expands a glob/globs level through _expand_globs, since it has no
    # lazy materialization step of its own.
    from hyera import HieraLevel
    from hyera.backends import YAMLBackend

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.yaml").write_bytes(b"k: v\n")
    (tmp_path / "data" / "b.yaml").write_bytes(b"k: v2\n")
    # A directory whose own name matches the glob pattern is dropped, not
    # just one that doesn't match at all.
    (tmp_path / "data" / "sub.yaml").mkdir()

    level = HieraLevel.new(
        {"name": "lvl", "datadir": "data", "glob": "*.yaml"}, YAMLBackend()
    )
    paths = level.paths(tmp_path, Scope())
    assert sorted(str(p) for p in paths) == [
        str(tmp_path / "data" / "a.yaml"),
        str(tmp_path / "data" / "b.yaml"),
    ]


# --- path/paths extension (used for Hiera 3 configs) ---------------------


def test_resolve_paths_extension(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "p", "path": "x"}]},
        files={"data/x.yaml": "k: found\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    from hyera._config.location_resolver import _resolve_paths
    from hyera._lookup.invocation import Invocation

    inv = Invocation(Scope(), lambda k, i: None, lenient=True)
    (loc,) = _resolve_paths(str(root / "data"), ["x"], inv, extension=".yaml")
    assert str(loc.location).endswith("x.yaml")
    (loc2,) = _resolve_paths(str(root / "data"), ["x.yaml"], inv, extension=".yaml")
    assert str(loc2.location).endswith("x.yaml")
    assert not str(loc2.location).endswith("x.yaml.yaml")


# --- undefined variable in a location: never skipped, always probed ------


@pytest.mark.parametrize("strict", ["warning", "error", "off"])
def test_undefined_variable_in_location_is_probed(make_tree, caplog, strict):
    root = make_tree(
        {"hierarchy": [{"name": "n", "path": "nodes/%{nosuch}.yaml"}]},
        files={"data/nodes/.yaml": "k: empty\n"},
    )
    with caplog.at_level("WARNING"):
        h = Hiera(str(root / "hiera.yaml"), scope=Scope(strict=strict))
        assert h.lookup("k") == "empty"
    messages = [r.message for r in caplog.records]
    if strict == "warning":
        assert any("Undefined variable 'nosuch'" in m for m in messages)
    elif strict == "error":
        assert any(
            "Interpolation failed with 'nosuch', but compilation continuing" in m
            for m in messages
        )
    else:
        assert not messages


# --- undefined variable in datadir: follows strict ------------------------


def test_undefined_variable_in_datadir_warning_and_off(make_tree):
    for strict in ("warning", "off"):
        root = make_tree(
            {
                "hierarchy": [
                    {"name": "dd", "path": "common.yaml", "datadir": "data/%{nosuch}"}
                ]
            },
            files={"data/common.yaml": "k: common\n"},
        )
        h = Hiera(str(root / "hiera.yaml"), scope=Scope(strict=strict))
        assert h.lookup("k") == "common"


def test_undefined_variable_in_datadir_error_raises(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "dd", "path": "common.yaml", "datadir": "data/%{nosuch}"}
            ]
        },
        files={"data/common.yaml": "k: common\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(strict="error"))
    with pytest.raises(InterpolationError, match="Undefined variable 'nosuch'"):
        h.lookup("k")


def test_nested_miss_in_datadir_under_strict_error(make_tree):
    # A nested (dotted) navigation miss is silent, even under strict=error --
    # only a genuinely undefined *root* variable raises there.
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "dd",
                    "path": "common.yaml",
                    "datadir": "data/%{facts.nosuch}",
                }
            ]
        },
        files={"data/common.yaml": "k: common\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(facts={"os": "linux"}, strict="error"),
    )
    assert h.lookup("k") == "common"


# --- method syntax is rejected in every location context ------------------


@pytest.mark.parametrize(
    "hierarchy",
    [
        [{"name": "p", "path": "%{lookup('x')}.yaml"}],
        [{"name": "p", "paths": ["common.yaml", "%{alias('x')}.yaml"]}],
        [{"name": "g", "glob": "%{hiera('x')}/*.yaml"}],
        [{"name": "m", "mapped_paths": ["roles", "r", "r/%{scope('r')}.yaml"]}],
    ],
)
def test_method_syntax_in_locations_raises(make_tree, hierarchy):
    root = make_tree(
        {"hierarchy": hierarchy},
        files={"data/common.yaml": "k: common\n"},
    )
    # Method syntax is a hiera.yaml problem, but interpolating a location
    # template happens lazily on the first lookup, not in the constructor.
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"roles": ["web"]}))
    with pytest.raises(ConfigError, match="method syntax is not allowed"):
        h.lookup("k")


def test_method_syntax_in_datadir_raises(make_tree):
    root = make_tree(
        {
            "defaults": {"data_hash": "yaml_data"},
            "hierarchy": [
                {"name": "p", "path": "common.yaml", "datadir": "%{literal('data')}"}
            ],
        },
        files={"data/common.yaml": "k: common\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ConfigError, match="method syntax is not allowed"):
        h.lookup("k")


# --- mapped_paths collection semantics ------------------------------------


@pytest.mark.parametrize(
    "value, rendered",
    [
        (True, "Boolean true"),
        (5, "Integer 5"),
        (5.0, "Float 5.0"),
    ],
    ids=["bool", "int", "float"],
)
def test_mapped_collection_items_scalar_type_error_direct(value, rendered):
    # A mapped_paths collection variable resolved to a non-collection
    # scalar: Puppet's own NoMethodError calling .empty? on it, ported as
    # a ConfigError naming the Ruby type. Exercised directly -- a scope
    # variable can hold any of these, but not every one has a recorded
    # Puppet-oracle case.
    from hyera._config.location_resolver import _mapped_collection_items
    from hyera.exceptions import ConfigError

    with pytest.raises(ConfigError) as exc:
        _mapped_collection_items(value, "var", "lvl")
    assert str(exc.value) == (
        "mapped_paths collection 'var' in hierarchy 'lvl' must be a String, "
        "an Array or a Hash, got {}".format(rendered)
    )


def test_mapped_collection_items_other_type_error_direct():
    # The final fallback (neither a Puppet scalar/collection type this
    # project's own scope values can otherwise be): a plain Python
    # type name and str(), matching what any unmodeled object falls back
    # to elsewhere in this codebase too.
    from hyera._config.location_resolver import _mapped_collection_items
    from hyera.exceptions import ConfigError

    marker = object()
    with pytest.raises(ConfigError) as exc:
        _mapped_collection_items(marker, "var", "lvl")
    assert str(exc.value) == (
        "mapped_paths collection 'var' in hierarchy 'lvl' must be a String, "
        "an Array or a Hash, got object {}".format(marker)
    )


def test_mapped_paths_collection_array(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "m", "mapped_paths": ["roles", "role", "roles/%{role}.yaml"]}
            ]
        },
        files={"data/roles/web.yaml": "k: web\n", "data/roles/db.yaml": "k: db\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"roles": ["web", "db"]}))
    assert h.lookup("k") == "web"
    # A plain (non-hierarchy-rebuild) lookup's own Invocation has no
    # scope_interpolations list to track -- with_local_memory_eluding's
    # own no-op path, distinct from a hierarchy build's refs-tracking one.
    # explain() resolves the same mapped_paths locations through
    # without_explain() (locations are never themselves recorded).
    assert "roles/web.yaml" in h.explain("k").text()


def test_mapped_paths_collection_colon_prefix(make_tree):
    # "::roles" must be quoted in the YAML text: unquoted, PyYAML emits a
    # plain scalar that this project's Psych-emulating loader reads as a
    # Ruby Symbol (a real hiera.yaml quotes it the same way).
    root = make_tree(
        """\
        version: 5
        defaults: {datadir: data, data_hash: yaml_data}
        hierarchy:
          - {name: m, mapped_paths: ["::roles", role, "roles/%{role}.yaml"]}
        """,
        files={"data/roles/web.yaml": "k: web\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"roles": ["web"]}))
    assert h.lookup("k") == "web"


def test_mapped_paths_collection_string(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "m", "mapped_paths": ["roles", "role", "roles/%{role}.yaml"]}
            ]
        },
        files={"data/roles/web.yaml": "k: web\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"roles": "web"}))
    assert h.lookup("k") == "web"


def test_mapped_paths_collection_hash(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "m",
                    "mapped_paths": ["roles", "item", "roles/%{item.1}.yaml"],
                },
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/roles/web.yaml": "k: web\n", "data/common.yaml": "k: common\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"), scope=Scope(facts={"roles": {"primary": "web"}})
    )
    assert h.lookup("k") == "web"


@pytest.mark.parametrize("collection", [None, "", []])
def test_mapped_paths_collection_empty_forms_no_paths(make_tree, collection):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "m", "mapped_paths": ["roles", "role", "roles/%{role}.yaml"]},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: common\n"},
    )
    facts = {} if collection is None else {"roles": collection}
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts=facts))
    assert h.lookup("k") == "common"


@pytest.mark.parametrize(
    "collection, match",
    [
        (True, "Boolean true"),
        (False, "Boolean false"),
        (5, "Integer 5"),
        (1.5, "Float"),
    ],
)
def test_mapped_paths_scalar_collection_raises(make_tree, collection, match):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "m", "mapped_paths": ["roles", "role", "roles/%{role}.yaml"]}
            ]
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"roles": collection}))
    with pytest.raises(ConfigError, match=match):
        h.lookup("k")


def test_mapped_item_is_a_local_variable_top_scope_still_reachable(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "m", "mapped_paths": ["roles", "role", "r/%{role}.yaml"]}
            ]
        },
        files={"data/r/toplevel.yaml": "k: toplevel\n", "data/r/web.yaml": "k: web\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(facts={"roles": ["web"], "role": "toplevel"}),
    )
    # Unqualified %{role} reads the mapped item ("web"), shadowing the
    # top-scope fact of the same name.
    assert h.lookup("k") == "web"


def test_mapped_item_explicit_top_scope_bypasses_local_layer(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "m", "mapped_paths": ["roles", "role", "r/%{::role}.yaml"]}
            ]
        },
        files={"data/r/toplevel.yaml": "k: toplevel\n", "data/r/web.yaml": "k: web\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(facts={"roles": ["web"], "role": "toplevel"}),
    )
    # %{::role} is explicitly top-scope: it must reach the fact, not the
    # mapped item variable of the same name.
    assert h.lookup("k") == "toplevel"


# --- a directory location raises, never silently loads its files ---------


def test_path_directory_location_raises(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "d", "path": "confd"}]},
        files={"data/confd/a.yaml": "k: a\n"},
    )
    # A directory location is a data-file problem: it surfaces on the first
    # lookup, not from the constructor.
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="Is a directory"):
        h.lookup("k")


def test_mapped_paths_directory_location_raises(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "m", "mapped_paths": ["roles", "role", "r/%{role}"]}]},
        files={"data/r/web/in.yaml": "k: in\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"roles": ["web"]}))
    with pytest.raises(BackendError, match="Is a directory"):
        h.lookup("k")


def test_glob_over_a_directory_drops_it(make_tree):
    # Unlike path/paths/mapped_paths, a glob match that is a directory is
    # dropped outright rather than raised.
    root = make_tree(
        {"hierarchy": [{"name": "g", "glob": "*"}]},
        files={"data/sub/in.yaml": "k: nope\n", "data/z.yaml": "k: z\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "z"
