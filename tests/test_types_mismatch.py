"""Type mismatch messages and instance assertion."""

import pytest
import yaml

from hyera import HieraLookupError
from hyera._types.mismatch import (
    _a_an,
    _size_text,
    assert_instance_of,
)
from hyera._types.parser import parse_type
from types_support import (  # noqa: F401
    _CASES,
    _data,
    _golden,
)

# ------------------------------------------------------- mismatch describer

#: convert-value-type queries whose golden status is a parse error, not an
#: instance mismatch -- already proved by test_parse_matches_golden.
_PARSE_ERROR_TNN = {"t62", "t63", "t69"}


def _assert_matches_params():
    case = yaml.safe_load(
        (_CASES / "convert-value-type" / "case.yaml").read_text(encoding="utf-8")
    )
    golden = _golden("convert-value-type")["results"]
    data = _data("convert-value-type")
    for q in case["queries"]:
        qid = q["id"]
        if qid in _PARSE_ERROR_TNN:
            continue
        yield pytest.param(qid, q["type"], data[q["key"]], golden[qid], id=qid)


@pytest.mark.parametrize("qid,type_text,value,result", list(_assert_matches_params()))
def test_assert_matches_golden(qid, type_text, value, result):
    t = parse_type(type_text)
    if result["status"] == "found":
        assert assert_instance_of("Found value", t, value) is value
        return
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", t, value)
    want = [line.strip() for line in result["message"].splitlines()]
    assert [line.strip() for line in str(exc_info.value).splitlines()] == want


def test_describe_evidence():
    # lookup('hint', Data) with hint: {1: a} -> "Found value has wrong type,
    # expects a Data value, got Hash".
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", parse_type("Data"), {1: "a"})
    assert (
        str(exc_info.value)
        == "Found value has wrong type, expects a Data value, got Hash"
    )

    # lookup('hsi', Variant[Hash[String, String], Array]) -> first line
    # " Found value has wrong type, variant 0 entry 'a' expects a String
    # value, got Integer".
    hsi = {"a": 1, "b": 2}
    t = parse_type("Variant[Hash[String, String], Array]")
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", t, hsi)
    assert str(exc_info.value).splitlines()[0] == (
        "Found value has wrong type, variant 0 entry 'a' expects a String value, got Integer"
    )

    # lookup('big', Variant[Integer[0,10], Integer[20,30]]).
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Variant[Integer[0,10], Integer[20,30]]"), 11
        )
    assert str(exc_info.value) == (
        "Found value has wrong type, expects a value of type Integer[0, 10] or "
        "Integer[20, 30], got Integer[11, 11]"
    )

    # lookup('s', Variant[Enum['a'], Pattern[/^x/]]).
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Variant[Enum['a'], Pattern[/^x/]]"), "str"
        )
    assert str(exc_info.value) == (
        "Found value has wrong type, expects a match for "
        "Variant[Enum['a'], Pattern[/^x/]], got 'str'"
    )

    # Optional[Integer[0]] on -3.
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", parse_type("Optional[Integer[0]]"), -3)
    assert str(exc_info.value) == (
        "Found value has wrong type, expects a value of type Undef or "
        "Integer[0], got Integer[-3, -3]"
    )

    # Hash[String, Integer, 1, 2] on {a: "x"}.
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Hash[String, Integer, 1, 2]"), {"a": "x"}
        )
    assert str(exc_info.value) == (
        "Found value has wrong type, entry 'a' expects an Integer value, got String"
    )

    # Tuple[String, String, String] on [1, "x"] -> size mismatch.
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Tuple[String, String, String]"), [1, "x"]
        )
    assert (
        str(exc_info.value) == "Found value has wrong type, expects size to be 3, got 2"
    )

    # Hash[String, String] on {a: 1, b: 2} -> two lines, joined with "\n ".
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of(
            "Found value", parse_type("Hash[String, String]"), {"a": 1, "b": 2}
        )
    lines = str(exc_info.value).splitlines()
    assert (
        lines[0]
        == "Found value has wrong type, entry 'a' expects a String value, got Integer"
    )
    assert (
        lines[1]
        == " Found value has wrong type, entry 'b' expects a String value, got Integer"
    )


