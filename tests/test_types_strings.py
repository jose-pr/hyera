"""String formatting and quoting of type and value text."""

import pytest

from hyera import HieraLookupError, Sensitive
from hyera._types.new_function import new_instance
from hyera._types.string_converter import convert as _string_convert
from hyera._types.string_converter import puppet_quote as _puppet_quote
from hyera._types.parser import parse_type


def test_ruby_format_table():
    assert _string_convert(8, "%#o") == "010"
    # Plain "%o" (no "#" alternate-form flag) -- distinct from the "%#o"
    # case above, which always takes the "0" prefix branch.
    assert _string_convert(8, "%o") == "10"
    assert _string_convert(5, "%b") == "101"
    assert _string_convert(255, "%x") == "ff"
    assert _string_convert(1e20, "%p") == "1.0e+20"
    # A mantissa that already has a decimal point in exponent form (unlike
    # 1e20 above, whose repr() mantissa is bare "1" and needs ".0" added).
    assert _string_convert(1.5e20, "%p") == "1.5e+20"
    assert _string_convert(3.0, "%f") == "3.000000"
    assert _string_convert(2.5, "%s") == "2.5"
    # "#" alternate-form prefix on hex/binary (octal's own "#" is covered
    # above already).
    assert _string_convert(255, "%#x") == "0xff"
    assert _string_convert(5, "%#B") == "0B101"
    # An integer value with a float-style directive redirects through the
    # float body, not the integer one.
    assert _string_convert(3, "%f") == "3.000000"
    # Float body: uppercase exponent + width/precision, "+" on a
    # non-negative value, left-justify, and the hex-float form.
    assert _string_convert(3.14159, "%10.2E") == "  3.14E+00"
    assert _string_convert(3.14159, "%+.2f") == "+3.14"
    assert _string_convert(3.14159, "%-10.2f") + "|" == "3.14      |"
    assert _string_convert(3.14159, "%A") == "0X1.921F9F01B866EP+1"
    # NaN/Infinity (Ruby Float#inspect, not Python's repr spelling).
    assert _string_convert(float("nan"), "%p") == "NaN"
    assert _string_convert(float("inf"), "%p") == "Infinity"
    assert _string_convert(float("-inf"), "%p") == "-Infinity"
    # A string_formats value that is not one directive is refused.
    for bad in ("%", "nope", "%d items"):
        with pytest.raises(HieraLookupError, match="is not a valid format"):
            _string_convert(5, bad)
    # Sensitive redacts through every directive, as Ruby's inspect does.
    assert _string_convert(Sensitive("secret"), "%p") == "#<Sensitive [value redacted]>"
    assert _string_convert(Sensitive("secret"), "%s") == "Sensitive [value redacted]"
    # "%c": the integer's own character (Kernel#format's char directive).
    assert _string_convert(65, "%c") == "A"
    # Integer body width/padding: zero-padded, negative zero-padded (sign
    # then digits), left-justified, and plain space-padded.
    assert _string_convert(5, "%05d") == "00005"
    assert _string_convert(-5, "%05d") == "-0005"
    assert _string_convert(5, "%-5d") + "|" == "5    |"
    assert _string_convert(5, "%5x") == "    5"
    # "%g": Ruby's general float format. Python's repr already normalizes the exponent
    # sign, so _ruby_float_inspect's "prepend a sign" branch is effectively unreachable.
    assert _string_convert(3.14159, "%g") == "3.14159"
    assert _string_convert(1e10, "%g") == "1e+10"
    # The final, otherwise-unmodeled-type fallback: a RubySymbol (or any
    # other object with no Ruby equivalent this subset renders specially)
    # falls back to plain str().
    from hyera.backends import RubySymbol

    assert _string_convert(RubySymbol("x")) == ":x"


def test_puppet_quote():
    # A control character not in the named-escape table renders as a
    # \u{XX} escape, and forces the double-quoted form.
    assert _puppet_quote("a\x01b") == '"a\\u{1}b"'
    assert _puppet_quote("x", enforce_double_quotes=True) == '"x"'
    # Single-quoted form: an embedded "'" and a literal "\" both escape.
    assert _puppet_quote("it's") == "'it\\'s'"
    assert _puppet_quote("a\\b") == "'a\\b'"
    # A trailing, unpaired backslash still closes the quote correctly.
    assert _puppet_quote("trail\\") == "'trail\\'"


