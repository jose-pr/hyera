"""Backends installed by other distributions register through the entry-point
group ``hyera.backends``, loaded lazily and once."""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import os
import subprocess
import sys
import textwrap
import threading
import types

import pytest

from hyera.backends import Backend, _entry_points, default_backends

GROUP = "hyera.backends"


class _Entry:
    """What ``importlib.metadata.EntryPoint`` offers a loader."""

    def __init__(self, name, load, dist="plugin-dist"):
        self.name = name
        self.group = GROUP
        self.value = "plugin:" + name
        self._load = load
        self.loads = 0
        self.dist = types.SimpleNamespace(name=dist)

    def load(self):
        self.loads += 1
        return self._load()


@pytest.fixture
def fresh(monkeypatch):
    """A registry copy and a loader that has not run yet."""
    import copy

    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))
    monkeypatch.setattr(_entry_points, "_STARTED", False)
    monkeypatch.setattr(_entry_points, "_DONE", False)


def _select_form(entries):
    class Found(list):
        def select(self, **kw):
            assert kw == {"group": GROUP}
            return list(self)

    return lambda: Found(entries)


def _mapping_form(entries):
    return lambda: {GROUP: list(entries), "other.group": [object()]}


def _define(name):
    def load():
        return type(
            name.title() + "Backend",
            (Backend,),
            {"NAMES": {"function": (name,)}, "lookup_key": lambda s, k, o, c: 1},
        )

    return load


@pytest.mark.parametrize(
    "form", [_select_form, _mapping_form], ids=["select", "mapping"]
)
def test_an_installed_backend_is_registered_on_first_use(fresh, monkeypatch, form):
    entry = _Entry("ep_plugin_a", _define("ep_plugin_a"))
    monkeypatch.setattr(importlib.metadata, "entry_points", form([entry]))
    assert Backend.find("ep_plugin_a") is not None
    assert entry.loads == 1


@pytest.mark.parametrize(
    "ask",
    [
        lambda: Backend.find("nothing"),
        lambda: Backend.names(),
        lambda: Backend.for_path("x.nothing"),
        lambda: default_backends(),
        lambda: Backend.get("yaml_data"),
        lambda: Backend.new("yaml_data"),
    ],
    ids=["find", "names", "for_path", "default_backends", "get", "new"],
)
def test_every_registry_question_loads_the_entries(fresh, monkeypatch, ask):
    entry = _Entry("ep_plugin_b", _define("ep_plugin_b"))
    monkeypatch.setattr(importlib.metadata, "entry_points", _select_form([entry]))
    ask()
    assert entry.loads == 1


def test_entries_load_once(fresh, monkeypatch):
    entry = _Entry("ep_plugin_c", _define("ep_plugin_c"))
    monkeypatch.setattr(importlib.metadata, "entry_points", _select_form([entry]))
    Backend.names()
    Backend.names()
    default_backends()
    assert entry.loads == 1
    assert "ep_plugin_c" in Backend.names()
    assert any(c.__name__ == "Ep_Plugin_CBackend" for c in default_backends())


