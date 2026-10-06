"""The ``LookupContext`` handed to function providers."""

import os

import pytest

from hyera import BackendError, Hiera, LookupContext, Scope
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)

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
