"""A caller's bad argument is a plain ``TypeError`` or ``ValueError``; the same
strategy or type arriving from data stays a package error.

``MergeError`` and ``InterpolationError`` are also ``ValueError`` subclasses, so
every check here asserts the exact class or the ``HieraError`` exclusion, never
``pytest.raises(ValueError)`` alone.
"""

import pytest

import hyera
from hyera import Hiera, HieraError, HieraLookupError, MergeError
from hyera import types as T


@pytest.fixture
def hiera(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        {"data/one.yaml": "k: v\nh: {a: 1}\nn: 5\n"},
    )
    return Hiera(str(root / "hiera.yaml"))


def _plain(excinfo, kind):
    """The exception is exactly ``kind`` (``TypeError``/``ValueError``) and no
    package error."""
    assert type(excinfo.value) is kind
    assert not isinstance(excinfo.value, HieraError)


BAD_STRATEGIES = [
    "bogus",
    {"x": 1},
    {"strategy": "bogus"},
    {"strategy": "deep", "no_such_option": 1},
    {"strategy": "deep", "sort_merged_arrays": "yes"},
    "",
]
BAD_TYPE_EXPRESSIONS = ["Bogus[", "Integer[", "Array[Integer, 'x']", "Integer[2, 1]"]

# Each entry point that takes `merge`, as a callable of (instance, merge).
MERGE_CALLS = {
    "lookup": lambda h, m: h.lookup("k", merge=m),
    "lookup-positional": lambda h, m: h.lookup("k", None, m),
    "lookup-options-hash": lambda h, m: h.lookup("k", {"merge": m}),
    "lookup-name-hash": lambda h, m: h.lookup({"name": "k", "merge": m}),
    "lookup-subscript": lambda h, m: h["k", None, m],
    "call": lambda h, m: h("k", merge=m),
    "explain": lambda h, m: h.explain("k", merge=m),
    "dig": lambda h, m: h.dig("h", "a", merge=m),
    "get": lambda h, m: h.get("h.a", merge=m),
    "get-int-root": lambda h, m: h.get("0.a", merge=m),
    "to_dict": lambda h, m: h.to_dict(merge=m),
    "one-shot": lambda h, m: hyera.lookup(h.base_config, "k", merge=m),
}

# Each entry point that takes `value_type`, as a callable of (instance, type).
TYPE_CALLS = {
    "lookup": lambda h, t: h.lookup("k", value_type=t),
    "lookup-positional": lambda h, t: h.lookup("k", t),
    "lookup-options-hash": lambda h, t: h.lookup("k", {"value_type": t}),
    "lookup-name-hash": lambda h, t: h.lookup({"name": "k", "value_type": t}),
    "lookup-subscript": lambda h, t: h["k", t],
    "call": lambda h, t: h("k", t),
    "explain": lambda h, t: h.explain("k", t),
    "dig": lambda h, t: h.dig("h", "a", value_type=t),
    "get": lambda h, t: h.get("h.a", value_type=t),
    "one-shot": lambda h, t: hyera.lookup(h.base_config, "k", t),
}


@pytest.mark.parametrize("call", MERGE_CALLS)
@pytest.mark.parametrize("merge", BAD_STRATEGIES, ids=repr)
def test_unusable_merge_argument_is_a_plain_value_error(hiera, call, merge):
    with pytest.raises(Exception) as excinfo:
        MERGE_CALLS[call](hiera, merge)
    _plain(excinfo, ValueError)


@pytest.mark.parametrize("call", MERGE_CALLS)
@pytest.mark.parametrize("merge", [5, 1.5, ["first"], {1: "x"}, True], ids=repr)
def test_wrongly_typed_merge_argument_is_a_plain_type_error(hiera, call, merge):
    with pytest.raises(Exception) as excinfo:
        MERGE_CALLS[call](hiera, merge)
    _plain(excinfo, TypeError)


