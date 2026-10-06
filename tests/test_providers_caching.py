"""How long a function provider's results live and what they depend on."""

from hyera import Hiera, Scope
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)


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
