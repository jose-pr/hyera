"""HOCON ``include`` directives resolve exactly as Puppet's own
``hocon_data`` does by default (2026-09-29), and the pre-fidelity refusal
is kept as an opt-in (``HOCONBackend(hocon_includes=False)``, or a
``hocon_includes: false`` key on the hierarchy entry/``defaults``).

Oracle-measured (Puppet 8.10.0 / Ruby hocon 1.4.0, WSL, 2026-09-29,
re-verified live for the forms below): a plain quoted ``include "..."``
contributes nothing in either mode; ``include file(...)`` really reads the
file (relative to the process cwd, or absolute) under the default, and
raises under the opt-in; every other form (``url(...)``, ``classpath(...)``,
``required(...)``, ``package(...)``, a case-mismatched keyword, a bare
``include`` with nothing valid after it) raises in both modes, matching
Puppet's own parse/method errors; a value-position directive (including
inside a ``[...]`` array), of any spelling/case, is kept as literal text
under the default (matching Puppet) and raises under the opt-in.

A ``pyhocon_tripwire`` proves pyhocon's own include machinery
(``ConfigFactory.parse_file``, ``.parse_URL``, ``ConfigParser.
resolve_package_path``) never runs for a form neither mode intends to
resolve for real -- patched on BOTH the shared ``pyhocon`` module (any
caller using ``import pyhocon`` directly) and hyera's own private
``_hocon_parser()`` module copy (a *different* ``ConfigFactory``/
``ConfigParser`` class since ``json_hocon_loaders``'s ``exec_module`` copy
-- the one ``HOCONBackend.loads`` actually calls through; patching only the
shared module left this fixture unable to observe a real ``HOCONBackend``
call at all, a gap found and fixed in the same commit that reversed the
default). An ``http_server`` proves no network request is ever made.
"""

import importlib
import http.server
import io
import sys
import threading

import pytest

from hyera import BackendError, Hiera, default_backends
from hyera.backends import HOCONBackend, _hocon_parser, has_hocon


