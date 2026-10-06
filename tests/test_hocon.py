"""HOCON ``include`` directives resolve as Puppet's ``hocon_data`` does by default
and are refused under ``hocon_includes=False``; no form reaches pyhocon's own include machinery.
"""

import importlib
import io
import sys

import pytest

from hyera import BackendError, Hiera, default_backends
from hyera.backends import HOCONBackend, has_hocon
from hocon_support import (  # noqa: F401
    http_server,
    pyhocon_tripwire,
)


def _file_url(posix_path: str) -> str:
    return "file:///" + posix_path.lstrip("/")


# -- plain quoted include: contributes nothing, in either mode -------------


@pytest.mark.parametrize("kind", ["relative", "absolute", "file-url"])
@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_plain_include_contributes_nothing(
    kind, hocon_includes, tmp_path, monkeypatch, pyhocon_tripwire
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromcwd = yes\n")
    abs_conf = tmp_path / "abs.conf"
    abs_conf.write_bytes(b"fromabs = yes\n")
    abs_posix = abs_conf.resolve().as_posix()

    targets = {
        "relative": "inc.conf",
        "absolute": abs_posix,
        "file-url": _file_url(abs_posix),
    }
    content = 'include "{}"\nplain = p\n'.format(targets[kind])

    result = HOCONBackend(hocon_includes=hocon_includes).loads(content)

    assert result == {"plain": "p"}
    assert pyhocon_tripwire == []


# -- forms that always raise, in either mode --------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        "url-http",
        "url-file",
        "plain-http-url",
        "classpath",
        "required",
        "package",
        "space-before-paren",
        "uppercase-keyword",
        "bare-keyword",
    ],
)
@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_include_form_always_raises(
    kind, hocon_includes, tmp_path, monkeypatch, pyhocon_tripwire, http_server
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromcwd = yes\n")
    abs_conf = tmp_path / "abs.conf"
    abs_conf.write_bytes(b"fromabs = yes\n")
    abs_posix = abs_conf.resolve().as_posix()
    server, hits = http_server
    base_url = "http://{}:{}".format(*server.server_address)

    contents = {
        "url-http": 'include url("{}/probe")\n'.format(base_url),
        "url-file": 'include url("{}")\n'.format(_file_url(abs_posix)),
        "plain-http-url": 'include "{}/probe"\n'.format(base_url),
        "classpath": 'include classpath("x.conf")\n',
        "required": 'include required(file("inc.conf"))\n',
        "package": 'include package("pyhocon:__init__.py")\n',
        "space-before-paren": 'include file ("inc.conf")\n',
        "uppercase-keyword": 'INCLUDE file("inc.conf")\n',
        "bare-keyword": "include = 1\n",
    }

    with pytest.raises(BackendError, match="line 1"):
        HOCONBackend(hocon_includes=hocon_includes).loads(contents[kind])

    assert pyhocon_tripwire == []
    assert hits == []


@pytest.mark.parametrize(
    "content,expected",
    [
        ('"include" = 1\nplain = p\n', {"include": 1, "plain": "p"}),
        ("includes = 1\n", {"includes": 1}),
        ('s = "include \\"x\\""\n', {"s": 'include "x"'}),
    ],
    ids=["quoted-key", "plural-key", "in-string"],
)
def test_include_words_that_are_not_directives(content, expected, pyhocon_tripwire):
    result = HOCONBackend().loads(content)
    assert result == expected
    assert pyhocon_tripwire == []


def test_invalid_utf8_is_backend_error():
    pytest.importorskip("pyhocon")
    with pytest.raises(BackendError):
        HOCONBackend().load(io.BytesIO(b"k = \xff\n"))


def test_broken_pyhocon_leaves_other_backends_working(tmp_path, monkeypatch, make_tree):
    fake_pkg = tmp_path / "fake" / "pyhocon"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_bytes(
        b"raise AttributeError(\"module 'collections' has no attribute "
        b"'MutableMapping'\")\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "fake"))
    for name in list(sys.modules):
        if name == "pyhocon" or name.startswith("pyhocon."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.invalidate_caches()

    assert has_hocon() is False
    # HOCONBackend is *always* registered; other backends keep
    # working regardless, and a hocon_data level fails at build time instead.
    assert HOCONBackend in default_backends()

    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"

    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend.check_available()
    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend().loads("k = v")


# -- default: include file(...) really reads the file ----------------------


@pytest.mark.parametrize("kind", ["relative", "absolute"])
def test_include_file_reads_by_default(kind, tmp_path, monkeypatch):
    # No tripwire here: a real `parse_file` call is exactly the point.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")
    abs_conf = tmp_path / "abs.conf"
    abs_conf.write_bytes(b"fromabs = included-abs\n")
    abs_posix = abs_conf.resolve().as_posix()

    targets = {"relative": "inc.conf", "absolute": abs_posix}
    key = "fromfile" if kind == "relative" else "fromabs"
    content = 'include file("{}")\nplain = p\n'.format(targets[kind])

    result = HOCONBackend().loads(content)

    assert result == {
        key: "included" if kind == "relative" else "included-abs",
        "plain": "p",
    }

    # `file(...)` is the one form this mode lets pyhocon resolve for real,
    # so it is deliberately NOT a tripwire target (see the fixture) -- no
    # separate assertion needed here, only that the real content came back.


def test_include_file_missing_contributes_nothing_by_default(tmp_path, monkeypatch):
    # Oracle-measured (WSL, 2026-09-29): a non-required `include file(...)`
    # whose target does not exist silently contributes nothing in Puppet
    # too -- confirmed via `puppet lookup` directly, not assumed.
    monkeypatch.chdir(tmp_path)
    result = HOCONBackend().loads('include file("missing.conf")\nplain = p\n')
    assert result == {"plain": "p"}


def test_include_file_inside_nested_object_and_after_a_key(tmp_path, monkeypatch):
    # Oracle-measured: `include file(...)` also resolves inside a nested
    # object and after another key on its own line -- not just at the
    # document's own top level.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")

    nested = HOCONBackend().loads('sub {\n include file("inc.conf")\n}\n')
    assert nested == {"sub": {"fromfile": "included"}}

    after_key = HOCONBackend().loads('a = 1,\ninclude file("inc.conf")\n')
    assert after_key == {"a": 1, "fromfile": "included"}


def test_include_required_always_raises_present_or_missing(tmp_path, monkeypatch):
    # Oracle-measured: `required(...)` is a parse error in Puppet
    # regardless of whether its target exists -- Ruby hocon never gets far
    # enough to check, so presence/absence makes no difference.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")
    for target in ("inc.conf", "missing.conf"):
        with pytest.raises(BackendError, match="line 1"):
            HOCONBackend().loads('include required(file("{}"))\n'.format(target))


def test_include_file_globs_where_puppet_does_not(tmp_path, monkeypatch):
    # ACCEPTED DIVERGENCE (hyera may do more than Puppet, never less):
    # Puppet's
    # `include file("*.conf")` never globs (oracle-measured: contributes
    # nothing); pyhocon's own `file(...)` resolution does glob and include
    # every match, which this mode leaves untouched. Pinned here so a
    # future pyhocon upgrade that changes this is caught, not silently
    # unnoticed.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")
    result = HOCONBackend().loads('include file("*.conf")\nplain = p\n')
    assert result == {"fromfile": "included", "plain": "p"}


# -- default: value/array position is always literal text ------------------


@pytest.mark.parametrize(
    "content,expected",
    [
        ('msg = please include "inc.conf"\n', {"msg": "please include inc.conf"}),
        (
            'msg = please include file("inc.conf")\n',
            {"msg": "please include file(inc.conf)"},
        ),
        (
            'msg = please include url("http://h/x")\n',
            {"msg": "please include url(http://h/x)"},
        ),
        (
            'msg = please include classpath("x.conf")\n',
            {"msg": "please include classpath(x.conf)"},
        ),
        (
            'msg = please include required(file("inc.conf"))\n',
            {"msg": "please include required(file(inc.conf))"},
        ),
    ],
    ids=["quoted", "file", "url", "classpath", "required"],
)
def test_include_value_position_is_literal_text_by_default(
    content, expected, pyhocon_tripwire, http_server
):
    # Oracle-measured (WSL, 2026-09-29, real `puppet lookup`): `include`,
    # in ANY of these forms, is never special outside statement position
    # to Ruby -- it is ordinary text that HOCON's own string concatenation
    # joins with its neighbours, quotes stripped exactly as any other
    # quoted segment. Every one of these would otherwise resolve for real
    # (or error) if treated as a directive; the tripwire/http_server prove
    # none of that happens.
    _server, hits = http_server
    assert HOCONBackend().loads(content) == expected
    assert pyhocon_tripwire == []
    assert hits == []


@pytest.mark.parametrize(
    "content,expected",
    [
        ('l = [\n include "inc.conf"\n]\n', {"l": ["include inc.conf"]}),
        (
            'l = [\n 1,\n include "inc.conf"\n]\n',
            {"l": [1, "include inc.conf"]},
        ),
        (
            'l = [\n include file("inc.conf")\n]\n',
            {"l": ["include file(inc.conf)"]},
        ),
    ],
    ids=["sole-element", "after-a-value", "file-form"],
)
def test_include_in_array_value_position_is_literal_text_by_default(
    content, expected, pyhocon_tripwire
):
    # A key-position plain include contributes nothing (blanked) at the
    # top level or inside an object, but the same directive inside a
    # `[...]` array is a value, not a key -- Puppet keeps it as literal
    # text (oracle-measured, real `puppet lookup`:
    # `l=[include "inc.conf"]` -> `["include inc.conf"]`), and the
    # default must not silently blank it into an empty/short array, nor
    # resolve it as a real include, instead.
    assert HOCONBackend().loads(content) == expected
    assert pyhocon_tripwire == []


@pytest.mark.parametrize(
    "content,expected",
    [
        ('l = [\n include "inc.conf"\n]\n', {"l": ["include inc.conf"]}),
        (
            'l = [\n 1,\n include "inc.conf"\n]\n',
            {"l": [1, "include inc.conf"]},
        ),
    ],
    ids=["sole-element", "after-a-value"],
)
def test_include_in_array_value_position_raises_when_refused(
    content, expected, pyhocon_tripwire
):
    # Opt-in guard (`hocon_includes=False`): a directive-shaped
    # value-position occurrence raises rather than being kept as literal
    # text.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=False).loads(content)
    assert pyhocon_tripwire == []


# -- opt-in guard: every include form is refused, file() included ------------


@pytest.mark.parametrize(
    "kind",
    [
        "file-relative",
        "file-absolute",
        "url-http",
        "url-file",
        "plain-http-url",
        "classpath",
        "required",
        "package",
        "space-before-paren",
        "uppercase-keyword",
        "bare-keyword",
        "value-position",
    ],
)
def test_include_form_raises_when_refused(
    kind, tmp_path, monkeypatch, pyhocon_tripwire, http_server
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromcwd = yes\n")
    abs_conf = tmp_path / "abs.conf"
    abs_conf.write_bytes(b"fromabs = yes\n")
    abs_posix = abs_conf.resolve().as_posix()
    server, hits = http_server
    base_url = "http://{}:{}".format(*server.server_address)

    contents = {
        "file-relative": 'include file("inc.conf")\n',
        "file-absolute": 'include file("{}")\n'.format(abs_posix),
        "url-http": 'include url("{}/probe")\n'.format(base_url),
        "url-file": 'include url("{}")\n'.format(_file_url(abs_posix)),
        "plain-http-url": 'include "{}/probe"\n'.format(base_url),
        "classpath": 'include classpath("x.conf")\n',
        "required": 'include required(file("inc.conf"))\n',
        "package": 'include package("pyhocon:__init__.py")\n',
        "space-before-paren": 'include file ("inc.conf")\n',
        "uppercase-keyword": 'INCLUDE file("inc.conf")\n',
        "bare-keyword": "include = 1\n",
        "value-position": 'msg = please include "inc.conf"\n',
    }

    with pytest.raises(BackendError, match="line 1"):
        HOCONBackend(hocon_includes=False).loads(contents[kind])

    assert pyhocon_tripwire == []
    assert hits == []
