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
    """Register four in-test backends in this test's registry copy.

    One per kind, each overriding one hook (so ``Backend.implements`` reports a
    single capability), plus one implementing none."""

    class TestLookupKeyBackend(Backend):
        NAMES = {"function": ("test_lookup_key",)}

        def lookup_key(self, key, options, context):
            # `lookup_options` is gathered through this provider before every
            # lookup; unless a test sets `script["handle_lookup_options"]`, that
            # gather must be an invisible miss, not a recorded call.
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
