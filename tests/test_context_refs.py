"""Dotted context references (``%{trusted.certname}``) in paths and values.

Hiera 5 defines a dotted reference as nested key access. ``str.format``
reads ``{a.b}`` as *attribute* access, so these used to raise
``AttributeError`` out of ``HieraLevel.paths()`` -- which crashed the
README's own lead example config.
"""

import textwrap

import pytest

from hiera import Hiera


def build(tmp_path, config, files):
    (tmp_path / "hiera.yaml").write_text(textwrap.dedent(config), encoding="utf-8")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
    return tmp_path


CONFIG = """\
    version: 5
    defaults: {data_hash: yaml_data, data_dir: data}
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

NESTED = {
    "trusted": {"certname": "web01.example.com"},
    "facts": {"os": "linux"},
    "a": {"b": {"c": 42}},
}


@pytest.fixture
def tree(tmp_path):
    return build(tmp_path, CONFIG, FILES)


def test_dotted_ref_in_path_selects_the_right_file(tree):
    # Regression: this raised AttributeError before reaching any lookup.
    h = Hiera(str(tree / "hiera.yaml"), context=NESTED)
    assert h.get("role") == "web"


def test_dotted_ref_in_value_interpolates_nested(tree):
    h = Hiera(str(tree / "hiera.yaml"), context=NESTED)
    assert h.get("os_msg") == "running linux"


def test_dotted_ref_multiple_levels_deep(tree):
    h = Hiera(str(tree / "hiera.yaml"), context=NESTED)
    assert h.get("deep_ref") == "v42"


def test_scope_function_resolves_dotted_ref(tree):
    # %{scope('facts.os')} must agree with the bare %{facts.os} form.
    h = Hiera(str(tree / "hiera.yaml"), context=NESTED)
    assert h.get("scoped") == "linux"


def test_absent_nested_ref_in_value_is_empty_string(tree):
    h = Hiera(str(tree / "hiera.yaml"), context=NESTED)
    assert h.get("absent") == "xy"


def test_absent_nested_ref_in_path_skips_level(tree):
    # No `trusted` at all: the node level is skipped, not an error.
    h = Hiera(str(tree / "hiera.yaml"), context={"facts": {"os": "linux"}})
    assert h.get("role") == "none"


def test_partial_nested_ref_in_path_skips_level(tree):
    # `trusted` exists but has no `certname` -- still a skip, not a crash.
    h = Hiera(str(tree / "hiera.yaml"), context={"trusted": {"other": "x"}})
    assert h.get("role") == "none"


def test_scalar_walked_as_container_skips_level(tree):
    # `trusted` is a string, so `.certname` cannot resolve.
    h = Hiera(str(tree / "hiera.yaml"), context={"trusted": "not-a-dict"})
    assert h.get("role") == "none"


def test_flat_dotted_key_takes_precedence(tree):
    # A context key that literally contains dots keeps working.
    h = Hiera(
        str(tree / "hiera.yaml"),
        context={"trusted.certname": "web01.example.com"},
    )
    assert h.get("role") == "web"


def test_format_resolves_dotted_refs(tree):
    h = Hiera(str(tree / "hiera.yaml"), context=NESTED)
    assert h.format("node %{trusted.certname} on %{facts.os}") == (
        "node web01.example.com on linux"
    )


def test_list_index_segment_in_dotted_ref(tmp_path):
    # Numeric segments index into lists, matching LookupDict.lookup.
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy:
          - {name: first_role, path: "roles/%{roles.0}.yaml"}
          - {name: c, path: common.yaml}
        """,
        {
            "data/roles/web.yaml": "picked: web\n",
            "data/common.yaml": "picked: none\n",
        },
    )
    h = Hiera(str(tmp_path / "hiera.yaml"), context={"roles": ["web", "db"]})
    assert h.get("picked") == "web"


def test_datadir_supports_dotted_refs(tmp_path):
    # The data_dir itself is formatted against the context too.
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: "data/%{facts.env}"}
        hierarchy:
          - {name: c, path: common.yaml}
        """,
        {"data/prod/common.yaml": "k: v\n"},
    )
    h = Hiera(str(tmp_path / "hiera.yaml"), context={"facts": {"env": "prod"}})
    assert h.get("k") == "v"


def test_mapped_paths_template_supports_dotted_refs(tmp_path):
    build(
        tmp_path,
        """\
        version: 5
        defaults: {data_hash: yaml_data, data_dir: data}
        hierarchy:
          - name: roles
            mapped_paths: [roles, role, "roles/%{role}-%{facts.env}.yaml"]
        """,
        {"data/roles/web-prod.yaml": "web_setting: true\n"},
    )
    h = Hiera(
        str(tmp_path / "hiera.yaml"),
        context={"roles": ["web"], "facts": {"env": "prod"}},
    )
    assert h.get("web_setting") is True
