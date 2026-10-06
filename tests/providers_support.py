"""Fixtures and helpers shared by the function provider test modules."""

import copy

import pytest

from hyera.backends import Backend


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test gets its own copy of the process-global backend registry,
    so a throwaway registration here can never collide with another test
    module or the real built-ins."""
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def script():
    return {"lookup_key": None, "data_dig": None, "data_hash": None}


@pytest.fixture
def backends(_isolated_registry, calls, script):
    """Register four fresh in-test backends into this test's isolated
    registry copy: one per kind (each overriding exactly one hook, so
    ``Backend.implements`` reports a single capability) plus one
    implementing none of the three."""

    class TestLookupKeyBackend(Backend):
        NAMES = {"function": ("test_lookup_key",)}

        def lookup_key(self, key, options, context):
            # `lookup_options` is gathered through this exact provider
            # before every real lookup (`retrieve_lookup_options`);
            # every test but `test_lookup_options_through_lookup_key_provider`
            # (which sets `script["handle_lookup_options"]`) wants that
            # gather to be an invisible miss, not another recorded call.
            if key == "lookup_options" and not script.get("handle_lookup_options"):
                context.not_found()
            calls.append(("lookup_key", key, dict(options)))
            return script["lookup_key"](key, options, context)

    class TestDataDigBackend(Backend):
        NAMES = {"function": ("test_data_dig",)}

        def data_dig(self, key_segments, options, context):
            if key_segments == ["lookup_options"] and not script.get(
                "handle_lookup_options"
            ):
                context.not_found()
            calls.append(("data_dig", tuple(key_segments), dict(options)))
            return script["data_dig"](key_segments, options, context)

    class TestDataHashBackend(Backend):
        NAMES = {"function": ("test_data_hash",)}

        def data_hash(self, path, options):
            calls.append(("data_hash", path, dict(options)))
            return script["data_hash"](path, options)

    class TestNoneBackend(Backend):
        NAMES = {"function": ("test_none",)}

    class _Backends:
        lookup_key = TestLookupKeyBackend
        data_dig = TestDataDigBackend
        data_hash = TestDataHashBackend
        none = TestNoneBackend

    return _Backends
