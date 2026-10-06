"""``new()`` and each type's own conversion function."""

import json

import pytest
import yaml

from hyera import HieraLookupError, Sensitive
from hyera._types.new_function import new_instance
from hyera._types.parser import parse_type
from types_support import (  # noqa: F401
    _CASES,
    _data,
    _golden,
)

# ----------------------------------------------------------- new() / format


def _run_new(spec, value):
    """Build new()'s args from a lookup_options convert_to spec exactly as
    the lookup engine does: a list as is, else [spec]; a str first element goes
    through parse_type, anything else (a malformed convert_to shape) is
    passed to new_instance raw."""
    args = list(spec) if isinstance(spec, list) else [spec]
    type_arg = args[0] if args else None
    rest = args[1:]
    type_ = parse_type(type_arg) if isinstance(type_arg, str) else type_arg
    return new_instance(type_, value, *rest)


def _new_params():
    for case_name in ("convert-new", "convert-cv"):
        case = yaml.safe_load(
            (_CASES / case_name / "case.yaml").read_text(encoding="utf-8")
        )
        golden = _golden(case_name)["results"]
        data = _data(case_name)
        opts = data["lookup_options"]
        seen = set()
        for q in case["queries"]:
            if q.get("merge") is not None:
                continue
            key = q["key"]
            qid = q.get("id") or key
            if qid in seen:
                continue
            seen.add(qid)
            spec = opts.get(key, {}).get("convert_to")
            if spec is None:
                continue
            yield pytest.param(
                qid, spec, data[key], golden[qid], id="{}::{}".format(case_name, qid)
            )


#: hyera has no new() for these (Puppet does) -- ours is a
#: deliberate deviation, never the golden's recorded (Puppet-real) outcome.
_Q8_UNSUPPORTED_QIDS = {"semver", "tspan", "re_t"}


#: convert-cv/tuple_t: a real, oracle-confirmed discrepancy between what
#: ``Tuple.new()`` reports through a direct call (both index 0 and index 1
#: mismatch, verified with `puppet apply`) and what the SAME conversion
#: reports through `lookup_options`/`convert_to` (only index 1) -- the
#: lookup_adapter path's own error surfaces only the last element checked.
#: Not reproduced here (this subset collects every mismatch, matching the
#: direct-call oracle behaviour); asserted loosely instead of exactly.
_KNOWN_QUIRK_QIDS = {"tuple_t"}


@pytest.mark.parametrize("qid,spec,value,result", list(_new_params()))
def test_new_matches_golden(qid, spec, value, result):
    from hyera import Sensitive

    if qid in _Q8_UNSUPPORTED_QIDS:
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, value)
        assert "hyera does not support new() for the Puppet type" in str(exc_info.value)
        return

    message = result.get("message", "")
    if "could not parse" in message and message.startswith(
        "Invalid data type in lookup_options for key"
    ):
        inner = message.split("error: '", 1)[1]
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, value)
        assert str(exc_info.value) == inner
        return
    if result["status"] == "error":
        inner = (
            message.split("raised error: ", 1)[1]
            if "raised error: " in message
            else message
        )
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, value)
        if qid in _KNOWN_QUIRK_QIDS:
            assert inner.splitlines()[-1].strip() in str(exc_info.value)
        else:
            assert str(exc_info.value) == inner
        return
    got = _run_new(spec, value)
    if isinstance(got, Sensitive):
        got = "Sensitive [value redacted]"
    assert json.dumps(got, sort_keys=True) == json.dumps(
        result["value"], sort_keys=True
    )


def test_new_unrecorded():
    with pytest.raises(HieraLookupError) as exc_info:
        _run_new([parse_type("Integer"), "default", True], "-5")
    assert str(exc_info.value) == (
        "Illegal radix: default, expected 2, 8, 10, 16, or default"
    )

    for spec in (
        [parse_type("String"), {"Integer": "%x"}],
        [parse_type("String"), 5],
    ):
        with pytest.raises(HieraLookupError) as exc_info:
            _run_new(spec, "x" if not isinstance(spec[1], int) else 5)
        assert "'new_string' parameter 'string_formats'" in str(exc_info.value)

    with pytest.raises(HieraLookupError) as exc_info:
        _run_new(parse_type("Integer"), {"a": 1})
    assert "Integer.new has wrong type" in str(
        exc_info.value
    ) or "unrecognized key 'a'" in str(exc_info.value)


def test_new_dispatch_optional_notundef_bare_and_literal_contained():
    # _dispatch directly: new_instance()'s own fast path ("not args and
    # type_.instance(value): return value") would otherwise skip _dispatch
    # entirely here, since any non-None value trivially satisfies a bare
    # Optional/NotUndef's own instance() check.
    from hyera._types.new_function import _dispatch

    # A bare Optional/NotUndef (no contained type argument at all) is not
    # actually new()-able on its own -- Puppet has no meaningful
    # "construct an Optional" operation without a contained type.
    with pytest.raises(HieraLookupError, match="is not supported"):
        _dispatch(parse_type("Optional"), "x", ())
    with pytest.raises(HieraLookupError, match="is not supported"):
        _dispatch(parse_type("NotUndef"), "x", ())
    # A bareword literal contained type (Optional[integer]): dispatches
    # through a plain String new(), since the literal is kept as a raw str
    # rather than a real type.
    assert _dispatch(parse_type("Optional[integer]"), "integer", ()) == "integer"


