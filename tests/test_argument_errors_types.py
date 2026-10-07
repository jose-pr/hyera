"""A caller's bad argument to the type objects and ``facts_from_facter`` is a plain
``TypeError`` or ``ValueError``; the same type arriving from data stays a lookup
error. The class is asserted exactly: ``InterpolationError`` and ``MergeError`` are
also ``ValueError`` subclasses.
"""

import pytest

import hyera
from hyera import Hiera, HieraError, HieraLookupError
from hyera import types as T


def _plain(excinfo, kind):
    assert type(excinfo.value) is kind
    assert not isinstance(excinfo.value, HieraError)


@pytest.mark.parametrize(
    "build",
    [
        lambda: T.Integer[2, 1],
        lambda: T.Integer[1, 2, 3],
        lambda: T.Integer["a"],
        lambda: T.Array[5],
        lambda: T.Array["x"],
        lambda: T.Enum[5],
        lambda: T.Boolean[1],
        lambda: T.Any[1],
        lambda: T.Pattern["("],
        # `None` is Puppet's `default`, which no type position takes.
        lambda: T.Optional[None],
        lambda: T.NotUndef[None],
        lambda: T.Variant[None],
        lambda: T.Variant[T.Integer, None],
        lambda: T.Hash[None, T.Integer],
        lambda: T.Hash[T.Integer, None],
        lambda: T.Tuple[None, T.Integer],
        lambda: T.Sensitive[None],
    ],
)
def test_a_type_subscript_the_caller_gets_wrong_is_a_plain_value_error(build):
    with pytest.raises(Exception) as excinfo:
        build()
    _plain(excinfo, ValueError)


@pytest.mark.parametrize(
    "convert",
    [
        lambda: T.Integer("x"),
        lambda: T.Integer(None),
        lambda: T.Float("x"),
        lambda: T.Boolean("x"),
        lambda: T.Hash(5),
        lambda: T.Integer[1, 5](9),
    ],
)
def test_a_value_the_caller_cannot_convert_is_a_plain_value_error(convert):
    with pytest.raises(Exception) as excinfo:
        convert()
    _plain(excinfo, ValueError)


def test_a_wrongly_typed_subscript_argument_is_still_a_type_error():
    with pytest.raises(Exception) as excinfo:
        T.Optional[5]
    _plain(excinfo, TypeError)


def test_a_type_subscript_or_call_that_works_is_unchanged():
    assert T.Integer[1, 5]("3") == 3
    assert str(T.Array[T.Integer, 1]) == "Array[Integer, 1]"


def test_forms_puppet_accepts_stay_accepted():
    # Each is read by Puppet's type parser or evaluator without error.
    assert not isinstance(["a"] * 2, T.Tuple[5])
    assert isinstance(["a"] * 5, T.Tuple[5])
    assert isinstance("abc", T.String[-1])
    assert T.Array(5) == [0, 1, 2, 3, 4]
    assert isinstance({}, T.Hash[None, None])
    assert not isinstance({"a": 1}, T.Hash[None, None])
    assert isinstance(["a"], T.Tuple[T.String, None])


def test_the_same_conversion_from_a_convert_to_in_data_stays_a_lookup_error(
    make_tree,
):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        {"data/one.yaml": "lookup_options:\n  k:\n    convert_to: Integer\nk: x\n"},
    )
    with pytest.raises(HieraLookupError) as excinfo:
        Hiera(str(root / "hiera.yaml")).lookup("k")
    assert type(excinfo.value) is HieraLookupError


@pytest.mark.parametrize(
    "timeout, kind",
    [("x", TypeError), (True, TypeError), (0, ValueError), (-1, ValueError)],
)
def test_a_bad_facter_timeout_is_a_plain_error_raised_before_facter_runs(timeout, kind):
    with pytest.raises(Exception) as excinfo:
        hyera.facts_from_facter(timeout=timeout)
    _plain(excinfo, kind)