# (value, format, Puppet 8.10's ``String.new`` output).
STRING_FORMAT_ROWS = [
    (0, "%+d", "+0"),
    (1, "%5d", "    1"),
    (1, "% d", " 1"),
    (1, "%10s", "         1"),
    (1, "%#s", '"1"'),
    (1, "%.3d", "001"),
    (-1, "%05d", "-0001"),
    (-1, "%+d", "-1"),
    (-1, "%x", "..f"),
    (-1, "%o", "..7"),
    (-1, "%b", "..1"),
    (-1, "%#b", "0b..1"),
    (255, "%x", "ff"),
    (255, "%#x", "0xff"),
    (255, "%o", "377"),
    (255, "%#o", "0377"),
    (255, "%b", "11111111"),
    (255, "%.3d", "255"),
    (255, "%.10x", "00000000ff"),
    (-255, "%x", "..f01"),
    (-255, "%X", "..F01"),
    (-255, "%#x", "0x..f01"),
    (-255, "%+x", "-ff"),
    (65, "%c", "A"),
    (65, "%5c", "    A"),
    (1.0, "%A", "0X1P+0"),
    (-1.5, "%d", "-1"),
    (-1.5, "%e", "-1.500000e+00"),
    (-1.5, "%E", "-1.500000E+00"),
    (-1.5, "%f", "-1.500000"),
    (-1.5, "%10.3f", "    -1.500"),
    (-1.5, "%+.1f", "-1.5"),
    (-1.5, "%010.2f", "-000001.50"),
    (3.14159, "%.2f", "3.14"),
    (1e20, "%e", "1.000000e+20"),
    (1e20, "%g", "1e+20"),
    (1e20, "%s", "1.0e+20"),
    (1e-05, "%G", "1E-05"),
    (1e16, "%s", "1.0e+16"),
    (1000000000000000.0, "%s", "1.0e+15"),
    (1000000000000000.0, "%p", "1.0e+15"),
    (100.0, "%g", "100"),
    (100.0, "%#g", "100.000"),
    ("abc", "%10s", "       abc"),
    ("abc", "%.2s", "ab"),
    ("abc", "%u", "ABC"),
    ("abc", "%C", "Abc"),
    ("a'b", "%p", "'a\\'b'"),
    ("a\nb", "%p", '"a\\nb"'),
    ("Abc", "%d", "abc"),
    (True, "%d", "1"),
    (True, "%T", "True"),
    (True, "%y", "yes"),
    (True, "%#y", "y"),
    (False, "%t", "false"),
    (False, "%Y", "No"),
    (None, "%d", "NaN"),
    (None, "%s", ""),
    (None, "%p", "undef"),
    ([1, "a", None, True, 2.5], "%p", "[1, 'a', undef, true, 2.5]"),
    ({"a": 1}, "%a", "[['a', 1]]"),
    ({"a": 1}, "%p", "{'a' => 1}"),
    ({"a": [1, {"b": None}]}, "%s", "{'a' => [1, {'b' => undef}]}"),
    (5, "%d\n", "5\n"),
]


@pytest.mark.parametrize("value, fmt, expected", STRING_FORMAT_ROWS)
def test_string_format_matches_puppet(value, fmt, expected):
    assert _string_convert(value, fmt) == expected


@pytest.mark.parametrize(
    "value, fmt, message",
    [
        (5, "%z", "Illegal format 'z' specified for value of Integer type"),
        (1.5, "%z", "Illegal format 'z' specified for value of Float type"),
        ("a", "%z", "Illegal format 'z' specified for value of String type"),
        ([1], "%d", "Illegal format 'd' specified for value of Array type"),
        ({}, "%d", "Illegal format 'd' specified for value of Hash type"),
        (5, "%d items", "The format '%d items' is not a valid format on the form"),
        (5, "abc", "The format 'abc' is not a valid format on the form"),
        (5, "%dd", "is not a valid format"),
        (5, "%-5d|", "is not a valid format"),
        (5, "%--5d", "The same flag can only be used once, got '%--5d'"),
        (65, "%.3c", None),
        (-1, "%c", "pack(U): value out of range"),
    ],
)
def test_string_format_refuses_what_puppet_refuses(value, fmt, message):
    if message is None:
        assert _string_convert(value, fmt) == "A"
        return
    with pytest.raises(HieraLookupError) as info:
        _string_convert(value, fmt)
    assert message in str(info.value)


def test_string_format_outside_the_subset_is_refused_not_ignored():
    with pytest.raises(HieraLookupError, match="indenting"):
        _string_convert([1], "%#a")
    with pytest.raises(HieraLookupError, match="precision"):
        _string_convert(1.5, "%.2a")
    with pytest.raises(HieraLookupError, match="parameter 'string_formats'"):
        _string_convert(5, {"Integer": "%x"})


def test_string_of_a_hash_uses_puppets_own_separator():
    assert _string_convert({"a": 1, "b": "c"}) == "{'a' => 1, 'b' => 'c'}"


def test_string_new_arity_and_argument_types():
    with pytest.raises(
        HieraLookupError, match="expects between 1 and 2 arguments, got 3"
    ):
        new_instance(parse_type("String"), "x", "%s", "%s")
    with pytest.raises(HieraLookupError, match="parameter 'string_formats' expects"):
        new_instance(parse_type("String"), 5, None)


def test_quotes_are_rendered_by_one_function():
    assert str(parse_type("Enum['a\\b']")) == "Enum['a\\b']"
    assert str(parse_type('Enum["a\\tb"]')) == 'Enum["a\\tb"]'
    assert str(parse_type("Struct[{'a\\b' => Integer}]")) == (
        "Struct[{'a\\b' => Integer}]"
    )


def test_ruby_regex_rejects_a_trailing_backslash():
    from hyera._types.ruby_regexp import _ruby_regex

    with pytest.raises(HieraLookupError) as info:
        _ruby_regex("a\\")
    assert str(info.value) == "too short escape sequence: /a\\/"