def test_describe_evidence_more_branches():
    def err(t, v):
        with pytest.raises(HieraLookupError) as exc_info:
            assert_instance_of("Found value", t, v)
        return str(exc_info.value)

    # A Variant branch that is itself an Optional[<bareword literal>]:
    # short_name's wrapper-type case, with a raw-str `.contained` (never a
    # real Any -- `_bare_name`'s own str branch).
    assert err(parse_type("Variant[Optional[integer], Boolean]"), 5) == (
        "Found value has wrong type, expects a value of type Undef, String, "
        "or Boolean, got Integer"
    )

    # NotUndef on Undef, both bare and with a contained type.
    assert err(parse_type("NotUndef[Integer]"), None) == (
        "Found value has wrong type, expects an Integer value, got Undef"
    )
    assert err(parse_type("NotUndef"), None) == (
        "Found value has wrong type, expects a NotUndef value, got Undef"
    )
    # A bare NotUndef (no contained type) never rejects a non-Undef value,
    # whatever it is; a bare Optional accepts only undef, and its refusal of
    # anything else has no text (Puppet's description of it is empty).
    marker = object()
    assert assert_instance_of("Found value", parse_type("NotUndef"), marker) is marker
    assert err(parse_type("Optional"), 5) == ""

    # A type alias whose own `.instance()` fails collapses to one mismatch
    # on the alias itself (never the branches' own structural detail).
    assert err(parse_type("RichDataKey"), [1, 2]) == (
        "Found value has wrong type, expects a RichDataKey value, got Tuple"
    )

    # Array/Collection/Tuple/Struct all reject a non-list/non-dict value the
    # same way, before any element/size check.
    assert err(parse_type("Array[Integer]"), "not a list") == (
        "Found value has wrong type, expects an Array value, got String"
    )
    assert err(parse_type("Collection"), "not a list") == (
        "Found value has wrong type, expects a Collection value, got String"
    )
    assert err(parse_type("Tuple[Integer]"), "not a list") == (
        "Found value has wrong type, expects a Tuple value, got String"
    )
    assert err(parse_type("Struct[{a=>String}]"), [1, 2]) == (
        "Found value has wrong type, expects a Struct value, got Tuple"
    )

    # `_size_text`'s shapes: unlimited (the Collection case above), "at least" (lower
    # bound only), "at most" (upper bound only), "between" (the Tuple/Hash cases).
    assert err(parse_type("Array[Integer,3]"), [1, 2]) == (
        "Found value has wrong type, expects size to be at least 3, got 2"
    )
    assert err(parse_type("Array[Integer,default,3]"), [1, 2, 3, 4]) == (
        "Found value has wrong type, expects size to be at most 3, got 4"
    )

    # A Variant with a mix of immediate (whole-value) and deeper (nested
    # path) failures lists every branch's, each prefixed with its own
    # "variant N" path element.
    assert err(parse_type("Variant[Hash[String,String],Array]"), {"a": 1, "b": 2}) == (
        "Found value has wrong type, variant 0 entry 'a' expects a String "
        "value, got Integer\n"
        " Found value has wrong type, variant 0 entry 'b' expects a String "
        "value, got Integer\n"
        " Found value has wrong type, variant 1 expects an Array value, got Struct"
    )

    # `short_name`'s own top-level "expected is a raw literal string"
    # branch (distinct from the wrapper-type case above): NotUndef[integer]
    # unwraps to its raw-str contained type directly (not re-wrapped).
    assert err(parse_type("NotUndef[integer]"), 5) == (
        "Found value has wrong type, expects a String value, got Integer"
    )

    # A wrapper type whose contained type is literally `Any` renders bare
    # (no "[Any]" suffix) -- `short_name`'s own "or not _is_any(contained)"
    # guard.
    assert err(parse_type("Sensitive[Any]"), 5) == (
        "Found value has wrong type, expects a Sensitive value, got Integer"
    )

    # An Optional wrapping a Variant: the "Undef" branch ahead of the list-shaped
    # expected types, `_join_or`'s 3-way "a, b, or c" join.
    assert err(parse_type("Optional[Variant[Integer,Boolean]]"), "x") == (
        "Found value has wrong type, expects a value of type Undef, "
        "Integer, or Boolean, got String"
    )

    # Two Variant branches that render to the *same* short name dedupe down
    # to a single type (no "or" at all).
    assert err(parse_type("Variant[Array[String], Array[Integer]]"), 5) == (
        "Found value has wrong type, expects an Array value, got Integer"
    )

    # `_actual_literal`'s own fallback (a non-string actual value against a
    # pattern-shaped expected type renders the actual's short name, not a
    # quoted literal).
    assert err(parse_type("Enum[a,b]"), 5) == (
        "Found value has wrong type, expects a match for Enum['a', 'b'], " "got Integer"
    )

    # A type alias whose own `.instance()` succeeds: `_describe` returns no
    # mismatches for it, same as any other matching type.
    assert (
        assert_instance_of("Found value", parse_type("RichDataKey"), "a string")
        == "a string"
    )

    # A literal-string contained type (NotUndef[integer]) that DOES match.
    assert (
        assert_instance_of("Found value", parse_type("NotUndef[integer]"), "integer")
        == "integer"
    )

    # A Variant where the FIRST branch fails immediately (shallow) and a
    # LATER one fails deeper: both are listed, in branch order.
    assert err(
        parse_type("Variant[Boolean, Hash[String,String]]"), {"a": 1, "b": 2}
    ) == (
        "Found value has wrong type, variant 0 expects a Boolean value, got Struct\n"
        " Found value has wrong type, variant 1 entry 'a' expects a String "
        "value, got Integer\n"
        " Found value has wrong type, variant 1 entry 'b' expects a String "
        "value, got Integer"
    )


