"""Regression tests for interpolation edge cases that the old code got wrong."""

import pytest

from hiera import Hiera, InterpolationError


def _hiera(tmp_path, common):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text(common, encoding="utf-8")
    (tmp_path / "hiera.yaml").write_text(
        "defaults:\n  data_hash: yaml_data\n  data_dir: data\n"
        "hierarchy:\n  - name: c\n    path: common.yaml\n",
        encoding="utf-8",
    )
    return Hiera(str(tmp_path / "hiera.yaml"))


def test_value_with_backslash_is_literal(tmp_path):
    # Regression: re.sub treated the resolved value as a replacement template,
    # so a backslash (e.g. a Windows path) raised or was mangled. Use a YAML
    # double-quoted scalar so the stored value has exactly ONE backslash per
    # separator: C:\Users\me
    h = _hiera(
        tmp_path,
        'winpath: "C:\\\\Users\\\\me"\n'
        "ref: \"%{hiera('winpath')}\"\n",
    )
    stored = h.get("winpath")
    assert stored == "C:\\Users\\me"  # sanity: one backslash each
    assert h.get("ref") == stored     # interpolation preserves it verbatim


def test_scope_value_with_group_ref_is_literal(tmp_path):
    # A context value containing "\g<0>" must not be treated as a group ref.
    h = _hiera(tmp_path, "msg: \"got %{token}\"\n")
    assert h.get("msg", token=r"\g<0>") == r"got \g<0>"


def test_missing_scope_interpolates_empty(tmp_path):
    h = _hiera(tmp_path, "msg: \"[%{absent}]\"\n")
    assert h.get("msg") == "[]"


def test_alias_missing_key_raises(tmp_path):
    h = _hiera(tmp_path, "ref: \"%{alias('does::not::exist')}\"\n")
    with pytest.raises(InterpolationError):
        h.get("ref", throw=True)


def test_format_uses_context(tmp_path):
    # Regression: Hiera.format passed the dict positionally instead of **ctx.
    h = _hiera(tmp_path, "x: 1\n")
    assert h.format("hi %{name}", name="bob") == "hi bob"
