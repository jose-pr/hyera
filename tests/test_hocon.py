"""HOCON ``include`` directives never read a file or fetch a URL, and an
installed-but-broken ``pyhocon`` never breaks every ``Hiera()``.

A plain quoted ``include "..."`` contributes nothing (matching Puppet);
every other include form (``file()``, ``url()``, ``classpath()``,
``required()``, ``package()``, a case-mismatched keyword, a directive in
value position, or a bare ``include`` with nothing valid after it) raises
``BackendError`` before pyhocon's own include machinery ever runs. A
``pyhocon_tripwire`` proves that machinery (``ConfigFactory.parse_file``,
``.parse_URL``, ``ConfigParser.resolve_package_path``) is never called, and
an ``http_server`` proves no network request is ever made.
"""

import importlib
import http.server
import sys
import threading

import pytest

from pyera import BackendError, Hiera, default_backends
from pyera.backends import HOCONBackend, has_hocon


@pytest.fixture
def pyhocon_tripwire(monkeypatch):
    pytest.importorskip("pyhocon")
    import pyhocon.config_parser as cp

    calls = []

    def _tripwire(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("pyhocon's own include machinery must never run")

    monkeypatch.setattr(cp.ConfigFactory, "parse_file", _tripwire)
    monkeypatch.setattr(cp.ConfigFactory, "parse_URL", _tripwire)
    monkeypatch.setattr(cp.ConfigParser, "resolve_package_path", _tripwire)
    return calls


@pytest.fixture
def http_server():
    hits = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"probe = hit\n")

        def log_message(self, *args, **kwargs):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _file_url(posix_path: str) -> str:
    return "file:///" + posix_path.lstrip("/")


@pytest.mark.parametrize("kind", ["relative", "absolute", "file-url"])
def test_plain_include_contributes_nothing(
    kind, tmp_path, monkeypatch, pyhocon_tripwire
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_text("fromcwd = yes\n")
    abs_conf = tmp_path / "abs.conf"
    abs_conf.write_text("fromabs = yes\n")
    abs_posix = abs_conf.resolve().as_posix()

    targets = {
        "relative": "inc.conf",
        "absolute": abs_posix,
        "file-url": _file_url(abs_posix),
    }
    content = 'include "{}"\nplain = p\n'.format(targets[kind])

    result = HOCONBackend().load(content.encode("utf-8"))

    assert result == {"plain": "p"}
    assert pyhocon_tripwire == []


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
def test_include_form_raises(
    kind, tmp_path, monkeypatch, pyhocon_tripwire, http_server
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_text("fromcwd = yes\n")
    abs_conf = tmp_path / "abs.conf"
    abs_conf.write_text("fromabs = yes\n")
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
        HOCONBackend().load(contents[kind].encode("utf-8"))

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
    result = HOCONBackend().load(content.encode("utf-8"))
    assert result == expected
    assert pyhocon_tripwire == []


def test_invalid_utf8_is_backend_error():
    pytest.importorskip("pyhocon")
    with pytest.raises(BackendError):
        HOCONBackend().load(b"k = \xff\n")


def test_broken_pyhocon_leaves_other_backends_working(tmp_path, monkeypatch):
    fake_pkg = tmp_path / "fake" / "pyhocon"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text(
        "raise AttributeError(\"module 'collections' has no attribute "
        "'MutableMapping'\")\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path / "fake"))
    for name in list(sys.modules):
        if name == "pyhocon" or name.startswith("pyhocon."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.invalidate_caches()

    assert has_hocon() is False
    assert HOCONBackend not in default_backends()

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text("k: v\n")
    config = {
        "version": 5,
        "defaults": {"data_hash": "yaml_data", "data_dir": "data"},
        "hierarchy": [{"name": "c", "path": "common.yaml"}],
    }
    h = Hiera(config, base_path=str(tmp_path))
    assert h.get("k") == "v"

    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend().load(b"k = v")