def test_size_text_direct():
    # `_size_text`'s "unlimited" branch is unreachable through the `_describe_*`
    # functions (each normalizes its bounds before `_size_mismatch`); exercised
    # directly.
    assert _size_text(None, None) == "unlimited"
    assert _size_text(3, None) == "at least 3"
    assert _size_text(None, 3) == "at most 3"
    assert _size_text(1, 3) == "between 1 and 3"


def test_a_an_direct():
    # _a_an's leading-quote skip (Puppet's a_an handles a quoted label): a quote is
    # skipped, a letter stops the loop, an all-quote or empty label finds none.
    assert _a_an("'x'") == "a"
    assert _a_an("'Anvil'") == "an"
    assert _a_an("''") == "a"
    assert _a_an("") == "a"


def test_assert_nil_ok_and_reference():
    assert (
        assert_instance_of("Found value", parse_type("String"), None, nil_ok=True)
        is None
    )
    with pytest.raises(HieraLookupError) as exc_info:
        assert_instance_of("Found value", parse_type("Stdlib::Port"), "80")
    assert str(exc_info.value) == (
        "Found value has wrong type, references an unresolved type 'Stdlib::Port'"
    )


@pytest.mark.parametrize(
    "type_, data",
    [
        ("Variant", 5),
        ("Struct[{a=>Optional[Integer]}]", {}),
        ("Struct", {}),
    ],
)
def test_assert_reports_bare_forms_without_crashing(type_, data):
    try:
        assert_instance_of("Found value", parse_type(type_), data)
    except HieraLookupError:
        pass


def test_struct_of_non_string_keys_reports_a_hash_type_mismatch():
    t = parse_type("Struct[{a=>Integer, Optional[b]=>String}]")
    with pytest.raises(HieraLookupError) as info:
        assert_instance_of("Found value", t, {1: "a"})
    assert str(info.value) == (
        "Found value has wrong type, expects a Struct[{'a' => Integer, "
        "Optional['b'] => String}] value, got Hash[Integer[1, 1], String]"
    )
    with pytest.raises(HieraLookupError, match="expects size to be between 1 and 2"):
        assert_instance_of("Found value", t, {})


def _text(type_text, value):
    with pytest.raises(HieraLookupError) as info:
        assert_instance_of("Found value", parse_type(type_text), value)
    return str(info.value)


_W = "Found value has wrong type,"


def test_struct_lists_unrecognized_keys_after_entry_mismatches():
    t = "Struct[{a=>Integer, Optional[b]=>String}]"
    assert _text(t, {"a": "x", "b": 3, "z": 1}) == (
        _W + " entry 'a' expects an Integer value, got String\n"
        " " + _W + " entry 'b' expects a String value, got Integer\n"
        " " + _W + " unrecognized key 'z'"
    )