def test_new_integer_binary_literal():
    from hyera._types.new_function import new_instance

    assert new_instance(parse_type("Integer"), "0b101") == 5


def test_new_dispatch_integer_int_passthrough_and_empty_dict():
    # _dispatch directly (same fast-path reason as the Optional/NotUndef
    # case above -- an int already satisfies Integer's own instance()).
    from hyera._types.new_function import _dispatch

    assert _dispatch(parse_type("Integer"), 5, ()) == 5
    # An empty dict is a named-arguments hash with no entry at all.
    with pytest.raises(HieraLookupError) as exc_info:
        _dispatch(parse_type("Integer"), {}, ())
    assert str(exc_info.value) == (
        "Integer.new has wrong type, expects size to be between 1 and 3, got 0"
    )


def test_new_dispatch_float_and_numeric_from_int_and_unsupported_type():
    # _dispatch directly, for the same reason as above: an int already
    # satisfies Numeric's own instance() check (unlike Float's, so that
    # one call goes through new_instance() normally).
    from hyera._types.new_function import _dispatch, new_instance

    assert new_instance(parse_type("Float"), 5) == 5.0
    assert _dispatch(parse_type("Numeric"), 5, ()) == 5
    with pytest.raises(HieraLookupError, match="cannot be converted to Float"):
        _dispatch(parse_type("Float"), [1, 2], ())
    with pytest.raises(HieraLookupError, match="cannot be converted to Numeric"):
        _dispatch(parse_type("Numeric"), [1, 2], ())


@pytest.mark.parametrize(
    "type_, value, args, expected",
    [
        ("Integer", "-11", (16, True), 17),
        ("Integer", "-5", (10, True), 5),
        ("Integer", -5, (10, True), 5),
        ("Integer", {"from": "-5", "abs": True}, (), 5),
        ("Integer", {"from": "10", "radix": 16}, (), 16),
        ("Integer", "0x1F", (), 31),
        ("Integer", "- 11", (), -11),
        ("Float", "-5.5", (True,), 5.5),
        ("Float", {"from": "-5.5", "abs": True}, (), 5.5),
        ("Float", "0x1F", (), 31.0),
        ("Float", "- 1.5", (), -1.5),
        ("Numeric", -5, (True,), 5),
        ("Numeric", "- 1.5", (), -1.5),
        ("Numeric", "010", (), 8),
        ("Numeric", "0x10", (), 16),
    ],
)
def test_new_numbers_follow_puppet(type_, value, args, expected):
    got = new_instance(parse_type(type_), value, *args)
    assert got == expected and type(got) is type(expected)


@pytest.mark.parametrize(
    "type_, value",
    [
        ("Integer", "12\n"),
        ("Integer", "١٢"),
        ("Integer", "1_000"),
        ("Float", "inf"),
        ("Float", "nan"),
        ("Float", "1_000.5"),
        ("Float", "1.5 "),
        ("Float", ".5"),
        ("Float", "1."),
        ("Float", "1.5e+3"),
        ("Numeric", "1 "),
        ("Numeric", "1_000"),
    ],
)
def test_new_numbers_reject_strings_puppet_rejects(type_, value):
    with pytest.raises(HieraLookupError, match="cannot be converted to"):
        new_instance(parse_type(type_), value)


@pytest.mark.parametrize(
    "type_, args, message",
    [
        ("Integer", (10, True, 1), "'new' expects between 1 and 3 arguments, got 4"),
        ("Float", (True, True), "'new_float' expects between 1 and 2 arguments, got 3"),
        (
            "Numeric",
            (True, True),
            "'new_numeric' expects between 1 and 2 arguments, got 3",
        ),
        ("Boolean", (True,), "'new_boolean' expects 1 argument, got 2"),
        ("Sensitive", (1,), "'new_sensitive' expects 1 argument, got 2"),
        ("Array", (True, True), "'new_array' expects between 1 and 2 arguments, got 3"),
        (
            "Array",
            ("x",),
            "'new_array' parameter 'wrap' expects a Boolean value, got String",
        ),
        ("Integer", (3,), "Illegal radix: 3, expected 2, 8, 10, 16, or default"),
    ],
)
def test_new_checks_arguments_as_puppet_does(type_, args, message):
    with pytest.raises(HieraLookupError) as info:
        new_instance(parse_type(type_), "5", *args)
    assert str(info.value) == message


def test_new_numeric_hash_that_is_not_named_arguments_is_just_a_bad_value():
    with pytest.raises(HieraLookupError, match="cannot be converted to Float"):
        new_instance(parse_type("Float"), {"from": 5, "x": 1})
