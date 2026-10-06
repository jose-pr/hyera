"""``lookup_key``/``data_dig`` function providers: per-location dispatch,
``LookupContext``, hierarchy ``options``, and location-less entries.

In-test backends (``test_lookup_key``/``test_data_dig``/``test_data_hash``/
``test_none``, one method each, registered under ``function``) isolate the
process-global registry per test (``_isolated_registry``) and record every
call in ``calls``; their return/raise behavior for a given test comes from
``script``, a callable the test sets before constructing a ``Hiera``.
"""

import copy
import datetime
import decimal
import os
import re

import pytest

from hyera import (
    BackendError,
    ConfigError,
    Hiera,
    HieraError,
    InterpolationError,
    KeyNotFoundError,
    LookupContext,
    Scope,
)
from hyera._lookup.function_provider import _EnvironmentContext
from hyera._lookup.provider_classes import _FunctionProvider
from hyera._lookup.invocation import Invocation
from hyera._lookup.lookup_adapter import extract_lookup_options_for_key
from hyera._lookup.lookup_options import retrieve_lookup_options
from hyera._lookup.navigation import _MISSING
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


@pytest.mark.parametrize("revalidate, expected_calls", [(True, 2), (False, 1)])
def test_lookup_key_result_lifetime_follows_revalidate(
    make_tree, backends, calls, script, revalidate, expected_calls
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"), revalidate=revalidate)
    assert h.lookup("k") == "v"
    assert h.lookup("k") == "v"
    assert len(calls) == expected_calls


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


def test_data_dig_negative_index_segment_is_a_miss(make_tree, backends, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_dig": "test_data_dig", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_dig"] = lambda key_segments, options, context: "leaf"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("a.-1", default_value="D") == "D"
    assert "a.-1" not in h


@pytest.mark.parametrize("revalidate, expected_calls", [(True, 2), (False, 1)])
def test_data_dig_result_lifetime_follows_revalidate(
    make_tree, backends, calls, script, revalidate, expected_calls
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_dig": "test_data_dig", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_dig"] = lambda key_segments, options, context: "leaf"
    h = Hiera(str(root / "hiera.yaml"), revalidate=revalidate)
    assert h.lookup("a.b") == "leaf"
    assert h.lookup("a.b") == "leaf"
    assert len(calls) == expected_calls


def test_data_dig_missing_location_not_called(make_tree, backends, calls, script):
    # _DataDigProvider's own "location is not None and not location.exist"
    # check (distinct from _LookupKeyProvider's copy, covered by
    # test_lookup_key_missing_location_not_called above).
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "data_dig": "test_data_dig",
                    "paths": ["missing.yaml", "b.yaml"],
                }
            ]
        },
        files={"data/b.yaml": "x"},
    )
    script["data_dig"] = lambda key_segments, options, context: "found"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "found"
    assert len(calls) == 1
    assert calls[0][2]["path"].endswith("b.yaml")


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


def test_options_nested_data_value_reaches_the_provider(
    make_tree, backends, calls, script
):
    # A Data value can nest scalars inside a list inside a hash -- every
    # branch of the hiera.yaml schema's own _is_data recursion, none of
    # them raising.
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "path": "a.yaml",
                    "options": {"nested": {"a": 1, "b": [1, 2, None, True]}},
                }
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert calls[0][2]["nested"] == {"a": 1, "b": [1, 2, None, True]}


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

    def fn(key, options, context: LookupContext):
        assert isinstance(context, LookupContext)
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
    h = Hiera(str(root / "hiera.yaml"), revalidate=True)
    assert h.lookup("k") == "one"

    # Same size, newer mtime: only the stat stamp tells the two apart.
    side_path.write_text("two", encoding="utf-8")
    stat = side_path.stat()
    os.utime(side_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert h.lookup("k") == "two"


def test_cached_file_data_missing_file_is_backend_error(
    make_tree, backends, script, tmp_path
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    missing = tmp_path / "nosuchfile.txt"

    def fn(key, options, context):
        return context.cached_file_data(str(missing))

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="Unable to read"):
        h.lookup("k")


def test_cached_file_data_directory_is_backend_error_like_missing_file(
    make_tree, backends, script, tmp_path
):
    # The stat-based staleness check above already wraps its own OSError
    # (a missing file); the open()/read() that follows it must wrap one the
    # same way instead of letting it escape raw -- a directory sitting
    # where a data file is expected is the easiest OSError to provoke here
    # (IsADirectoryError on open(), PermissionError on Windows).
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    a_directory = tmp_path / "a_directory"
    a_directory.mkdir()

    def fn(key, options, context):
        return context.cached_file_data(str(a_directory))

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="Unable to read"):
        h.lookup("k")