def test_struct_lists_a_missing_key_before_an_unrecognized_one():
    t = "Variant[Struct[{a=>String}], String]"
    assert _text(t, {"b": 1}) == (
        _W + " variant 0 expects a value for key 'a'\n"
        " " + _W + " variant 0 unrecognized key 'b'\n"
        " " + _W + " variant 1 expects a String value, got Struct"
    )


def test_variant_lists_the_mismatches_of_every_branch():
    t = "Variant[String[1], Array[String[1]]]"
    assert _text(t, [5, ""]) == (
        _W + " variant 0 expects a String value, got Tuple\n"
        " " + _W + " variant 1 index 0 expects a String value, got Integer\n"
        " " + _W + " variant 1 index 1 expects a String[1] value, got String"
    )
    assert _text("Variant[Struct[{a=>String}], String]", {"a": 1}) == (
        _W + " variant 0 entry 'a' expects a String value, got Integer\n"
        " " + _W + " variant 1 expects a String value, got Struct"
    )


def test_optional_variant_has_an_undef_branch_of_its_own():
    t = "Optional[Variant[String[1], Array[String[1]]]]"
    assert _text(t, [5]) == (
        _W + " variant 0 expects an Undef value, got Tuple\n"
        " " + _W + " variant 1 expects a String value, got Tuple\n"
        " " + _W + " variant 2 index 0 expects a String value, got Integer"
    )


def test_variant_branches_failing_on_size_merge_into_one_size_mismatch():
    assert _text("Variant[Array[String,2], Array[String,3]]", ["a"]) == (
        _W + " expects size to be at least 2, got 1"
    )


def test_variant_renders_every_expected_type_in_full_when_any_is_detailed():
    t = "Variant[String[1], Array[String[1]]]"
    assert _text(t, "") == (
        _W + " expects a value of type String[1] or Array[String[1]], got String"
    )
    assert _text("Variant[Integer, Enum[a,b]]", "d") == (
        _W + " expects a value of type Integer or Enum['a', 'b'], got String"
    )


def test_merged_enums_collapse_into_one_enum():
    assert _text("Variant[Enum[a,b], Enum[c]]", "d") == (
        _W + " expects a match for Enum['a', 'b', 'c'], got 'd'"
    )


def test_optional_wrapper_does_not_reach_a_nested_mismatch():
    assert _text("Optional[Struct[{a=>Integer}]]", {"a": "x"}) == (
        _W + " entry 'a' expects an Integer value, got String"
    )
    assert _text("Optional[Hash[String, Integer]]", {"a": "x"}) == (
        _W + " entry 'a' expects an Integer value, got String"
    )
    assert _text("Struct[{a=>Optional[Integer]}]", {"a": "x"}) == (
        _W + " entry 'a' expects a value of type Undef or Integer, got String"
    )


def test_hash_with_a_non_string_key_is_one_mismatch_on_the_hash():
    assert _text("Hash[Pattern[/a/], String]", {5: "x"}) == (
        _W + " expects a Hash[Pattern[/a/], String] value, "
        "got Hash[Integer[5, 5], String]"
    )
    assert _text("Hash[String[1], Any]", {"datadir": "data", 5: "x"}) == (
        _W + " expects a Hash[String[1], Any] value, "
        "got Hash[Variant[String, Integer[5, 5]], Enum['data', 'x']]"
    )
    assert _text("Hash[String, Integer]", {5: "x", 6: "y", "z": "w"}) == (
        _W + " expects a Hash[String, Integer] value, "
        "got Hash[Variant[Integer[5, 6], String], Enum['w', 'x', 'y']]"
    )


def test_a_key_is_named_as_written_without_escaping():
    assert _text("Struct[{a=>Integer}]", {"a": 1, "we'ird": 2}) == (
        _W + " unrecognized key 'we'ird'"
    )
    assert _text("Hash[String, Integer]", {"we'ird": "x", "k": 1}) == (
        _W + " entry 'we'ird' expects an Integer value, got String"
    )


def test_data_with_a_nested_failure_lists_the_variants():
    assert _text("Data", [1, {1: 2}]) == (
        _W + " variant 0 expects a ScalarData value, got Tuple\n"
        " " + _W + " variant 1 expects a Hash value, got Tuple\n"
        " " + _W + " variant 2 index 1 expects a Data value, got Hash"
    )
