"""``lookup_key``/``data_dig`` function providers: per-location dispatch,
``LookupContext``, hierarchy ``options``, and location-less entries.

In-test backends (``test_lookup_key``/``test_data_dig``/``test_data_hash``/
``test_none``, one method each, registered under ``function``) isolate the
process-global registry per test (``_isolated_registry``) and record every
call in ``calls``; their return/raise behavior for a given test comes from
``script``, a callable the test sets before constructing a ``Hiera``.
"""

import copy
import os
import re
import time

import pytest

from hyera import BackendError, ConfigError, Hiera, InterpolationError, Scope
from hyera._invocation import Invocation
from hyera._lookup_adapter import extract_lookup_options_for_key
from hyera.backends import Backend, HOCONBackend, JSONBackend, SopsBackend, YAMLBackend


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
            # before every real lookup (`Hiera._retrieve_lookup_options`);
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


def _not_found(key, options, context):
    context.not_found()


# --- lookup_key ------------------------------------------------------


def test_lookup_key_walks_locations_until_found(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "paths": ["a.yaml", "b.yaml"],
                }
            ]
        },
        files={"data/a.yaml": "x", "data/b.yaml": "x"},
    )

    def fn(key, options, context):
        if options["path"].endswith("a.yaml"):
            context.not_found()
        return "found-in-b"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "found-in-b"
    assert [c[0] for c in calls] == ["lookup_key", "lookup_key"]
    assert calls[0][2]["path"].endswith("a.yaml")
    assert calls[1][2]["path"].endswith("b.yaml")