def test_cached_file_data_bad_utf8_is_backend_error(
    make_tree, backends, script, tmp_path
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    bad_path = tmp_path / "bad.bin"
    bad_path.write_bytes(b"\xff\xfe not utf-8")

    def fn(key, options, context):
        return context.cached_file_data(str(bad_path))

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="Unable to parse"):
        h.lookup("k")


def test_cached_file_data_parse_backend_error_wrapped_or_reraised(
    make_tree, backends, script, tmp_path
):
    # A parse callback's own BackendError with no `path` set is wrapped
    # ("Unable to parse (<path>): ..."); one that already names a path is
    # re-raised as-is (it already knows where it came from).
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    side_path = tmp_path / "side.txt"
    side_path.write_text("x", encoding="utf-8")

    def parse_unwrapped(text):
        raise BackendError("boom, no path")

    def fn(key, options, context):
        return context.cached_file_data(str(side_path), parse=parse_unwrapped)

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="Unable to parse.*boom, no path"):
        h.lookup("k")

    def parse_with_path(text):
        raise BackendError("boom, own path", path="elsewhere")

    def fn2(key, options, context):
        return context.cached_file_data(str(side_path), parse=parse_with_path)

    script["lookup_key"] = fn2
    h2 = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="^boom, own path$"):
        h2.lookup("k")


def test_cached_file_data_parses_an_unchanged_file_once(
    make_tree, backends, script, tmp_path
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    side_path = tmp_path / "side.txt"
    side_path.write_text("one", encoding="utf-8")
    parsed = []

    def parse(text):
        parsed.append(text)
        return text.upper()

    def fn(key, options, context):
        first = context.cached_file_data(str(side_path), parse)
        second = context.cached_file_data(str(side_path), parse)
        assert first == second == "ONE"
        return "v"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert h.lookup("other") == "v"
    assert parsed == ["one"]


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


def test_lookup_context_module_name_in_a_module_layer(
    tmp_path, make_tree, backends, script
):
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "g: 1\n"},
    )
    modules = tmp_path / "modules"
    for name in ("m", "other"):
        config = modules / name / "hiera.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "version: 5\nhierarchy:\n  - {name: c, lookup_key: test_lookup_key}\n",
            encoding="utf-8",
        )
    script["lookup_key"] = lambda key, options, context: context.module_name
    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    assert h.lookup("m::k") == "m"
    assert h.lookup("other::k") == "other"


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


def test_provider_value_rich_data_validated_no_location(make_tree, backends, script):
    # A location-less lookup_key entry: the "when using location '...'"
    # clause is omitted entirely from the message, not rendered empty.
    root = make_tree(
        {"hierarchy": [{"name": "s", "lookup_key": "test_lookup_key"}]},
    )
    script["lookup_key"] = lambda key, options, context: {True: 1}
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as exc:
        h.lookup("k")
    assert "when using location" not in str(exc.value)
    assert "has wrong type, expects Puppet::LookupValue" in str(exc.value)


@pytest.mark.parametrize(
    "value",
    [datetime.date(2026, 1, 2), decimal.Decimal("1.5"), b"bytes", {1, 2}],
    ids=["date", "decimal", "bytes", "set"],
)
@pytest.mark.parametrize("kind", ["lookup_key", "data_dig"])
def test_provider_value_of_an_unknown_python_type_is_a_backend_error(
    make_tree, backends, script, kind, value
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "path": "a.yaml", kind: "test_" + kind},
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script[kind] = lambda *args: value
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="function 'test_" + kind + "'.*got "):
        h.lookup("k")


