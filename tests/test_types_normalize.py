"""The mismatch text for NotUndef, Optional and Variant types: Puppet's
TypeAsserter output, recorded from Puppet 8.10.0."""

import pytest

from hyera import HieraLookupError
from hyera._types.mismatch import assert_instance_of
from hyera._types.parser import parse_type

#: (type, value, the text Puppet's TypeAsserter raises; None when the value matches)
RECORDED = [
    (
        "NotUndef[Integer]",
        "None",
        "S has wrong type, expects an Integer value, got Undef",
    ),
    (
        "NotUndef[Integer]",
        '"a"',
        "S has wrong type, expects an Integer value, got String",
    ),
    ("NotUndef[Integer]", "5", None),
    ("NotUndef", "None", "S has wrong type, expects a NotUndef value, got Undef"),
    ("NotUndef", "5", None),
    ("NotUndef[String]", "None", "S has wrong type, expects a String value, got Undef"),
    ("NotUndef['x']", "None", "S has wrong type, expects a String value, got Undef"),
    ("NotUndef['x']", '"y"', "S has wrong type, expects a String value, got String"),
    ("Optional", "5", ""),
    ("Optional", "None", None),
    ("Optional", '"a"', ""),
    (
        "Optional[Integer]",
        '"a"',
        "S has wrong type, expects a value of type Undef or Integer, got String",
    ),
    ("Optional[Integer]", "None", None),
    (
        "Variant[NotUndef[Integer], NotUndef[Float]]",
        "None",
        "S has wrong type, expects a value of type Integer or Float, got Undef",
    ),
    (
        "Variant[NotUndef[Integer], NotUndef[Float]]",
        '"a"',
        "S has wrong type, expects a value of type Integer or Float, got String",
    ),
    (
        "Variant[NotUndef[Integer], Float]",
        '"a"',
        "S has wrong type, expects a value of type Integer or Float, got String",
    ),
    (
        "Variant[NotUndef[Integer], String]",
        "None",
        "S has wrong type, expects a value of type Integer or String, got Undef",
    ),
    (
        "Variant[NotUndef[String], NotUndef[Integer]]",
        "None",
        "S has wrong type, expects a value of type String or Integer, got Undef",
    ),
    (
        "Variant[Optional['a'], Optional['b']]",
        '"c"',
        "S has wrong type, expects an undef value or a match for Enum['a', 'b'], got 'c'",
    ),
    (
        "Variant[Optional['a'], Integer]",
        '"c"',
        "S has wrong type, expects a value of type Undef, String, or Integer, got String",
    ),
    ("Variant[Optional[Integer], String]", "None", None),
    (
        "Variant[Optional[Integer], String]",
        "[]",
        "S has wrong type, expects a value of type Undef, Integer, or String, got Array",
    ),
    (
        "Variant[Optional[Integer], Optional[String]]",
        "[]",
        "S has wrong type, expects a value of type Undef, Integer, or String, got Array",
    ),
    ("Variant[Optional[Integer], Optional[String]]", "None", None),
    ("Variant[Optional, Integer]", "None", None),
    ("Variant[Optional, Integer]", "3", None),
    (
        "Variant[NotUndef, Integer]",
        "None",
        "S has wrong type, expects a value of type NotUndef or Integer, got Undef",
    ),
    ("Variant[NotUndef, Integer]", '"c"', None),
    (
        "Variant[NotUndef[String], Integer]",
        "None",
        "S has wrong type, expects a value of type String or Integer, got Undef",
    ),
    (
        "Variant[Undef, Integer]",
        '"c"',
        "S has wrong type, expects a value of type Undef or Integer, got String",
    ),
    (
        "Variant[Undef, NotUndef[Integer]]",
        '"c"',
        "S has wrong type, expects a value of type Undef or Integer, got String",
    ),
    (
        "Variant[NotUndef[Enum[a,b]], NotUndef[Enum[c]]]",
        '"z"',
        "S has wrong type, expects a match for Enum['a', 'b', 'c'], got 'z'",
    ),
    (
        "Variant[NotUndef['a'], NotUndef['b']]",
        '"z"',
        "S has wrong type, expects a match for Enum['a', 'b'], got 'z'",
    ),
    (
        "Variant[Optional['a'], Optional['b'], Integer]",
        '"c"',
        "S has wrong type, expects a value of type Undef, Integer, or Enum['a', 'b'], got String",
    ),
    (
        "Variant[Optional['a'], Optional['b'], Enum['x']]",
        '"c"',
        "S has wrong type, expects an undef value or a match for Enum['a', 'b', 'x'], got 'c'",
    ),
    (
        "Variant[Optional['a'], Enum['x']]",
        '"c"',
        "S has wrong type, expects an undef value or a match for Enum['a', 'x'], got 'c'",
    ),
    (
        "Variant[NotUndef[Optional[Integer]], String]",
        "[]",
        "S has wrong type, expects a value of type Integer or String, got Array",
    ),
    (
        "Variant[NotUndef[Optional[Integer]], String]",
        "None",
        "S has wrong type, expects a value of type Integer or String, got Undef",
    ),
    (
        "Variant[Optional[NotUndef[Integer]], String]",
        "[]",
        "S has wrong type, expects a value of type Undef, Integer, or String, got Array",
    ),
    (
        "Array[NotUndef[Integer]]",
        "[None]",
        "S has wrong type, index 0 expects an Integer value, got Undef",
    ),
    (
        "Array[NotUndef[Integer]]",
        '["a"]',
        "S has wrong type, index 0 expects an Integer value, got String",
    ),
    (
        "Array[NotUndef]",
        "[None]",
        "S has wrong type, index 0 expects a NotUndef value, got Undef",
    ),
    (
        "Hash[String, NotUndef[Integer]]",
        '{"a": None}',
        "S has wrong type, entry 'a' expects an Integer value, got Undef",
    ),
    ("Hash[String, Optional]", '{"a": 5}', ""),
    (
        "Struct[{a => NotUndef[Integer]}]",
        "{}",
        "S has wrong type, expects size to be 1, got 0",
    ),
    (
        "Struct[{a => NotUndef[Integer]}]",
        '{"a": None}',
        "S has wrong type, entry 'a' expects an Integer value, got Undef",
    ),
    ("Struct[{a => Optional}]", '{"a": 5}', ""),
    ("Struct[{a => Optional}]", '{"a": None}', None),
    (
        "Struct[{a => Optional[Integer]}]",
        '{"a": "x"}',
        "S has wrong type, entry 'a' expects a value of type Undef or Integer, got String",
    ),
    (
        "Struct[{Optional[a] => NotUndef[Integer]}]",
        '{"a": None}',
        "S has wrong type, entry 'a' expects an Integer value, got Undef",
    ),
    (
        "Tuple[NotUndef[Integer]]",
        "[None]",
        "S has wrong type, index 0 expects an Integer value, got Undef",
    ),
    ("Tuple[Optional]", "[5]", ""),
    ("Optional[Optional]", "5", ""),
    (
        "Optional[Optional[Integer]]",
        '"a"',
        "S has wrong type, expects a value of type Undef or Integer, got String",
    ),
    (
        "NotUndef[NotUndef[Integer]]",
        "None",
        "S has wrong type, expects an Integer value, got Undef",
    ),
    (
        "NotUndef[Optional[Integer]]",
        "None",
        "S has wrong type, expects an Integer value, got Undef",
    ),
    (
        "NotUndef[Optional[Integer]]",
        '"a"',
        "S has wrong type, expects an Integer value, got String",
    ),
    (
        "NotUndef[Variant[Integer,String]]",
        "None",
        "S has wrong type, expects a value of type Integer or String, got Undef",
    ),
    (
        "NotUndef[Variant[Integer,String]]",
        "[]",
        "S has wrong type, expects a value of type Integer or String, got Array",
    ),
    (
        "Optional[Variant[Integer,String]]",
        "[]",
        "S has wrong type, expects a value of type Undef, Integer, or String, got Array",
    ),
    (
        "Optional[Enum[a,b]]",
        '"c"',
        "S has wrong type, expects an undef value or a match for Enum['a', 'b'], got 'c'",
    ),
    (
        "Optional[Pattern[/x/]]",
        '"c"',
        "S has wrong type, expects an undef value or a match for Pattern[/x/], got 'c'",
    ),
    (
        "Optional[Struct[{a=>Integer}]]",
        "{}",
        "S has wrong type, expects size to be 1, got 0",
    ),
    (
        "Sensitive[NotUndef[Integer]]",
        "None",
        "S has wrong type, expects a Sensitive[Integer] value, got Undef",
    ),
    ("Optional[NotUndef]", "None", None),
    ("Optional[NotUndef]", "5", None),
    ("NotUndef[Any]", "None", "S has wrong type, expects a NotUndef value, got Undef"),
    (
        "NotUndef[Optional]",
        "None",
        "S has wrong type, expects a NotUndef value, got Undef",
    ),
    ("NotUndef[Optional]", "5", ""),
    ("Optional[Any]", "None", None),
    ("Optional[Any]", "5", None),
]


@pytest.mark.parametrize("text, literal, expected", RECORDED)
def test_the_text_equals_puppets(text, literal, expected):
    value = eval(literal)  # a literal written in this file
    type_ = parse_type(text)
    if expected is None:
        assert assert_instance_of("S", type_, value) == value
        return
    with pytest.raises(HieraLookupError) as info:
        assert_instance_of("S", type_, value)
    assert str(info.value) == expected


def test_a_type_that_rejects_a_value_with_nothing_to_describe_raises_without_text():
    with pytest.raises(HieraLookupError) as info:
        assert_instance_of("S", parse_type("Optional"), 5)
    assert str(info.value) == ""