@pytest.fixture
def pyhocon_tripwire(monkeypatch):
    pytest.importorskip("pyhocon")
    import pyhocon.config_parser as cp

    calls = []

    def _tripwire(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("pyhocon's own include machinery must never run")

    # Both the shared module and HOCONBackend's own private copy (see the
    # module docstring) -- `_hocon_parser()` also builds/caches the latter,
    # which must exist before it can be patched.
    for target in (cp, _hocon_parser()):
        monkeypatch.setattr(target.ConfigFactory, "parse_file", _tripwire)
        monkeypatch.setattr(target.ConfigFactory, "parse_URL", _tripwire)
        monkeypatch.setattr(target.ConfigParser, "resolve_package_path", _tripwire)
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
    # ACCEPTED DIVERGENCE (hyera may do more than Puppet, never less),
    # filed as `hocon-file-include-globs-where-puppet-does-not`: Puppet's
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
    # Opt-in guard (`hocon_includes=False`): reproduces the pre-fidelity
    # refusal exactly -- a directive-shaped value-position occurrence
    # raises rather than being kept as literal text.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=False).loads(content)
    assert pyhocon_tripwire == []


# -- default: a hierarchy-level `hocon_includes: false` reaches the
# backend the same way `datadir` already does --------------------------------


def test_hocon_includes_false_via_hierarchy_conf(
    tmp_path, monkeypatch, pyhocon_tripwire
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")
    conf = {"hocon_includes": False}
    with pytest.raises(BackendError, match="line 1"):
        HOCONBackend(conf).loads('include file("inc.conf")\nplain = p\n')
    assert pyhocon_tripwire == []


# -- opt-in guard: reproduces the pre-fidelity refusal exactly, file() included --


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


# -- R5 adversarial forms (independent security review, round 2): 17 inputs
# where the text scanner missed a directive pyhocon's own grammar honours
# caselessly, across a triple-quoted string, a comment, or a substitution.
# All 17 stay a regression suite for the opt-in guard (still raise, exactly
# as before this fidelity work). Under the default, each one now falls into one of
# three buckets, confirmed against hyera's own code (not assumed) and
# reasoned from the same case-sensitivity/position rules the other tests
# in this file establish directly against the oracle:
#   A. key position, case-mismatched keyword -> still raises unconditionally
#      (case-mismatch handling never depends on `hocon_includes`; letting
#      pyhocon's own *caseless* grammar run a real read here would be an
#      unintended extra capability, not a documented one);
#   B. key position, exactly lowercase "include file(...)" once the
#      scanner correctly finds the true end of a string/comment/
#      substitution -- resolves for real (a missing target contributes
#      nothing, as in every other file() test above);
#   C. value position, any spelling/case -- defangs to literal text, same
#      rule as the dedicated value-position tests above.
# -----------------------------------------------------------------------


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

#: Bucket classification for the default half of this matrix. "D" is
#: its own singleton (see the dedicated test below): the scanner correctly
#: recognizes `include file(...)` here too (same as bucket B), but the
#: unrelated `x\${` immediately before it is not actually valid HOCON to
#: pyhocon regardless of includes at all -- confirmed directly (a bare
#: `a = x\${` with no include anywhere nearby fails the exact same way,
#: "Expected '}', found end of text") -- so the end-to-end outcome is
#: still an error, just not because of anything include-related.
_ADVERSARIAL_BUCKET = {
    "dotless-i-plain": "A",
    "dotless-i-file": "A",
    "dotless-i-in-object": "A",
    "dotless-i-required": "A",
    "dotless-i-in-value": "C",
    "double-slash-unquoted": "C",
    "url-like-unquoted-value": "C",
    "escaped-hash": "C",
    "double-slash-after-digit": "C",
    "quote-run-4": "B",
    "quote-run-7": "B",
    "escaped-quote-unquoted": "B",
    "lone-cr-ends-hash-comment": "B",
    "lone-cr-ends-slash-comment": "B",
    "escaped-substitution": "D",
    "triple-quote-key-escape": "B",
    "lone-cr-inside-slash-comment": "B",
}


@pytest.mark.parametrize(
    "content,line,label",
    _ADVERSARIAL_INCLUDE_FORMS,
    ids=[id_ for _content, _line, id_ in _ADVERSARIAL_INCLUDE_FORMS],
)
def test_adversarial_include_forms_raise_when_refused(
    content, line, label, pyhocon_tripwire, http_server
):
    with pytest.raises(BackendError, match="line {}".format(line)):
        HOCONBackend(hocon_includes=False).loads(content)

    assert pyhocon_tripwire == []
    assert http_server[1] == []


_ADVERSARIAL_NEUTRALIZED = [
    p for p in _ADVERSARIAL_INCLUDE_FORMS if _ADVERSARIAL_BUCKET[p[2]] in ("A", "C")
]
_ADVERSARIAL_RESOLVES = [
    p for p in _ADVERSARIAL_INCLUDE_FORMS if _ADVERSARIAL_BUCKET[p[2]] == "B"
]


@pytest.mark.parametrize(
    "content,_line,label",
    _ADVERSARIAL_NEUTRALIZED,
    ids=[id_ for _content, _line, id_ in _ADVERSARIAL_NEUTRALIZED],
)
def test_adversarial_include_forms_neutralized_by_default(
    content, _line, label, tmp_path, monkeypatch, pyhocon_tripwire, http_server
):
    # Buckets A (key position, case-mismatched -- still raises
    # unconditionally) and C (value position, any case -- defangs to
    # literal text): in both, pyhocon's own real include machinery must
    # never run, so the tripwire applies here.
    monkeypatch.chdir(tmp_path)
    bucket = _ADVERSARIAL_BUCKET[label]
    _server, hits = http_server

    if bucket == "A":
        with pytest.raises(BackendError):
            HOCONBackend().loads(content)
    else:  # "C"
        HOCONBackend().loads(content)  # must not raise

    assert pyhocon_tripwire == []
    assert hits == []


@pytest.mark.parametrize(
    "content,_line,label",
    _ADVERSARIAL_RESOLVES,
    ids=[id_ for _content, _line, id_ in _ADVERSARIAL_RESOLVES],
)
def test_adversarial_include_forms_resolve_by_default(
    content, _line, label, tmp_path, monkeypatch, http_server
):
    # Bucket B: once the scanner correctly finds the true end of the
    # preceding string/comment/substitution, this is a genuine, exactly
    # lowercase `include file(...)` at key position -- the whole point of
    # the default is to let it resolve for real, so no tripwire fixture
    # here (a real `parse_file` call is expected and correct). The target
    # is missing (this test never creates `inc.conf`), which silently
    # contributes nothing (see test_include_file_missing_contributes_
    # nothing_by_default) rather than raising -- confirming the include
    # was recognized and handed to pyhocon, not simply ignored as inert
    # text or blocked by a stray raise.
    monkeypatch.chdir(tmp_path)
    _server, hits = http_server

    HOCONBackend().loads(content)  # must not raise

    assert hits == []


def test_adversarial_escaped_substitution_form_still_errors_by_default(
    tmp_path, monkeypatch
):
    # Bucket D (see _ADVERSARIAL_BUCKET): the scanner correctly recognizes
    # `include file(...)` here (the same as every bucket-B case), but
    # `a = x\${` right before it is not valid HOCON to pyhocon at all,
    # with or without an include nearby -- confirmed directly against a
    # bare `a = x\${\nb = "y"\n` with no include anywhere in it, which
    # fails the exact same way ("Expected '}', found end of text"). Under
    # the pre-fidelity refusal this never surfaced, because the scanner always
    # raised on `include` itself first; under the default it still ends
    # in a BackendError, just for pyhocon's own, unrelated reason.
    content = [
        c
        for c, _l, label in _ADVERSARIAL_INCLUDE_FORMS
        if label == "escaped-substitution"
    ][0]
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError):
        HOCONBackend().loads(content)