@pytest.mark.parametrize(
    "value",
    [datetime.date(2026, 1, 2), decimal.Decimal("1.5"), b"bytes", {1, 2}],
    ids=["date", "decimal", "bytes", "set"],
)
def test_data_hash_value_of_an_unknown_python_type_names_function_and_key(
    make_tree, backends, script, value
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: {"k": value}
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(HieraError, match="key 'k'.*function 'test_data_hash'"):
        h.lookup("k")


def test_a_tuple_from_a_data_hash_hook_reads_as_a_list(make_tree, backends, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: {"k": ("a", ("b",))}
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == ["a", ["b"]]
    assert h.lookup("k.0") == "a"


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig"])
def test_an_exception_a_hook_raises_propagates_unchanged(
    make_tree, backends, script, kind
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml", kind: "test_" + kind}]},
        files={"data/a.yaml": "x"},
    )

    def hook(*args):
        raise ValueError("from the hook")

    script[kind] = hook
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ValueError, match="from the hook"):
        h.lookup("k")


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig"])
def test_a_tuple_from_a_hook_reads_as_a_list(make_tree, backends, script, kind):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "path": "a.yaml", kind: "test_" + kind},
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script[kind] = lambda *args: {"k": ("a", ("b", "c"))}
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == {"k": ["a", ["b", "c"]]}
    assert h.lookup("k", merge="deep") == {"k": ["a", ["b", "c"]]}


def test_a_tuple_from_a_lookup_key_hook_is_navigable_and_merges(
    make_tree, backends, script
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml", "lookup_key": "test_lookup_key"},
                {"name": "b", "path": "b.yaml", "lookup_key": "test_lookup_key"},
            ]
        },
        files={"data/a.yaml": "x", "data/b.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: (
        ("a", "b") if options["path"].endswith("a.yaml") else ("c",)
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("tup") == ["a", "b"]
    assert h.lookup("tup.0") == "a"
    assert h.lookup("tup", merge="unique") == ["a", "b", "c"]


def test_data_hash_non_dict_return_is_backend_error(make_tree, backends, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: ["not", "a", "hash"]
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        BackendError,
        match=(
            "Value returned from data_hash function 'test_data_hash', when "
            "using location '.*a\\.yaml', has wrong type, expects a Hash "
            # A non-empty list infers as Tuple (Array is the empty-list case).
            "value, got Tuple"
        ),
    ):
        h.lookup("k")


def test_data_hash_path_based_type_label_fallback(make_tree, backends, script):
    # The path-based non-dict check falls back to the plain Python type
    # name for a value that is not any of Puppet's own JSON-ish shapes.
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: object()
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="got object$"):
        h.lookup("k")


def test_data_hash_no_location_non_dict_return_is_backend_error(
    make_tree, backends, calls, script
):
    # A location-less data_hash entry calls the backend directly and
    # validates/caches the raw result itself (no path to name in the error).
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash"}]},
    )
    script["data_hash"] = lambda path, options: ["not", "a", "hash"]
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as exc:
        h.lookup("k")
    assert "when using location" not in str(exc.value)
    assert (
        "Value returned from data_hash function 'test_data_hash' has wrong "
        "type, expects a Hash value, got Tuple" in str(exc.value)
    )
    assert calls[0][1] is None


@pytest.mark.parametrize(
    "value, label",
    [
        (True, "Boolean"),
        (None, "Undef"),
        ("x", "String"),
        (5, "Integer"),
        (5.0, "Float"),
        ([], "Array"),
        (object(), "object"),
    ],
    ids=["bool", "none", "str", "int", "float", "empty-array", "other"],
)
def test_data_hash_no_location_type_label_table(
    make_tree, backends, script, value, label
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash"}]},
    )
    script["data_hash"] = lambda path, options: value
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match=re.escape("got " + label)):
        h.lookup("k")


