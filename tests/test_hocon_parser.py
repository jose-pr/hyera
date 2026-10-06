"""The private pyhocon parser copy, the include guard and the values and keys
pyhocon returns.
"""

import os
import subprocess
import sys
import types

import pytest

from hyera import BackendError
from hyera.backends import HOCONBackend
from hyera.backends import _hocon as hocon_mod
from hocon_support import (  # noqa: F401
    BS,
)

# direct unit coverage: the include-resolution backstop and the private parser copy
# machinery, which are defense in depth or process-global caching that no HOCON text
# reaches through the public API.


def test_guarded_hocon_classmethod_raises_when_guard_is_set():
    calls = []

    def original(cls, *args, **kwargs):
        calls.append((args, kwargs))
        return "ran for real"

    guarded = hocon_mod._guarded_hocon_classmethod(original, "file include")
    token = hocon_mod._HOCON_INCLUDE_GUARD.set(frozenset({"file include"}))
    try:
        with pytest.raises(BackendError, match="file include"):
            guarded.__func__(object())
    finally:
        hocon_mod._HOCON_INCLUDE_GUARD.reset(token)
    assert calls == []


def test_install_hocon_include_guard_is_idempotent_for_an_explicit_module():
    class _FakeConfigFactory:
        parse_file = classmethod(lambda cls, *a, **kw: None)
        parse_URL = classmethod(lambda cls, *a, **kw: None)

    class _FakeConfigParser:
        resolve_package_path = classmethod(lambda cls, *a, **kw: None)

    class _FakeModule:
        ConfigFactory = _FakeConfigFactory
        ConfigParser = _FakeConfigParser

    mod = _FakeModule()
    hocon_mod._install_hocon_include_guard(mod)
    assert mod._hocon_include_guard_installed is True
    installed = _FakeConfigFactory.__dict__["parse_file"]

    hocon_mod._install_hocon_include_guard(mod)  # second call must be a no-op

    assert _FakeConfigFactory.__dict__["parse_file"] is installed


def test_hocon_parser_returns_module_built_while_waiting_for_the_lock(monkeypatch):
    # Simulates the double-checked-locking race: another thread built the module while
    # this caller waited for the lock, so the inner `_HOCON_PARSER_MODULE is None`
    # check must return early instead of building a second copy.
    sentinel = object()

    class _FakeLock:
        def __enter__(self):
            hocon_mod._HOCON_PARSER_MODULE = sentinel
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(hocon_mod, "_HOCON_PARSER_MODULE", None)
    monkeypatch.setattr(hocon_mod, "_HOCON_PARSER_LOCK", _FakeLock())

    assert hocon_mod._hocon_parser() is sentinel


def test_hocon_parser_requires_get_period_expr(monkeypatch):
    pytest.importorskip("pyhocon")

    class _FakeSpec:
        loader = None

    class _FakeLoader:
        def exec_module(self, module):
            pass  # a pyhocon old enough to lack `get_period_expr` entirely

    fake_spec = _FakeSpec()
    fake_spec.loader = _FakeLoader()
    fake_module = types.ModuleType("pyhocon.config_parser")

    monkeypatch.setattr(hocon_mod, "_HOCON_PARSER_MODULE", None)
    monkeypatch.setattr(hocon_mod.importlib.util, "find_spec", lambda name: fake_spec)
    monkeypatch.setattr(
        hocon_mod.importlib.util, "module_from_spec", lambda spec: fake_module
    )

    with pytest.raises(BackendError, match="pyhocon>=0.3.60"):
        hocon_mod._hocon_parser()


def test_hocon_loads_reraises_a_backend_error_raised_while_parsing(monkeypatch):
    def _boom():
        raise BackendError("boom from parser")

    monkeypatch.setattr(hocon_mod, "_hocon_parser", _boom)

    with pytest.raises(BackendError, match="boom from parser"):
        HOCONBackend().loads("k = v")


# -- values and keys pyhocon would hand back as parser objects or raw text --


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_quoted_key_loses_its_quote_characters(hocon_includes):
    result = HOCONBackend(hocon_includes=hocon_includes).loads(
        '"ntp::servers" = [a, b]\n"a.b" = 1\nq { "in::ner" = 2, "a b" = 3 }\n'
        '"""tq""" = 4\n'
    )
    assert result == {
        "ntp::servers": ["a", "b"],
        "a.b": 1,
        "q": {"in::ner": 2, "a b": 3},
        "tq": 4,
    }


def test_null_in_a_concatenation_is_the_text_null():
    result = HOCONBackend().loads(
        'a = null x\nb = x null\nc = "p" null\nd = null\ne = [null x]\n'
    )
    assert result == {
        "a": "null x",
        "b": "x null",
        "c": "p null",
        "d": None,
        "e": ["null x"],
    }


def test_null_alone_stays_null_and_keeps_only_the_blanks_between_values():
    result = HOCONBackend().loads(
        "a = null  # c\nb = [null, null]\nc { d = null }\ne = x null // c\n"
        'f = null "q"\n'
    )
    assert result["a"] is None
    assert result["b"] == [None, None]
    assert result["c"] == {"d": None}
    assert result["e"] == "x null"
    assert result["f"] == "null q"


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_unicode_escape_in_a_quoted_string_is_decoded(hocon_includes):
    b = HOCONBackend(hocon_includes=hocon_includes)
    assert b.loads('e = "' + BS + 'u00e9"') == {"e": "é"}
    assert b.loads('e = "x' + BS + "u00E9y" + BS + 'n"') == {"e": "xéy\n"}
    assert b.loads('"k' + BS + 'u00e9" = 1') == {"ké": 1}
    # a decoded quote or backslash neither ends nor escapes the string
    assert b.loads('e = "a' + BS + "u0022b" + BS + 'u005cc"') == {"e": 'a"b' + BS + "c"}
    assert b.loads('e = "' + BS + 'u0041" "' + BS + 'u0042"') == {"e": "A B"}


def test_unicode_escape_stays_literal_where_the_text_is_not_an_escape():
    b = HOCONBackend()
    assert b.loads('e = """' + BS + 'u00e9"""') == {"e": BS + "u00e9"}
    assert b.loads('e = "' + BS * 2 + 'u00e9"') == {"e": BS + "u00e9"}
    assert b.loads('e = "' + BS + 'u00zz"') == {"e": BS + "u00zz"}
    assert b.loads('e = "' + BS + 'ud83d"') == {"e": BS + "ud83d"}
    assert b.loads('# "' + BS + 'u00e9"\ne = 1') == {"e": 1}


# -- hyera neither imports nor patches pyhocon until a document is parsed --


def _run_python(code):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": src},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_import_hyera_does_not_import_pyhocon():
    pytest.importorskip("pyhocon")
    out = _run_python("import sys, hyera; print('pyhocon' in sys.modules)")
    assert out == "False"


def test_parsing_leaves_the_shared_pyhocon_module_unpatched():
    pytest.importorskip("pyhocon")
    out = _run_python(
        "from hyera.backends import HOCONBackend\n"
        "HOCONBackend().loads('k = v')\n"
        "import pyhocon.config_parser as cp\n"
        "attrs = [(cp.ConfigFactory, 'parse_file'), (cp.ConfigFactory, 'parse_URL'),\n"
        "         (cp.ConfigParser, 'resolve_package_path')]\n"
        "print([getattr(c.__dict__[a], '__func__', c.__dict__[a]).__module__\n"
        "       for c, a in attrs])\n"
    )
    assert out == str(["pyhocon.config_parser"] * 3)
