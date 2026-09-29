"""Interpolation edge cases: literal backslashes, regex-special values, format()."""

from pyera import Hiera


def _hiera(make_tree, common):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": common},
    )
    return Hiera(str(root / "hiera.yaml"))


def test_value_with_backslash_is_literal(make_tree):
    # An interpolated value is inserted literally, never treated as a re.sub
    # replacement template, so a backslash (e.g. a Windows path) is preserved
    # as-is. Use a YAML double-quoted scalar so the stored value has exactly
    # ONE backslash per separator: C:\data\sub
    h = _hiera(
        make_tree,
        'winpath: "C:\\\\data\\\\sub"\n' "ref: \"%{hiera('winpath')}\"\n",
    )
    stored = h.get("winpath")
    assert stored == "C:\\data\\sub"  # sanity: one backslash each
    assert h.get("ref") == stored  # interpolation preserves it verbatim


def test_scope_value_with_group_ref_is_literal(make_tree):
    # A context value containing "\g<0>" must not be treated as a group ref.
    h = _hiera(make_tree, 'msg: "got %{token}"\n')
    assert h.get("msg", token=r"\g<0>") == r"got \g<0>"


def test_missing_scope_interpolates_empty(make_tree):
    h = _hiera(make_tree, 'msg: "[%{absent}]"\n')
    assert h.get("msg") == "[]"


def test_format_uses_context(make_tree):
    # Hiera.format accepts context variables as **kwargs, not just a dict.
    h = _hiera(make_tree, "x: 1\n")
    assert h.format("hi %{name}", name="bob") == "hi bob"
