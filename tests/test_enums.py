"""hyera's public string enums (``Merge``, ``Strict``, ``FunctionKind``,
``BackendKind``, ``RenderAs``): a member is a plain ``str`` that substitutes
cleanly for the string it equals, on every supported Python, everywhere a
parameter already accepted that string.
"""

import enum

import pytest
import yaml

from hyera import (
    Backend,
    BackendKind,
    FunctionKind,
    HieraLevel,
    InterpolationError,
    Merge,
    RenderAs,
    Scope,
    Strict,
)
from hyera._output.render import JSONRender, StringRender, YAMLRender

_ALL_ENUMS = (Merge, Strict, FunctionKind, BackendKind, RenderAs)


# -- shape: every member is its value, on every supported Python ----------


@pytest.mark.parametrize("enum_cls", _ALL_ENUMS, ids=lambda c: c.__name__)
def test_members_equal_their_strings(enum_cls):
    for member in enum_cls:
        assert isinstance(member, str)
        assert isinstance(member, enum.Enum)
        assert member == member.value
        assert member.value in (member.value,)  # a plain str, hashable the same


@pytest.mark.parametrize("enum_cls", _ALL_ENUMS, ids=lambda c: c.__name__)
def test_str_format_fstring_give_the_value(enum_cls):
    for member in enum_cls:
        assert str(member) == member.value
        assert format(member) == member.value
        assert "{}".format(member) == member.value
        assert f"{member}" == member.value
        assert "{:>10}".format(member) == format(member.value, ">10")


def test_merge_members():
    assert {m.value for m in Merge} == {"first", "unique", "hash", "deep"}


def test_strict_members():
    assert {m.value for m in Strict} == {"off", "warning", "error"}


def test_function_kind_members():
    assert {m.value for m in FunctionKind} == {"data_hash", "lookup_key", "data_dig"}


def test_backend_kind_members():
    assert {m.value for m in BackendKind} == {"function", "v3", "format", "render"}
    assert set(Backend.KINDS) == {m.value for m in BackendKind}


def test_render_as_members():
    assert {m.value for m in RenderAs} == {"s", "json", "yaml"}
    assert set(Backend.names("render")) == {m.value for m in RenderAs}


# -- Merge: a real lookup gives identical results for member and string ---


def test_lookup_merge_member_equals_string(hiera_root):
    from hyera import Hiera

    h = Hiera(hiera_root / "hiera.yaml")
    by_member = h.lookup("db", merge=Merge.DEEP)
    by_string = h.lookup("db", merge="deep")
    assert by_member == by_string == {"host": "db.prod.internal", "port": 5432}


def test_dig_and_get_accept_merge_member(hiera_root):
    from hyera import Hiera

    h = Hiera(hiera_root / "hiera.yaml")
    assert h.dig("db", "port", merge=Merge.DEEP) == h.dig("db", "port", merge="deep")
    assert h.get("db.port", merge=Merge.DEEP) == h.get("db.port", merge="deep")


def test_merge_spec_dict_accepts_member_strategy(hiera_root):
    from hyera import Hiera

    h = Hiera(hiera_root / "hiera.yaml")
    by_member = h.lookup("db", merge={"strategy": Merge.DEEP})
    by_string = h.lookup("db", merge={"strategy": "deep"})
    assert by_member == by_string


# -- Strict: raises the same way for a member as for the equal string -----


def test_strict_member_raises_like_the_string():
    by_member = Scope(strict=Strict.ERROR)
    by_string = Scope(strict="error")
    for scope in (by_member, by_string):
        with pytest.raises(InterpolationError, match="Undefined variable 'nope'"):
            scope.lookupvar("nope")


def test_strict_off_member_is_silent():
    assert Scope(strict=Strict.OFF).lookupvar("nope") is None


def test_scoped_accepts_strict_member(hiera_root):
    from hyera import Hiera

    h = Hiera(hiera_root / "hiera.yaml")
    view = h.scoped(strict=Strict.ERROR)
    assert view.scope.strict == "error"


def test_invalid_strict_message_unchanged():
    with pytest.raises(
        ValueError,
        match=r"strict must be one of \('off', 'warning', 'error'\), not 'bogus'",
    ):
        Scope(strict="bogus")


# -- FunctionKind: HieraLevel.new accepts a member --------------------------


def test_hiera_level_new_accepts_function_kind_member():
    backend = Backend.new("yaml_data")
    level = HieraLevel.new(
        {"name": "x", "datadir": "data"}, backend, kind=FunctionKind.LOOKUP_KEY
    )
    assert level.kind == FunctionKind.LOOKUP_KEY
    assert level.kind == "lookup_key"
    assert type(level.kind) is str


# -- BackendKind: find/get/new/names accept a member -------------------------


def test_backend_find_get_new_names_accept_backend_kind_member():
    assert Backend.find("yaml_data", kind=BackendKind.FUNCTION) is Backend.find(
        "yaml_data", kind="function"
    )
    assert Backend.get("yaml_data", kind=BackendKind.FUNCTION) is Backend.get(
        "yaml_data", kind="function"
    )
    by_member = Backend.new("yaml_data", kind=BackendKind.FUNCTION)
    by_string = Backend.new("yaml_data", kind="function")
    assert type(by_member) is type(by_string)
    assert Backend.names(BackendKind.RENDER) == Backend.names("render")


def test_backend_strict_accepts_strict_member():
    backend = Backend.new("yaml_data", strict=Strict.ERROR)
    assert backend.strict == "error"
    assert type(backend.strict) is str


# -- RenderAs: Backend.new(name, kind="render") accepts a member ----------


def test_render_as_member_selects_the_right_backend():
    assert type(Backend.new(RenderAs.S, kind="render")) is StringRender
    assert type(Backend.new(RenderAs.JSON, kind="render")) is JSONRender
    assert type(Backend.new(RenderAs.YAML, kind="render")) is YAMLRender


# -- outputs stay plain str, never a member --------------------------------


def test_scope_strict_output_is_plain_str():
    assert type(Scope(strict=Strict.WARNING).strict) is str


def test_hiera_level_kind_output_is_plain_str():
    backend = Backend.new("yaml_data")
    level = HieraLevel.new(
        {"name": "x", "datadir": "data"}, backend, kind=FunctionKind.DATA_DIG
    )
    assert type(level.kind) is str


def test_explain_tree_built_with_members_is_yaml_safe(hiera_root):
    from hyera import Hiera

    h = Hiera(hiera_root / "hiera.yaml")
    result = h.explain("db", merge=Merge.DEEP)
    tree = result.to_hash()
    # Must not raise "cannot represent an object" for a leaked enum member.
    dumped = yaml.safe_dump(tree)
    assert isinstance(dumped, str)
