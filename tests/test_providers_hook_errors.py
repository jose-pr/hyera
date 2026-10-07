"""An exception a hook raises itself: a foreign one becomes ``BackendError``."""

from __future__ import annotations

import pytest

from hyera import BackendError, ConfigError, Hiera, InterpolationError
from hyera.backends import Backend, default_backends
from hyera.testing import data_dig, data_hash, lookup_key

from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)

SECRET = "document text that must not reach the message"


def _raiser(exc):
    def raise_it(*args):
        raise exc

    return raise_it


def _hiera(make_tree, entry, files=None):
    root = make_tree({"hierarchy": [entry]}, files=files or {})
    return Hiera(str(root / "hiera.yaml"))


def _entry(kind, **location):
    return dict({"name": "n", kind: "test_" + kind}, **location)


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig", "data_hash"])
@pytest.mark.parametrize(
    "location", [{}, {"uri": "https://x/y"}], ids=["no-location", "uri"]
)
def test_a_foreign_exception_becomes_a_backend_error(
    make_tree, backends, script, kind, location
):
    script[kind] = _raiser(ValueError(SECRET))
    h = _hiera(make_tree, _entry(kind, **location))
    with pytest.raises(BackendError) as excinfo:
        h.lookup("k")
    err = excinfo.value
    assert isinstance(err.__cause__, ValueError)
    assert "ValueError" in str(err) and "test_" + kind in str(err)
    assert SECRET not in str(err)


def test_a_foreign_exception_from_a_file_data_hash_names_the_path(
    make_tree, backends, script
):
    script["data_hash"] = _raiser(KeyError(SECRET))
    h = _hiera(
        make_tree,
        _entry("data_hash", path="a.yaml"),
        files={"data/a.yaml": "k: v\n"},
    )
    with pytest.raises(BackendError) as excinfo:
        h.lookup("k")
    assert "KeyError" in str(excinfo.value) and SECRET not in str(excinfo.value)
    assert str(excinfo.value.path).endswith("a.yaml")
    assert isinstance(excinfo.value.__cause__, KeyError)


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig", "data_hash"])
def test_a_hiera_error_passes_through_unchanged(make_tree, backends, script, kind):
    original = InterpolationError("from the hook")
    script[kind] = _raiser(original)
    h = _hiera(make_tree, _entry(kind))
    with pytest.raises(InterpolationError) as excinfo:
        h.lookup("k")
    assert excinfo.value is original


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig", "data_hash"])
def test_a_keyboard_interrupt_passes_through(make_tree, backends, script, kind):
    script[kind] = _raiser(KeyboardInterrupt())
    h = _hiera(make_tree, _entry(kind))
    with pytest.raises(KeyboardInterrupt):
        h.lookup("k")


def test_the_testing_runners_translate_the_same_way():
    class Hooks(Backend):
        def lookup_key(self, key, options, context):
            raise ValueError(SECRET)

        def data_dig(self, key_segments, options, context):
            raise ValueError(SECRET)

        def data_hash(self, path, options, context):
            raise ValueError(SECRET)

    for run in (
        lambda: lookup_key(Hooks(), "k"),
        lambda: data_dig(Hooks(), ["k"]),
        lambda: data_hash(Hooks(), "p"),
    ):
        with pytest.raises(BackendError) as excinfo:
            run()
        assert "ValueError" in str(excinfo.value)
        assert SECRET not in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)


def test_a_config_error_from_a_hook_is_not_translated():
    class Hooks(Backend):
        def lookup_key(self, key, options, context):
            raise ConfigError("mine")

    with pytest.raises(ConfigError, match="mine"):
        lookup_key(Hooks(), "k")


def test_a_two_parameter_data_hash_chains_pythons_type_error(make_tree):
    class Old(Backend):
        NAMES = {"function": ("hook_errors_old",)}

        def data_hash(self, path, options):
            return {"k": "v"}

    root = make_tree(
        {"hierarchy": [{"name": "n", "data_hash": "hook_errors_old"}]}, files={}
    )
    h = Hiera(str(root / "hiera.yaml"), backends=list(default_backends()) + [Old])
    with pytest.raises(BackendError) as excinfo:
        h.lookup("k")
    assert isinstance(excinfo.value.__cause__, TypeError)
    assert "positional argument" in str(excinfo.value.__cause__)
