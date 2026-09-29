"""Dotted scope references (``%{trusted.certname}``) in paths and values.

Hiera 5 defines a dotted reference as nested key access. ``str.format``
reads ``{a.b}`` as *attribute* access, so a dotted reference is resolved via
``_scope_ref``/``_ContextFormatter`` instead of plain ``str.format``/
``format_map`` -- otherwise it would raise ``AttributeError`` out of
``HieraLevel.paths()``, including for the README's own lead example config.
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
    assert h.get("role") == "web"


def test_dotted_ref_in_value_interpolates_nested(tree):
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.get("os_msg") == "running linux"


def test_dotted_ref_multiple_levels_deep(tree):
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(variables={"a": {"b": {"c": 42}}}))
    assert h.get("deep_ref") == "v42"


def test_scope_function_resolves_dotted_ref(tree):
    # %{scope('facts.os')} must agree with the bare %{facts.os} form.
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.get("scoped") == "linux"


def test_absent_nested_ref_in_value_is_empty_string(tree):
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.get("absent") == "xy"


def test_absent_nested_ref_in_path_skips_level(tree):
    # No `trusted` fact/variable at all: the node level is skipped, not an
    # error (Scope's own default $trusted has a nil certname, which is the
    # same "skip" outcome).
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(facts={"os": "linux"}))
    assert h.get("role") == "none"


def test_partial_nested_ref_in_path_skips_level(tree):
    # An explicit `trusted` with no `certname` key -- still a skip, not a
    # crash.
    h = Hiera(str(tree / "hiera.yaml"), scope=_scope(trusted={"other": "x"}))
    assert h.get("role") == "none"


def test_scalar_walked_as_container_raises(make_tree):
    # A non-dict variable: walking `.certname` into it is a Puppet type
    # mismatch, not a silent level skip. Construction itself still succeeds
    # (the constructor only pre-warms the cache; a lookup-time error there
    # is swallowed and deferred to the first real lookup, which raises it).
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
        h.get("role")


def test_flat_dotted_key_does_not_shadow(tree):
    # Puppet reads %{a.b} as nested key access only -- a variable literally
    # named "trusted.certname" is as unreachable here as it is in Puppet (a
    # variable name cannot contain '.'), so the node level is skipped
    # rather than matched by the flat key.
    h = Hiera(
        str(tree / "hiera.yaml"),
        scope=Scope(variables={"trusted.certname": "web01.example.com"}),
    )
    assert h.get("role") == "none"


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
    assert h.get("picked") == "web"


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
    assert h.get("k") == "v"


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
    assert h.get("web_setting") is True