def test_a_failing_entry_is_logged_once_and_skipped(fresh, monkeypatch, caplog):
    def broken():
        raise ImportError("secret path /home/x")

    bad = _Entry("ep_bad", broken, dist="bad-dist")
    good = _Entry("ep_good", _define("ep_good"))
    monkeypatch.setattr(importlib.metadata, "entry_points", _select_form([bad, good]))
    with caplog.at_level(logging.WARNING):
        assert Backend.find("ep_good") is not None
        Backend.names()
    records = [r for r in caplog.records if "ep_bad" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "bad-dist" in message and "ImportError" in message
    assert "secret path" not in message
    assert records[0].levelno == logging.WARNING
    assert bad.loads == 1


def test_a_broken_metadata_scan_is_logged_and_the_built_ins_still_work(
    fresh, monkeypatch, caplog
):
    def scan():
        raise OSError("scan failed")

    monkeypatch.setattr(importlib.metadata, "entry_points", scan)
    with caplog.at_level(logging.WARNING):
        assert Backend.find("yaml_data") is not None
    assert any("OSError" in r.getMessage() for r in caplog.records)


def test_several_threads_load_each_entry_once(fresh, monkeypatch):
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return _define("ep_threaded")()

    entry = _Entry("ep_threaded", slow)
    monkeypatch.setattr(importlib.metadata, "entry_points", _select_form([entry]))
    results = []

    def ask():
        results.append(Backend.find("ep_threaded"))

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert entry.loads == 1
    assert len(results) == 8 and all(r is results[0] for r in results)
    assert results[0] is not None


def test_a_plugin_that_asks_the_registry_while_loading_does_not_deadlock(
    fresh, monkeypatch
):
    seen = []

    def load():
        seen.append(Backend.find("yaml_data"))
        return _define("ep_reentrant")()

    entry = _Entry("ep_reentrant", load)
    monkeypatch.setattr(importlib.metadata, "entry_points", _select_form([entry]))
    assert Backend.find("ep_reentrant") is not None
    assert seen and seen[0] is not None


def test_a_name_registered_twice_is_an_error_naming_both_classes(fresh, monkeypatch):
    class First(Backend):
        NAMES = {"function": ("ep_dup",)}

    with pytest.raises(ValueError, match=r"ep_dup.*First.*Second"):

        class Second(Backend):
            NAMES = {"function": ("ep_dup",)}


def test_a_duplicate_name_from_an_entry_skips_that_entry(fresh, monkeypatch, caplog):
    def clash():
        class Clash(Backend):
            NAMES = {"function": ("yaml_data",)}

    entry = _Entry("ep_clash", clash)
    monkeypatch.setattr(importlib.metadata, "entry_points", _select_form([entry]))
    with caplog.at_level(logging.WARNING):
        assert Backend.find("yaml_data").__name__ == "YAMLBackend"
    assert any("ep_clash" in r.getMessage() for r in caplog.records)


def _run(code, extra_path=None):
    env = dict(os.environ)
    paths = [os.path.join(os.path.dirname(__file__), "..", "src")]
    if extra_path is not None:
        paths.insert(0, str(extra_path))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_importing_hyera_does_not_scan_entry_points():
    out = _run("""
        import importlib.metadata as m
        calls = []
        real = m.entry_points
        m.entry_points = lambda *a, **k: calls.append(1) or real(*a, **k)
        import hyera, hyera.backends, hyera.core
        assert not calls, "scanned at import"
        hyera.Backend.names()
        assert len(calls) == 1, calls
        hyera.Backend.names()
        assert len(calls) == 1, calls
        """)
    assert out.returncode == 0, out.stderr


def test_an_installed_distribution_registers_its_backend(tmp_path):
    site = tmp_path / "site"
    dist = site / "hyera_demo_plugin-1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_bytes(
        b"Metadata-Version: 2.1\nName: hyera-demo-plugin\nVersion: 1.0\n"
    )
    (dist / "entry_points.txt").write_bytes(
        b"[hyera.backends]\ndemo = hyera_demo_plugin\n"
    )
    (site / "hyera_demo_plugin.py").write_bytes(textwrap.dedent("""
            from hyera.backends import Backend


            class DemoBackend(Backend):
                NAMES = {"function": ("demo_lookup",)}

                def lookup_key(self, key, options, context):
                    if key == "greeting":
                        return "hello from " + str(options.get("who", "nobody"))
                    context.not_found()
            """).encode("utf-8"))
    (tmp_path / "hiera.yaml").write_bytes(textwrap.dedent("""
            version: 5
            hierarchy:
              - name: demo
                lookup_key: demo_lookup
                options:
                  who: the plugin
            """).encode("utf-8"))
    out = _run(
        """
        import sys
        from hyera import Hiera
        h = Hiera(sys.argv[1])
        print(h.lookup("greeting"))
        """.replace("sys.argv[1]", repr(str(tmp_path / "hiera.yaml"))),
        extra_path=site,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "hello from the plugin"