def test_data_hash_no_location_called_once_and_cached(
    make_tree, backends, calls, script
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash"}]},
    )
    script["data_hash"] = lambda path, options: {"k": "v"}
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert h.lookup("k") == "v"
    assert len(calls) == 1


def test_data_hash_uri_non_dict_return_is_backend_error_with_location(
    make_tree, backends, script
):
    # A uri location takes the *other* branch of _validate_data_hash's own
    # location-is-None check (unlike the plain location-less case above):
    # `label` is the uri itself, so the message names it.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "data_hash": "test_data_hash", "uri": "x:custom"}
            ]
        },
    )
    script["data_hash"] = lambda path, options: ["not", "a", "hash"]
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        BackendError,
        match=re.escape(
            "Value returned from data_hash function 'test_data_hash', when "
            "using location 'x:custom', has wrong type, expects a Hash "
            "value, got Tuple"
        ),
    ):
        h.lookup("k")


def test_function_provider_key_lookup_is_abstract():
    # _FunctionProvider.key_lookup is never called directly in production
    # (every PROVIDER_CLASSES entry is a concrete subclass overriding it);
    # exercised by direct construction/call.
    provider = _FunctionProvider("n", None, {}, None, _EnvironmentContext(), "env")
    with pytest.raises(NotImplementedError):
        provider.key_lookup("root", [], None, None)


def test_data_hash_load_file_missing_is_not_found(make_tree, monkeypatch):
    # load_file returning _MISSING (a previously-cached file vanishing
    # under revalidation) makes this location a miss, not an error.
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml"}]},
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    monkeypatch.setattr(
        h._store,
        "load_file",
        lambda path, backend, options, invocation=None: _MISSING,
    )
    with pytest.raises(KeyNotFoundError):
        h.lookup("k")


def test_data_hash_module_layer_prune_is_applied(tmp_path, make_tree, backends, script):
    # `_prune` is set only for a module-owned level (core.Hiera._build_
    # provider); a location-less data_hash entry in a module's own
    # hiera.yaml is the only way to reach the "no location, or a uri"
    # branch's own prune call.
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "g: 1\n"},
    )
    modules = tmp_path / "modules"
    mod_config = modules / "m" / "hiera.yaml"
    mod_config.parent.mkdir(parents=True)
    mod_config.write_text(
        "version: 5\nhierarchy:\n  - {name: c, data_hash: test_data_hash}\n",
        encoding="utf-8",
    )
    script["data_hash"] = lambda path, options: {"m::k": "v", "unqualified": "dropped"}
    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    assert h.lookup("m::k") == "v"
    with pytest.raises(KeyNotFoundError):
        h.lookup("unqualified")


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
    # Lazy: the mismatch is only ever raised once the function is actually
    # invoked for a location that exists, not at construction.
    root = make_tree(
        {"hierarchy": [dict({"name": "s", "path": "a.yaml"}, **entry)]},
        files={"data/a.yaml": "x"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ConfigError, match=re.escape(expected)):
        h.lookup("x")


def test_kind_mismatch_is_not_raised_for_a_missing_location(make_tree, backends):
    # A kind-mismatched level whose own location does not exist must not
    # refuse the whole Hiera instance -- Puppet degrades gracefully and
    # still resolves every key a correctly-configured level can answer.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "bad", "path": "missing.yaml", "lookup_key": "test_data_hash"},
                {"name": "good", "path": "a.yaml"},
            ]
        },
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"


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
    compiled = retrieve_lookup_options(h, None, invocation)
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


