"""A ``data_hash`` hook receives a ``LookupContext`` like the other two hooks."""

from __future__ import annotations

import inspect

import pytest

from hyera import BackendError, Hiera, KeyNotFoundError, LookupContext, Scope
from hyera.backends import Backend, default_backends

from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)


class _Capture:
    """Collects what a ``data_hash`` hook was handed."""

    def __init__(self):
        self.contexts = []


def _hiera(make_tree, hierarchy, scope=None, **kw):
    root = make_tree({"hierarchy": hierarchy}, files={})
    return Hiera(str(root / "hiera.yaml"), scope=scope, **kw)


def test_a_data_hash_hook_receives_a_lookup_context_for_a_uri_location(
    make_tree, monkeypatch
):
    seen = _Capture()

    class Hook(Backend):
        NAMES = {"function": ("ctx_uri_hook",)}

        def data_hash(self, path, options, context):
            seen.contexts.append(context)
            return {"k": "v"}

    h = _hiera(
        make_tree,
        [{"name": "u", "data_hash": "ctx_uri_hook", "uri": "https://x/y"}],
        backends=list(default_backends()) + [Hook],
    )
    assert h.lookup("k") == "v"
    assert isinstance(seen.contexts[0], LookupContext)


def test_environment_name_and_interpolate_work_inside_data_hash(make_tree):
    seen = []

    class Hook(Backend):
        NAMES = {"function": ("ctx_interp_hook",)}

        def data_hash(self, path, options, context):
            seen.append(context.environment_name)
            return {"k": context.interpolate("on %{facts.host}")}

    h = _hiera(
        make_tree,
        [{"name": "n", "data_hash": "ctx_interp_hook"}],
        scope=Scope(facts={"host": "web1"}, environment="staging"),
        backends=list(default_backends()) + [Hook],
    )
    assert h.lookup("k") == "on web1"
    assert seen == ["staging"]


def test_module_name_is_the_module_of_a_module_layer_level(tmp_path, make_tree):
    seen = []

    class Hook(Backend):
        NAMES = {"function": ("ctx_module_hook",)}

        def data_hash(self, path, options, context):
            seen.append(context.module_name)
            return {"m::k": "v"}

    base = make_tree({"hierarchy": []}, files={})
    mod = tmp_path / "modules" / "m" / "hiera.yaml"
    mod.parent.mkdir(parents=True)
    mod.write_text(
        "version: 5\nhierarchy:\n  - {name: c, data_hash: ctx_module_hook}\n",
        encoding="utf-8",
    )
    h = Hiera(
        str(base / "hiera.yaml"),
        backends=list(default_backends()) + [Hook],
        basemodulepath=[tmp_path / "modules"],
    )
    assert h.lookup("m::k") == "v"
    assert seen == ["m"]


def test_cache_and_cached_file_data_work_inside_data_hash(make_tree, tmp_path):
    source = tmp_path / "side.txt"
    source.write_bytes(b"x=1\n")
    parses = []

    class Hook(Backend):
        NAMES = {"function": ("ctx_cache_hook",)}

        def data_hash(self, path, options, context):
            def parse(text):
                parses.append(text)
                return text.strip()

            side = context.cached_file_data(source, parse)
            context.cache("stored", side)
            return {"k": side}

    h = _hiera(
        make_tree,
        [{"name": "n", "data_hash": "ctx_cache_hook"}],
        backends=list(default_backends()) + [Hook],
    )
    assert h.lookup("k") == "x=1"
    assert h.lookup("k") == "x=1"
    assert parses == ["x=1\n"]


def test_a_file_location_data_hash_hook_receives_a_context_too(make_tree):
    seen = []

    class Hook(Backend):
        NAMES = {"function": ("ctx_file_hook",)}

        def data_hash(self, path, options, context):
            seen.append((path, type(context)))
            return {"k": "v"}

    root = make_tree(
        {"hierarchy": [{"name": "f", "data_hash": "ctx_file_hook", "path": "a.yaml"}]},
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), backends=list(default_backends()) + [Hook])
    assert h.lookup("k") == "v"
    assert seen[0][1] is LookupContext
    assert list(h.sources()) == [seen[0][0]]


def test_sources_gives_a_data_hash_hook_a_context_that_resolves_variables(
    make_tree,
):
    seen = []

    class Hook(Backend):
        NAMES = {"function": ("ctx_sources_hook",)}

        def data_hash(self, path, options, context):
            seen.append(context.interpolate("%{facts.host}"))
            return {}

    root = make_tree(
        {
            "hierarchy": [
                {"name": "f", "data_hash": "ctx_sources_hook", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(facts={"host": "web1"}),
        backends=list(default_backends()) + [Hook],
    )
    h.sources()
    assert seen == ["web1"]


def test_calling_not_found_inside_data_hash_is_an_error(make_tree):
    class Hook(Backend):
        NAMES = {"function": ("ctx_not_found_hook",)}

        def data_hash(self, path, options, context):
            context.not_found()

    h = _hiera(
        make_tree,
        [{"name": "n", "data_hash": "ctx_not_found_hook"}],
        backends=list(default_backends()) + [Hook],
    )
    with pytest.raises(BackendError, match="not_found") as excinfo:
        h.lookup("k")
    assert not isinstance(excinfo.value, KeyNotFoundError)


def test_a_two_parameter_data_hash_fails_with_pythons_own_type_error(make_tree):
    class Old(Backend):
        NAMES = {"function": ("ctx_old_hook",)}

        def data_hash(self, path, options):
            return {"k": "v"}

    h = _hiera(
        make_tree,
        [{"name": "n", "data_hash": "ctx_old_hook"}],
        backends=list(default_backends()) + [Old],
    )
    with pytest.raises(TypeError, match="positional argument"):
        h.lookup("k")


@pytest.mark.parametrize("backend", default_backends(), ids=lambda c: c.__name__)
def test_every_built_in_backend_takes_the_third_parameter(backend):
    params = list(inspect.signature(backend.data_hash).parameters)
    assert params == ["self", "path", "options", "context"]


def test_the_base_hook_takes_a_context():
    params = list(inspect.signature(Backend.data_hash).parameters)
    assert params == ["self", "path", "options", "context"]
