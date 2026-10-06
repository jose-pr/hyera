"""``convert_result`` and ``Sensitive``."""

import pytest

from hyera import HieraLookupError, Sensitive
from hyera._lookup.lookup_adapter import convert_result
from hyera._types.parser import parse_type

# ------------------------------------------------------ convert_result


def test_convert_result_messages():
    with pytest.raises(HieraLookupError) as exc_info:
        convert_result("k", "NoSuch[", "x")
    assert str(exc_info.value) == (
        "Invalid data type in lookup_options for key 'k' could not parse "
        "'NoSuch[', error: 'Syntax error at end of input"
    )
    assert exc_info.value.__cause__ is not None

    with pytest.raises(HieraLookupError) as exc_info:
        convert_result("k", "Boolean", "maybe")
    assert str(exc_info.value) == (
        "The convert_to lookup_option for key 'k' raised error: "
        "'new_boolean' The string 'maybe' cannot be converted to Boolean"
    )
    assert exc_info.value.__cause__ is not None

    assert convert_result("k", None, "5") == "5"

    with pytest.raises(HieraLookupError) as exc_info:
        convert_result("k", "SemVer", "1.2.3")
    assert str(exc_info.value) == (
        "The convert_to lookup_option for key 'k' raised error: hyera does "
        "not support new() for the Puppet type 'SemVer'"
    )
    assert exc_info.value.__cause__ is not None


# ------------------------------------------------------------ Sensitive


def test_sensitive_puppet_semantics():
    assert str(Sensitive("a")) == "Sensitive [value redacted]"
    assert repr(Sensitive("a")) == "Sensitive [value redacted]"

    assert Sensitive("a") == Sensitive("a")
    assert Sensitive(1) != Sensitive(1.0)
    assert Sensitive(1) != Sensitive(True)
    assert Sensitive([1, {"a": 2}]) == Sensitive([1, {"a": 2}])
    assert len({Sensitive([1]), Sensitive([1])}) == 1
    assert Sensitive("a") != "a"
    assert Sensitive("a").unwrap() == "a"


def test_convert_result_wraps_every_error_of_a_conversion():
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Numeric", "08")
    assert str(info.value).startswith(
        "The convert_to lookup_option for key 'k' raised error: "
    )
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Integer", float("inf"))
    assert "raised error" in str(info.value)
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Hash", [[[1, 2], 3]])
    assert "raised error: unusable Hash key" in str(info.value)


def test_convert_result_wraps_an_error_that_is_not_a_hiera_error(monkeypatch):
    def broken(*args):
        raise RuntimeError("boom")

    monkeypatch.setattr("hyera._lookup.lookup_adapter.new_instance", broken)
    with pytest.raises(HieraLookupError) as info:
        convert_result("k", "Integer", "5")
    assert str(info.value) == (
        "The convert_to lookup_option for key 'k' raised error: boom"
    )
    assert isinstance(info.value.__cause__, RuntimeError)


def test_a_bad_pattern_in_value_type_is_a_lookup_error():
    with pytest.raises(HieraLookupError, match="unmatched parenthesis"):
        parse_type("Pattern[/(/]")
