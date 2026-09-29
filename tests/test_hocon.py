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
import io
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

    result = HOCONBackend().loads(content)

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
        HOCONBackend().loads(contents[kind])

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
    # Design Q5: HOCONBackend is *always* registered; other backends keep
    # working regardless, and a hocon_data level fails at build time instead.
    assert HOCONBackend in default_backends()

    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("k") == "v"

    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend.check_available()
    with pytest.raises(BackendError, match="pyhocon"):
        HOCONBackend().loads("k = v")


# Independent security review, round 2: 17 adversarial inputs where the text
# scanner missed a directive pyhocon's own grammar honours caselessly, across
# a triple-quoted string, a comment, or a substitution -- each proven (before
# the fix) by the pyhocon include machinery actually running (the tripwire
# firing). Every one must raise before pyhocon ever parses the text, with an
# empty tripwire; most report line 1, but a few genuinely start their
# "include" on line 2, either because the source literally has a newline
# first, or because the scanner closes a run-away triple-quoted string and no
# longer treats an escaped "${" as a real substitution, so the scan continues
# past them instead of stopping there.
_ADVERSARIAL_INCLUDE_FORMS = [
    ('ınclude "inc.conf"\nplain = p\n', 1, "dotless-i-plain"),
    ('ınclude file("inc.conf")\nplain = p\n', 1, "dotless-i-file"),
    ('msg = x ınclude file("inc.conf")\n', 1, "dotless-i-in-value"),
    ('a = """x""""\ninclude file("inc.conf")\nb = "c"\n', 2, "quote-run-4"),
    ('a = """""""\ninclude file("inc.conf")\nb = "c"\n', 2, "quote-run-7"),
    (
        'a = x\\"\ninclude file("inc.conf")\nb = "y"\n',
        2,
        "escaped-quote-unquoted",
    ),
    ('a = x//y include file("inc.conf")\n', 1, "double-slash-unquoted"),
    ('a = http://h include file("inc.conf")\n', 1, "url-like-unquoted-value"),
    ('# c\rinclude file("inc.conf")\nplain = p\n', 2, "lone-cr-ends-hash-comment"),
    (
        '// c\rinclude file("inc.conf")\nplain = p\n',
        2,
        "lone-cr-ends-slash-comment",
    ),
    ('a = x\\# include file("inc.conf")\n', 1, "escaped-hash"),
    ('a = x\\${\ninclude file("inc.conf")\nb = }\n', 2, "escaped-substitution"),
    (
        '"""k\\"""" = 1\ninclude file("inc.conf")\nz = "q"\n',
        2,
        "triple-quote-key-escape",
    ),
    ('a = 1x//y include file("inc.conf")\n', 1, "double-slash-after-digit"),
    (
        'a = 1 // c\rinclude file("inc.conf")\n',
        2,
        "lone-cr-inside-slash-comment",
    ),
    ('o {\n ınclude file("inc.conf")\n}\n', 2, "dotless-i-in-object"),
    ('ınclude required(file("inc.conf"))\n', 1, "dotless-i-required"),
]


@pytest.mark.parametrize(
    "content,line",
    [(content, line) for content, line, _id in _ADVERSARIAL_INCLUDE_FORMS],
    ids=[id_ for _content, _line, id_ in _ADVERSARIAL_INCLUDE_FORMS],
)
def test_adversarial_include_forms_raise(content, line, pyhocon_tripwire, http_server):
    _server, hits = http_server

    with pytest.raises(BackendError, match="line {}".format(line)):
        HOCONBackend().loads(content)

    assert pyhocon_tripwire == []
    assert hits == []


@pytest.mark.parametrize(
    "content",
    [
        'l = [\n include "inc.conf"\n]\n',
        'l = [\n 1,\n include "inc.conf"\n]\n',
    ],
    ids=["sole-element", "after-a-value"],
)
def test_include_in_array_value_position_raises(content, pyhocon_tripwire):
    # A key-position plain include contributes nothing (blanked) at the top
    # level or inside an object, but the same directive inside a `[...]`
    # array is a value, not a key -- Puppet keeps it as literal text, and
    # pyera (which always raises for value position) must not silently blank
    # it into an empty/short array instead.
    with pytest.raises(BackendError):
        HOCONBackend().loads(content)

    assert pyhocon_tripwire == []