def _lookup_key_tree(make_tree, extra_levels=()):
    return make_tree(
        {
            "hierarchy": list(extra_levels)
            + [{"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}]
        },
        files={
            "data/a.yaml": "x",
            "data/common.yaml": "twice: \"%{lookup('k')}-%{lookup('k')}\"\n",
        },
    )


def test_lookup_key_result_without_a_file_read_lives_one_lookup(
    make_tree, backends, calls, script
):
    root = _lookup_key_tree(make_tree, [{"name": "c", "path": "common.yaml"}])
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("twice") == "v-v"
    assert [c[1] for c in calls] == ["k"]
    assert h.lookup("k") == "v"
    assert [c[1] for c in calls] == ["k", "k"]


def test_lookup_key_results_are_kept_until_clear_cache_without_revalidation(
    make_tree, backends, calls, script
):
    root = _lookup_key_tree(make_tree)
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"), revalidate=False)
    assert h.lookup("k") == "v"
    assert h.lookup("k") == "v"
    assert len(calls) == 1
    h.clear_cache()
    assert h.lookup("k") == "v"
    assert len(calls) == 2


def test_lookup_key_result_follows_the_files_the_hook_read(
    make_tree, backends, calls, script
):
    root = _lookup_key_tree(make_tree)
    side = root / "side.txt"
    side.write_text("one", encoding="utf-8")
    script["lookup_key"] = lambda key, options, context: context.cached_file_data(
        str(side)
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "one"
    assert h.lookup("k") == "one"
    assert len(calls) == 1

    side.write_text("two-longer", encoding="utf-8")
    assert h.lookup("k") == "two-longer"
    assert len(calls) == 2
    assert h.lookup("k") == "two-longer"
    assert len(calls) == 2


def test_data_dig_result_follows_the_files_the_hook_read(
    make_tree, backends, calls, script
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_dig": "test_data_dig", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    side = root / "side.txt"
    side.write_text("one", encoding="utf-8")
    script["data_dig"] = (
        lambda key_segments, options, context: context.cached_file_data(str(side))
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("a.b") == "one"
    assert h.lookup("a.b") == "one"
    assert len(calls) == 1
    side.write_text("two-longer", encoding="utf-8")
    assert h.lookup("a.b") == "two-longer"


def test_hook_cache_and_engine_results_are_separate(make_tree, backends, script):
    root = _lookup_key_tree(make_tree)
    seen = {}
    raw = {"greeting": "hello %{who}", "farewell": "bye %{who}"}

    def fn(key, options, context):
        if not context.cache_has_key("__loaded__"):
            context.cache_all(raw)
            context.cache("__loaded__", True)
        seen["entries"] = sorted(map(str, dict(context.cached_entries())))
        if not context.cache_has_key(key):
            context.not_found()
        return context.interpolate(context.cached_value(key))

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"who": "world"}))
    assert h.lookup("greeting") == "hello world"
    assert h.lookup("farewell") == "bye world"
    assert seen["entries"] == ["__loaded__", "farewell", "greeting"]


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


# --- uri/uris locations --------------------------------------------------


def test_uri_reaches_provider_as_option(make_tree, backends, calls, script):
    # No file named after this uri exists anywhere: the provider is still
    # called (proving the location is never fetched or checked for
    # existence -- `location.exist` is always True for a uri).
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "uri": "http://h.invalid/%{facts.os.family}",
                }
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    assert h.lookup("k") == "v"
    assert calls[0][2] == {"uri": "http://h.invalid/RedHat"}


def test_lookup_key_receives_uri(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "uri": "mailto:a@b"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert calls[0][2]["uri"] == "mailto:a@b"


def test_uris_walked_in_order(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "uris": ["mailto:a@b", "mailto:c@d"],
                }
            ]
        },
    )

    def fn(key, options, context):
        if options["uri"] == "mailto:a@b":
            context.not_found()
        return "found-second"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "found-second"
    assert [c[2]["uri"] for c in calls] == ["mailto:a@b", "mailto:c@d"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTP://X:8080/p?q=A B#f", "http://X:8080/p?q=A%20B#f"),
        ("http://x:80/a", "http://x/a"),
        ("https://x:443", "https://x"),
        ("http://x:/", "http://x/"),
        ("Foo+Bar://h/", "foo+bar://h/"),
    ],
)
def test_ruby_uri_to_s(raw, expected):
    from hyera._config.location_resolver import _ruby_uri

    assert _ruby_uri(raw) == expected


