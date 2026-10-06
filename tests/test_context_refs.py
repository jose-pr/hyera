"""Dotted scope references (``%{trusted.certname}``) in paths and values, resolved
as nested key access by the one interpolation engine.
"""

import pytest

from hyera import Hiera, HieraLookupError, Scope

CONFIG = """\
    version: 5
    defaults: {data_hash: yaml_data, datadir: data}
    hierarchy:
      - {name: node, path: "nodes/%{trusted.certname}.yaml"}
      - {name: c, path: common.yaml}
    """

FILES = {
    "data/nodes/web01.example.com.yaml": "role: web\n",
    "data/common.yaml": (
        "role: none\n"
        "os_msg: 'running %{facts.os}'\n"
        "scoped: \"%{scope('facts.os')}\"\n"
        "absent: 'x%{facts.nope}y'\n"
        "deep_ref: 'v%{a.b.c}'\n"
    ),
}


def _scope(**kwargs):
    return Scope(**kwargs)


@pytest.fixture
def tree(make_tree):
    return make_tree(CONFIG, FILES)


def test_dotted_ref_in_path_selects_the_right_file(tree):
    # A dotted reference in a hierarchy path resolves via nested key access,
    # not str.format attribute access.
    h = Hiera(
        str(tree / "hiera.yaml"),
        scope=_scope(
            trusted={"certname": "web01.example.com"},
            facts={"os": "linux"},
            variables={"a": {"b": {"c": 42}}},
        ),
    )
    assert h.lookup("role") == "web"


def test_dotted_ref_in_value_interpolates_nested(tree):
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.lookup("os_msg") == "running linux"


def test_dotted_ref_multiple_levels_deep(tree):
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(variables={"a": {"b": {"c": 42}}}))
    assert h.lookup("deep_ref") == "v42"


def test_scope_function_resolves_dotted_ref(tree):
    # %{scope('facts.os')} must agree with the bare %{facts.os} form.
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.lookup("scoped") == "linux"


def test_absent_nested_ref_in_value_is_empty_string(tree):
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.lookup("absent") == "xy"


def test_absent_nested_ref_in_path_probes_empty_segment(make_tree):
    # No `trusted.certname` at all: the reference resolves to '' (Puppet's
    # own rule), so "nodes/%{trusted.certname}.yaml" probes the literal
    # "nodes/.yaml" -- a real candidate, not a skipped level.
    root = make_tree(
        CONFIG, dict(FILES, **{"data/nodes/.yaml": "role: empty_segment\n"})
    )
    h = Hiera(str(root / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.lookup("role") == "empty_segment"


def test_partial_nested_ref_in_path_probes_empty_segment(make_tree):
    # An explicit `trusted` with no `certname` key: the nested lookup is a
    # genuine (silent) miss, resolving to '' the same way -- still a probe,
    # not a skip.
    root = make_tree(
        CONFIG, dict(FILES, **{"data/nodes/.yaml": "role: empty_segment\n"})
    )
    h = Hiera(str(root / "hiera.yaml"), scope=_scope(trusted={"other": "x"}))
    assert h.lookup("role") == "empty_segment"


def test_scalar_walked_as_container_raises(make_tree):
    # A non-dict variable: walking `.certname` into it is a Puppet type mismatch, not a
    # level skip; construction succeeds and the first lookup raises.
    root = make_tree(
        """\
        version: 5
        defaults: {data_hash: yaml_data, datadir: data}
        hierarchy:
          - {name: node, path: "nodes/%{t.certname}.yaml"}
          - {name: c, path: common.yaml}
        """,
        {"data/common.yaml": "role: none\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"t": "not-a-dict"}))
    with pytest.raises(HieraLookupError, match="Got String"):
        h.lookup("role")


def test_flat_dotted_key_does_not_shadow(tree):
    # Puppet reads %{a.b} as nested key access only; a variable literally named
    # "trusted.certname" is unreachable (names cannot contain '.'), so the node level is
    # skipped.
    h = Hiera(
        str(tree / "hiera.yaml"),
        scope=Scope(variables={"trusted.certname": "web01.example.com"}),
    )
    assert h.lookup("role") == "none"


def test_format_resolves_dotted_refs(tree):
    h = Hiera(
        str(tree / "hiera.yaml"),
        scope=_scope(trusted={"certname": "web01.example.com"}, facts={"os": "linux"}),
    )
    assert h.format("node %{trusted.certname} on %{facts.os}") == (
        "node web01.example.com on linux"
    )


def test_list_index_segment_in_dotted_ref(make_tree):
    # Numeric segments index into lists, per Puppet's Integer-segment rule.
    root = make_tree(
        """\
        version: 5
        defaults: {data_hash: yaml_data, datadir: data}
        hierarchy:
          - {name: first_role, path: "roles/%{roles.0}.yaml"}
          - {name: c, path: common.yaml}
        """,
        {
            "data/roles/web.yaml": "picked: web\n",
            "data/common.yaml": "picked: none\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"roles": ["web", "db"]}))
    assert h.lookup("picked") == "web"


def test_datadir_supports_dotted_refs(make_tree):
    # The data dir itself is formatted against the scope too.
    root = make_tree(
        """\
        version: 5
        defaults: {data_hash: yaml_data, datadir: "data/%{facts.env}"}
        hierarchy:
          - {name: c, path: common.yaml}
        """,
        {"data/prod/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"env": "prod"}))
    assert h.lookup("k") == "v"


def test_mapped_paths_template_supports_dotted_refs(make_tree):
    root = make_tree(
        """\
        version: 5
        defaults: {data_hash: yaml_data, datadir: data}
        hierarchy:
          - name: roles
            mapped_paths: [roles, role, "roles/%{role}-%{facts.env}.yaml"]
        """,
        {"data/roles/web-prod.yaml": "web_setting: true\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(variables={"roles": ["web"]}, facts={"env": "prod"}),
    )
    assert h.lookup("web_setting") is True
