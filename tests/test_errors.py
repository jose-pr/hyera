"""The exception hierarchy and the lookup miss/error boundary."""

import pickle

import pytest

import pyera
from pyera import (
    BackendError,
    ConfigError,
    Hiera,
    HieraError,
    HieraLookupError,
    InterpolationError,
    KeyNotFoundError,
    MergeError,
    make_merge,
)


def test_hierarchy_and_exports():
    assert issubclass(ConfigError, HieraError)
    assert issubclass(BackendError, HieraError)
    assert issubclass(HieraLookupError, HieraError)
    assert issubclass(InterpolationError, HieraLookupError)
    assert issubclass(MergeError, HieraLookupError)
    assert issubclass(KeyNotFoundError, HieraLookupError)
    assert issubclass(KeyNotFoundError, KeyError)

    names = [
        "HieraError",
        "ConfigError",
        "BackendError",
        "HieraLookupError",
        "InterpolationError",
        "MergeError",
        "KeyNotFoundError",
    ]
    for name in names:
        assert name in pyera.__all__
        assert getattr(pyera, name) is getattr(pyera.exceptions, name)


def test_error_attributes_survive_pickle():
    e = BackendError("x", path="p")
    e2 = pickle.loads(pickle.dumps(e))
    assert e2.path == "p"

    knf = KeyNotFoundError("k")
    knf2 = pickle.loads(pickle.dumps(knf))
    assert knf2.name == "k"
    assert str(knf2) == str(knf)


def test_miss_raises_key_not_found_error(hiera_root):
    hiera = Hiera(str(hiera_root / "hiera.yaml"))
    with pytest.raises(KeyError) as excinfo:
        hiera.get("nope::key", throw=True)
    assert isinstance(excinfo.value, HieraError)
    assert (
        str(excinfo.value)
        == "Function lookup() did not find a value for the name 'nope::key'"
    )
    assert hiera.has("nope::key") is False


def test_not_found_message_for_name_lists():
    assert str(KeyNotFoundError(["a", "b"])) == (
        "Function lookup() did not find a value for any of the names ['a', 'b']"
    )
    assert (
        str(KeyNotFoundError(["a"]))
        == "Function lookup() did not find a value for the name 'a'"
    )


def test_non_str_key_raises_type_error(hiera_root):
    hiera = Hiera(str(hiera_root / "hiera.yaml"))
    with pytest.raises(TypeError):
        hiera.get(5)
    with pytest.raises(TypeError):
        hiera.get(None)
    with pytest.raises(TypeError):
        hiera.has(5)


def test_bad_merge_strategy_raises_merge_error(make_tree):
    with pytest.raises(MergeError) as excinfo:
        make_merge("bogus")
    assert str(excinfo.value) == "Unknown merge strategy: 'bogus'"

    with pytest.raises(MergeError):
        make_merge(["deep"])

    root = make_tree(
        {
            "hierarchy": [
                {"name": "one", "path": "one.yaml"},
                {"name": "two", "path": "two.yaml"},
            ],
        },
        files={
            "data/one.yaml": """\
                k: [a]
                lookup_options:
                  k:
                    merge: bogus
                """,
            "data/two.yaml": """\
                k: [b]
                """,
        },
    )
    hiera = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(MergeError):
        hiera.get("k")
    assert hiera.get("k", merge="first") == ["a"]


def test_internal_keyerror_is_not_chained(make_tree):
    bad_backend_root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml", "data_hash": "nope"}]},
    )
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(bad_backend_root / "hiera.yaml"))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True

    alias_root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={
            "data/one.yaml": """\
                k: "%{alias('missing::key')}"
                """,
        },
    )
    hiera = Hiera(str(alias_root / "hiera.yaml"))
    with pytest.raises(InterpolationError) as excinfo2:
        hiera.get("k", throw=True)
    assert excinfo2.value.__cause__ is None
    assert excinfo2.value.__suppress_context__ is True