def test_lookup_key_missing_location_not_called(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "paths": ["missing.yaml", "b.yaml"],
                }
            ]
        },
        files={"data/b.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "found"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "found"
    # Only one call: the missing location is a miss without a call.
    assert len(calls) == 1
    assert calls[0][2]["path"].endswith("b.yaml")


def test_lookup_key_cached_per_key_and_location(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert h.lookup("k") == "v"
    assert len(calls) == 1


def test_lookup_key_not_found_is_not_cached(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = _not_found
    h = Hiera(str(root / "hiera.yaml"))
    assert "k" not in h
    assert "k" not in h
    assert len(calls) == 2


def test_lookup_key_value_not_interpolated_by_engine(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "literal %{facts.os.family}"
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    assert h.lookup("k") == "literal %{facts.os.family}"


# --- data_dig ----------------------------------------------------------


def test_data_dig_gets_segments_and_result_is_undug(make_tree, backends, calls, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_dig": "test_data_dig", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_dig"] = lambda key_segments, options, context: "leaf"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("a.b.0") == "leaf"
    assert calls[0][1] == ("a", "b", 0)


def test_data_dig_cached_per_full_key(make_tree, backends, calls, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_dig": "test_data_dig", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_dig"] = lambda key_segments, options, context: "leaf"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("a.b") == "leaf"
    assert h.lookup("a.b") == "leaf"
    assert len(calls) == 1


# --- locations and options ---------------------------------------------


def test_no_location_calls_once_with_options_only(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "options": {"foo": "bar"},
                }
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert len(calls) == 1
    assert calls[0][2] == {"foo": "bar"}


def test_empty_location_list_calls_nothing(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "glob": "nomatch-*.yaml"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert "k" not in h
    assert calls == []


def test_options_interpolated_and_merged_with_path(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "path": "a.yaml",
                    "options": {"foo": "%{facts.os.family}"},
                }
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    h.lookup("k")
    options = calls[0][2]
    assert options["foo"] == "RedHat"
    assert options["path"].endswith("a.yaml")


def test_entry_options_replace_defaults_options(make_tree, backends, calls, script):
    root = make_tree(
        {
            "defaults": {"datadir": "data", "options": {"a": "1"}},
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "path": "a.yaml",
                    "options": {"b": "2"},
                }
            ],
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    h.lookup("k")
    options = calls[0][2]
    assert "b" in options and "a" not in options


@pytest.mark.parametrize(
    "strict, expect_call",
    [("warning", True), ("error", False)],
    ids=["warning-lenient", "error-raises"],
)
def test_options_follow_strict_mode(
    make_tree, backends, calls, script, strict, expect_call
):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "path": "a.yaml",
                    "options": {"foo": "%{nosuch}"},
                }
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml")).scoped(strict=strict)
    if expect_call:
        assert h.lookup("k") == "v"
        assert calls[0][2]["foo"] == ""
    else:
        with pytest.raises(InterpolationError, match="Undefined variable 'nosuch'"):
            h.lookup("k")
        assert calls == []


def test_options_reject_method_syntax(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "path": "a.yaml",
                    "options": {"foo": "%{lookup('k')}"},
                }
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    # A hiera.yaml problem, not a lookup-time one: ConfigError, like the
    # other options-contract failures, not InterpolationError.
    with pytest.raises(
        ConfigError, match="Interpolation using method syntax is not allowed"
    ):
        h.lookup("k")
    assert calls == []


# --- LookupContext -------------------------------------------------------


def test_lookup_context_cache_api(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    seen = {}

    def fn(key, options, context):
        context.cache("side", 123)
        seen["has"] = context.cache_has_key("side")
        seen["value"] = context.cached_value("side")
        seen["missing"] = context.cached_value("nosuchkey")
        context.cache_all({"more": 1})
        seen["entries"] = dict(context.cached_entries())
        return "v"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert seen["has"] is True
    assert seen["value"] == 123
    assert seen["missing"] is None
    assert seen["entries"]["side"] == 123
    assert seen["entries"]["more"] == 1


def test_cached_file_data_revalidates_by_stat(make_tree, backends, script, tmp_path):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    side_path = root / "side.txt"
    side_path.write_text("one", encoding="utf-8")

    def fn(key, options, context):
        return context.cached_file_data(str(side_path))

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "one"

    # Rewrite with a bumped mtime -- a fresh Hiera/provider must see the
    # new content (a within-lookup cache would otherwise freeze the first
    # read, since h.lookup("k") itself is cached per key on the same
    # instance).
    time.sleep(0.05)
    side_path.write_text("two", encoding="utf-8")
    os.utime(side_path, None)
    h2 = Hiera(str(root / "hiera.yaml"))
    assert h2.lookup("k") == "two"


def test_lookup_context_names_and_explain_noop(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    seen = {}

    def boom():
        raise AssertionError("explain's producer must never be called")

    def fn(key, options, context):
        seen["env"] = context.environment_name
        seen["module"] = context.module_name
        context.explain(boom)
        return "v"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(environment="prod"))
    assert h.lookup("k") == "v"
    assert seen["env"] == "prod"
    assert seen["module"] is None


def test_not_found_escapes_except_exception(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )

    def fn(key, options, context):
        try:
            context.not_found()
        except Exception:
            return "swallowed"
        return "unreachable"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert "k" not in h


# --- contracts -----------------------------------------------------------


def test_provider_value_rich_data_validated(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: {True: 1}
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        BackendError, match="has wrong type, expects Puppet::LookupValue"
    ):
        h.lookup("k")


@pytest.mark.parametrize(
    "entry, expected",
    [
        (
            {"lookup_key": "test_data_hash"},
            "'test_data_hash' expects 2 arguments, got 3",
        ),
        (
            {"data_hash": "test_lookup_key"},
            "'test_lookup_key' expects 3 arguments, got 2",
        ),
        (
            {"data_dig": "test_lookup_key"},
            "'test_lookup_key' parameter 'key' expects a String value, got Tuple",
        ),
        (
            {"lookup_key": "test_data_dig"},
            "'test_data_dig' parameter 'key_segments' expects an Array value, got String",
        ),
        (
            {"data_hash": "test_none"},
            "'test_none' implements none of data_hash, lookup_key or data_dig",
        ),
    ],
    ids=["dh-as-lk", "lk-as-dh", "lk-as-dd", "dd-as-lk", "none"],
)
def test_kind_mismatch_uses_puppet_text(make_tree, backends, entry, expected):
    root = make_tree(
        {"hierarchy": [dict({"name": "s", "path": "a.yaml"}, **entry)]},
        files={"data/a.yaml": "x"},
    )
    with pytest.raises(ConfigError, match=re.escape(expected)):
        Hiera(str(root / "hiera.yaml"))


@pytest.mark.parametrize(
    "cls",
    [YAMLBackend, JSONBackend, HOCONBackend, SopsBackend],
    ids=["yaml_data", "json_data", "hocon_data", "sops_data"],
)
def test_file_data_hash_takes_only_path(monkeypatch, cls):
    def boom(*a, **k):
        raise AssertionError("no file read or subprocess before the options check")

    monkeypatch.setattr("builtins.open", boom)
    monkeypatch.setattr("subprocess.run", boom, raising=False)

    backend = cls({}) if cls is not SopsBackend else cls({})
    with pytest.raises(ConfigError, match="one of 'path'"):
        backend.data_hash(None, {})
    with pytest.raises(ConfigError, match="one of 'path'"):
        backend.data_hash("x.yaml", {"foo": "bar"})


def test_lookup_options_through_lookup_key_provider(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"},
            ]
        },
        files={"data/a.yaml": "x"},
    )

    def fn(key, options, context):
        if key == "lookup_options":
            return {"a": {"merge": "unique"}}
        if key == "a":
            return ["x", "y"]
        context.not_found()

    script["handle_lookup_options"] = True
    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    invocation = Invocation(h.scope, h._sub_lookup)
    compiled = h._retrieve_lookup_options(None, invocation)
    assert extract_lookup_options_for_key("a", compiled) == {"merge": "unique"}


def test_function_contexts_are_per_view(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    view = h.scoped(facts={"x": 1})
    assert h.lookup("k") == "v"
    assert view.lookup("k") == "v"
    # Each view builds its own provider/function context -- no cache shared
    # across a scope-binding boundary.
    assert len(calls) == 2


def test_provider_results_are_copied(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: [1, 2, 3]
    h = Hiera(str(root / "hiera.yaml"))
    result = h.lookup("k")
    result.append(4)
    assert h.lookup("k") == [1, 2, 3]
