"""``hyera.testing``: running a backend's hooks the way the engine does, written
as a third-party backend author would write them."""

from __future__ import annotations

import subprocess
import sys

import pytest

import hyera
from hyera import BackendError, LookupContext, Scope
from hyera.backends import Backend
from hyera.testing import NOT_FOUND, data_dig, data_hash, lookup_key


class _Greeter(Backend):
    """A ``lookup_key`` backend that answers from the context."""

    def lookup_key(self, key, options, context):
        if key == "greeting":
            return context.interpolate("hello %{facts.who}")
        if key == "from_data":
            return context.interpolate("%{alias('settings.port')}")
        if key == "text_from_data":
            return context.interpolate("port=%{lookup('settings.port')}")
        if key == "env":
            return context.environment_name
        if key == "module":
            return context.module_name
        if key == "bad":
            return object()
        if key == "tuple":
            return ("a", ("b",))
        if key == "opt":
            return options.get("flavour")
        context.not_found()


class _Digger(Backend):
    def data_dig(self, key_segments, options, context):
        if key_segments == ["a", "b"]:
            return 7
        context.not_found()


def test_lookup_key_returns_what_the_hook_returns():
    scope = Scope(facts={"who": "world"})
    context = LookupContext.for_testing(scope=scope)
    assert lookup_key(_Greeter(), "greeting", context=context) == "hello world"


def test_a_class_is_instantiated_for_the_call():
    assert lookup_key(_Greeter, "env") == "production"


def test_not_found_gives_the_sentinel():
    assert lookup_key(_Greeter(), "nope") is NOT_FOUND
    assert data_dig(_Digger(), ["x"]) is NOT_FOUND


def test_options_reach_the_hook():
    assert lookup_key(_Greeter(), "opt", {"flavour": "mint"}) == "mint"


def test_interpolate_resolves_lookup_against_data():
    context = LookupContext.for_testing(data={"settings": {"port": 8080}})
    assert lookup_key(_Greeter(), "from_data", context=context) == 8080


def test_a_data_miss_interpolates_as_the_engine_does_a_miss():
    context = LookupContext.for_testing(data={"settings": {}})
    assert lookup_key(_Greeter(), "text_from_data", context=context) == "port="
    assert lookup_key(_Greeter(), "from_data", context=context) == ""


def test_module_name_and_environment_come_from_the_arguments():
    context = LookupContext.for_testing(
        scope=Scope(environment="staging"), module_name="mymod"
    )
    assert lookup_key(_Greeter(), "module", context=context) == "mymod"
    assert lookup_key(_Greeter(), "env", context=context) == "staging"
    assert lookup_key(_Greeter(), "module") is None


def test_a_tuple_comes_back_as_a_list():
    assert lookup_key(_Greeter(), "tuple") == ["a", ["b"]]


def test_a_value_outside_the_data_types_raises_as_a_lookup_does():
    with pytest.raises(BackendError, match="has wrong type"):
        lookup_key(_Greeter(), "bad")


def test_data_dig_receives_the_segments():
    assert data_dig(_Digger(), ["a", "b"]) == 7


def test_a_hook_the_backend_lacks_raises_what_a_lookup_raises():
    with pytest.raises(hyera.ConfigError):
        lookup_key(_Digger(), "k")


def test_cached_file_data_parses_once_across_two_calls(tmp_path):
    source = tmp_path / "f.txt"
    source.write_text("a=1\n", encoding="utf-8")
    parses = []

    class _Reader(Backend):
        def lookup_key(self, key, options, context):
            def parse(text):
                parses.append(text)
                return dict(line.split("=") for line in text.split())

            return context.cached_file_data(source, parse)[key]

    context = LookupContext.for_testing()
    assert lookup_key(_Reader(), "a", context=context) == "1"
    assert lookup_key(_Reader(), "a", context=context) == "1"
    assert len(parses) == 1


def test_cache_is_shared_by_calls_through_one_context():
    context = LookupContext.for_testing()
    context.cache("k", 5)
    assert context.cache_has_key("k") and context.cached_value("k") == 5


def test_explain_never_calls_its_producer():
    context = LookupContext.for_testing()

    def producer():
        raise AssertionError("called")

    context.explain(producer)


def test_the_signature_of_for_testing_has_no_private_parameter():
    import inspect

    names = list(inspect.signature(LookupContext.for_testing).parameters)
    assert names == ["scope", "module_name", "data"]


def test_importing_hyera_does_not_import_hyera_testing_or_pytest():
    code = (
        "import sys, hyera; "
        "assert 'hyera.testing' not in sys.modules; "
        "import hyera.testing; "
        "assert 'pytest' not in sys.modules, 'pytest'; "
        "assert 'hocon' not in sys.modules and 'pyhocon' not in sys.modules"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr


class _Hasher(Backend):
    def data_hash(self, path, options, context):
        return {"path": path, "seen": dict(options), "env": context.environment_name}


class _BadHasher(Backend):
    def data_hash(self, path, options, context):
        if path == "values":
            return {"ok": 1, "bad": object()}
        if path == "tuples":
            return {"t": ("a", ("b",))}
        if path == "miss":
            context.not_found()
        return ["not", "a", "hash"]


def test_data_hash_runs_the_hook_with_a_context_and_the_path_in_options():
    got = data_hash(_Hasher(), "x.yaml", {"k": 1})
    assert got == {
        "path": "x.yaml",
        "seen": {"k": 1, "path": "x.yaml"},
        "env": "production",
    }


def test_data_hash_without_a_path_adds_none_to_options():
    assert data_hash(_Hasher())["seen"] == {}


def test_data_hash_reads_a_tuple_as_a_list():
    assert data_hash(_BadHasher(), "tuples") == {"t": ["a", ["b"]]}


def test_data_hash_validates_its_result():
    with pytest.raises(BackendError, match="expects a Hash"):
        data_hash(_BadHasher(), "other")
    with pytest.raises(hyera.HieraLookupError, match="has wrong type"):
        data_hash(_BadHasher(), "values")


def test_data_hash_not_found_is_an_error():
    with pytest.raises(BackendError, match="not_found"):
        data_hash(_BadHasher(), "miss")
