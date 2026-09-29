"""Interpolation edge cases: literal backslashes, regex-special values, format()."""

from hyera import Hiera, Scope


def _hiera(make_tree, common, **variables):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": common},
    )
    scope = Scope(variables=variables) if variables else None
    return Hiera(str(root / "hiera.yaml"), scope=scope)


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
    # A scope value containing "\g<0>" must not be treated as a group ref.
    h = _hiera(make_tree, 'msg: "got %{token}"\n', token=r"\g<0>")
    assert h.get("msg") == r"got \g<0>"


def test_missing_scope_interpolates_empty(make_tree):
    h = _hiera(make_tree, 'msg: "[%{absent}]"\n')
    assert h.get("msg") == "[]"


def test_format_uses_bound_scope(make_tree):
    # Hiera.format resolves against the instance's bound scope.
    # "name" is now a reserved, always-"main" top-scope variable, so this
    # uses an ordinary variable name instead.
    h = _hiera(make_tree, "x: 1\n", who="bob")
    assert h.format("hi %{who}") == "hi bob"