@pytest.mark.parametrize("raw", ["a b", "http://a%2", "http://u@h/p#f g"])
def test_bad_uri_raises_config_error(raw):
    from hyera._config.location_resolver import _ruby_uri

    with pytest.raises(
        ConfigError, match=re.escape('bad URI (is not URI?): "{}"'.format(raw))
    ):
        _ruby_uri(raw)


def test_uri_undefined_variable_is_lenient(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "uri": "%{nosuch}"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert calls[0][2]["uri"] == ""


def test_uri_method_syntax_rejected(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "uri": "%{lookup('k')}"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ConfigError, match="Interpolation using method syntax"):
        h.lookup("k")
    assert calls == []


def test_sources_exclude_uri_locations(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "u", "lookup_key": "test_lookup_key", "uri": "mailto:a@b"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert all("mailto" not in str(s) for s in sources)
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_exclude_a_data_hash_uri_location(make_tree, backends, script):
    # The built-in yaml/json/hocon/sops data_hash backends all reject a
    # uri location outright (ConfigError, before _files_for ever sees it),
    # but a third-party data_hash backend's own data_hash() is free to
    # accept one -- _files_for's own loc.is_uri check still has to exclude
    # it from sources() either way, same as test_sources_exclude_uri_
    # locations above does for a lookup_key uri (which never reaches this
    # check at all, being filtered out earlier by its own kind).
    root = make_tree(
        {
            "hierarchy": [
                {"name": "u", "data_hash": "test_data_hash", "uri": "mailto:a@b"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    script["data_hash"] = lambda path, options: {}
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert all("mailto" not in str(s) for s in sources)
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_exclude_a_nonexistent_location(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "missing", "path": "missing.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert all("missing.yaml" not in str(s) for s in sources)
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_skips_a_location_less_data_hash_level(make_tree):
    # A data_hash entry with no location key at all (calls its function
    # once, with no location, same as a location-less lookup_key/data_dig
    # entry) has provider.locations is None -- _files_for must skip it
    # outright rather than trying to iterate None as if it were a list of
    # resolved locations.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "nolock", "data_hash": "yaml_data"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_skips_a_file_that_vanishes_between_its_own_exist_check_and_load(
    make_tree, monkeypatch
):
    # _files_for resolves a level's own location (exist=True, a fresh
    # probe) and loads it immediately after, one level at a time -- unlike
    # the main lookup pipeline, nothing else of this same call runs in
    # between for a single-location level, so the only way the file can
    # still vanish between those two probes is something outside Hiera's
    # own code entirely (another process, here simulated by making the
    # exist-check's own probe delete the file as a side effect, the
    # instant after it observes "still there"). load_file's fresh probe
    # then correctly finds it gone despite the cached entry from an
    # earlier, successful sources() call, and this level contributes
    # nothing to the result instead of a stale or crashing entry.
    from hyera._lookup import locations

    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml"}]},
        files={"data/a.yaml": "k: v\n"},
    )
    path = root / "data" / "a.yaml"

    h = Hiera(str(root / "hiera.yaml"))
    first = h.sources()
    assert any(str(s).endswith("a.yaml") for s in first)

    real_probe = locations._probe
    armed = []

    def vanishing_probe(p):
        result = real_probe(p)
        if armed and str(p) == str(path) and result.kind == "file":
            path.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(locations, "_probe", vanishing_probe)
    armed.append(True)

    second = h.sources()
    assert second == ()


# --- eyaml: missing optional dependency -----------------------------------


def test_eyaml_missing_cryptography_hint(make_tree, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "cryptography", None)
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "secrets",
                    "lookup_key": "eyaml_lookup_key",
                    "path": "secrets.eyaml",
                    "options": {"pkcs7_private_key": "keys/private_key.pkcs7.pem"},
                }
            ]
        },
        files={"data/secrets.eyaml": "k: v\n"},
    )
    with pytest.raises(BackendError, match=r"hyera\[eyaml\]"):
        Hiera(str(root / "hiera.yaml"))
