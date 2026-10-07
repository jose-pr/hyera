"""``lookup_key``/``data_dig`` providers: per-location dispatch, locations and
options.
"""

import pytest

from hyera import ConfigError, Hiera, InterpolationError, Scope
from hyera._lookup.invocation import Invocation
from hyera._lookup.lookup_adapter import extract_lookup_options_for_key
from hyera._lookup.lookup_options import retrieve_lookup_options
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)


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
    # branch of the hiera.yaml schema's Data type, none of them raising.
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