def test_unusable_merge_is_rejected_before_any_data_is_read(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        {"data/one.yaml": "k: [unclosed\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ValueError, match="merge") as excinfo:
        h.lookup("k", merge="bogus")
    _plain(excinfo, ValueError)


def test_unusable_merge_is_rejected_even_when_the_override_would_answer(hiera):
    with pytest.raises(ValueError) as excinfo:
        hiera.lookup("k", merge="bogus", override={"k": "x"})
    _plain(excinfo, ValueError)


def test_unusable_merge_to_dict_is_rejected_with_no_keys():
    h = Hiera({"version": 5, "hierarchy": []})
    with pytest.raises(ValueError) as excinfo:
        h.to_dict(merge="bogus")
    _plain(excinfo, ValueError)


@pytest.mark.parametrize("call", TYPE_CALLS)
@pytest.mark.parametrize("value_type", BAD_TYPE_EXPRESSIONS, ids=repr)
def test_unparsable_value_type_argument_is_a_plain_value_error(hiera, call, value_type):
    with pytest.raises(Exception) as excinfo:
        TYPE_CALLS[call](hiera, value_type)
    _plain(excinfo, ValueError)


@pytest.mark.parametrize("call", TYPE_CALLS)
@pytest.mark.parametrize("value_type", [5, 1.5, ["Integer"], object()], ids=repr)
def test_wrongly_typed_value_type_argument_is_a_plain_type_error(
    hiera, call, value_type
):
    with pytest.raises(Exception) as excinfo:
        TYPE_CALLS[call](hiera, value_type)
    _plain(excinfo, TypeError)


def test_value_error_message_names_the_argument_and_what_was_given(hiera):
    with pytest.raises(ValueError, match=r"merge.*bogus"):
        hiera.lookup("k", merge="bogus")
    with pytest.raises(ValueError, match=r"value_type.*Bogus\["):
        hiera.lookup("k", "Bogus[")


def test_valid_value_type_that_the_found_value_does_not_match_stays_a_lookup_error(
    hiera,
):
    for call in (
        lambda: hiera.lookup("k", "Integer"),
        lambda: hiera.dig("h", "a", value_type="String"),
        lambda: hiera.get("h.a", value_type="String"),
        lambda: hyera.lookup(hiera.base_config, "k", "Integer"),
    ):
        with pytest.raises(HieraLookupError) as excinfo:
            call()
        assert isinstance(excinfo.value, HieraError)
        assert not isinstance(excinfo.value, (TypeError, ValueError))
    with pytest.raises(HieraLookupError):
        hiera.explain("k", "Integer")


def test_a_type_name_the_package_cannot_resolve_stays_a_run_time_failure(hiera):
    # A name may stand for a type a Puppet module defines: the argument alone
    # does not say it is wrong, so a miss is still a miss and a hit fails.
    with pytest.raises(hyera.KeyNotFoundError):
        hiera.lookup("absent", "Nonesuch")
    with pytest.raises(HieraLookupError) as excinfo:
        hiera.lookup("k", "Nonesuch")
    assert type(excinfo.value) is HieraLookupError


def test_a_strategy_in_lookup_options_data_stays_a_merge_error(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "one", "path": "one.yaml"},
                {"name": "two", "path": "two.yaml"},
            ]
        },
        {
            "data/one.yaml": "lookup_options:\n  k:\n    merge: bogus\nk: [a]\n",
            "data/two.yaml": "k: [b]\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(MergeError) as excinfo:
        h.lookup("k")
    assert type(excinfo.value) is MergeError
    assert isinstance(excinfo.value, HieraError)
    assert h.lookup("k", merge="first") == ["a"]


def test_a_merge_options_hash_without_a_strategy_in_data_stays_a_merge_error(
    make_tree,
):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        {
            "data/one.yaml": "lookup_options:\n  k:\n    merge: {knockout_prefix: x}\nk: [a]\n"
        },
    )
    with pytest.raises(MergeError):
        Hiera(str(root / "hiera.yaml")).lookup("k")


def test_a_convert_to_type_in_data_stays_a_lookup_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        {"data/one.yaml": "lookup_options:\n  k:\n    convert_to: 'Bogus['\nk: v\n"},
    )
    with pytest.raises(HieraLookupError) as excinfo:
        Hiera(str(root / "hiera.yaml")).lookup("k")
    assert type(excinfo.value) is HieraLookupError


def test_a_valid_merge_and_type_still_work(hiera):
    assert hiera.lookup("k", "String", "first") == "v"
    assert hiera.lookup("k", merge={"strategy": "first"}) == "v"
    assert hiera.get("h.a", value_type="Integer", merge="hash") == 1
    assert hiera.dig("h", "a", value_type=T.Integer, merge="deep") == 1
    assert hiera.to_dict(merge="first")["k"] == "v"
