"""The include scanner on adversarial inputs: unterminated tokens, substitutions
and bracket nesting.
"""

import pytest

from hyera import BackendError
from hyera.backends import HOCONBackend
from hocon_support import (  # noqa: F401
    http_server,
    pyhocon_tripwire,
)

# Adversarial forms the text scanner once missed. Each raises under the opt-in
# guard; by default: A key position, case-mismatched raises; B lowercase
# `include file(...)` resolves; C value position defangs to text.


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


# Bucket per adversarial case for the default half; "D" is a singleton (see below):
# recognized like bucket B, but the preceding `x\${` is invalid HOCON on its own
# ("Expected '}', found end of text").
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
    # Buckets A (case-mismatched key, still raises) and C (value position, defangs to
    # text): pyhocon's include machinery must never run, so the tripwire applies.
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
    # Bucket B: a genuine lowercase `include file(...)` resolves, so no tripwire. The
    # missing target (inc.conf) contributes nothing, which shows the include was
    # recognized and handed to pyhocon.
    monkeypatch.chdir(tmp_path)
    _server, hits = http_server

    HOCONBackend().loads(content)  # must not raise

    assert hits == []


def test_adversarial_escaped_substitution_form_still_errors_by_default(
    tmp_path, monkeypatch
):
    # Bucket D: recognized like bucket B, but `a = x\${` is invalid HOCON with or
    # without an include. The opt-in refusal raises on `include` first; the default ends
    # in a BackendError for pyhocon's own reason.
    content = [
        c
        for c, _l, label in _ADVERSARIAL_INCLUDE_FORMS
        if label == "escaped-substitution"
    ][0]
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError):
        HOCONBackend().loads(content)


# scanner edge cases: unterminated tokens, substitutions, brackets. These exercise
# the scanner's bookkeeping (end of a string, skipping `${...}`, `{`/`[` nesting),
# not the include rules of test_hocon.py.


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_unterminated_triple_quoted_string_reaches_end_of_text(hocon_includes):
    # No closing `"""` anywhere: `_find_hocon_string_end` must stop at end of text. The
    # HOCON is invalid either way; the scanner terminates and pyhocon's failure
    # surfaces as a BackendError.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=hocon_includes).loads('a = """abc\n')


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_uppercase_include_without_directive_argument_is_ordinary_key(
    hocon_includes, pyhocon_tripwire
):
    # `INCLUDE` matches the keyword caselessly, but nothing directive-shaped follows it
    # (`= 1`): `_hocon_directive_follows` says no and both scanners treat it as an
    # ordinary key in either mode, as Puppet's case-sensitive match would.
    result = HOCONBackend(hocon_includes=hocon_includes).loads(
        "INCLUDE = 1\nplain = p\n"
    )
    assert result == {"INCLUDE": 1, "plain": "p"}
    assert pyhocon_tripwire == []


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_substitution_reference_before_an_include_is_skipped(
    hocon_includes, pyhocon_tripwire
):
    # A `${...}` substitution is skipped wholesale up to its closing `}`; a real
    # include right after it must still be found at the correct position.
    result = HOCONBackend(hocon_includes=hocon_includes).loads(
        'foo = 1\nx = ${foo}\ninclude "inc.conf"\nplain = p\n'
    )
    assert result == {"foo": 1, "x": 1, "plain": "p"}
    assert pyhocon_tripwire == []


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_unclosed_substitution_reference_reaches_end_of_text(hocon_includes):
    # No closing `}` anywhere in the text -- the substitution skip must
    # stop at end-of-text rather than scanning past it. Invalid HOCON
    # either way; this only proves the scanner itself terminates cleanly.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=hocon_includes).loads("x = ${foo\n")


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_multiline_triple_quoted_plain_include_preserves_newlines(
    hocon_includes, pyhocon_tripwire
):
    # A plain quoted include's target may be a multi-line triple-quoted string; blanking
    # it must keep the newlines so later pyhocon error line numbers still line up.
    result = HOCONBackend(hocon_includes=hocon_includes).loads(
        'include """a\nb"""\nplain = p\n'
    )
    assert result == {"plain": "p"}
    assert pyhocon_tripwire == []


def test_bare_value_position_include_without_argument_stays_literal():
    # In value position, `include` with nothing directive-shaped after it is not
    # defanged: pyhocon already treats it as an ordinary bareword.
    result = HOCONBackend().loads("msg = foo include\n")
    assert result == {"msg": "foo include"}


def test_object_with_plain_include_scans_matching_close_brace(pyhocon_tripwire):
    # The bracket stack (`{`/`[` pushed, `}`/`]` popped) must find a match; a plain
    # include that raises before a closing bracket never exercises it.
    result = HOCONBackend(hocon_includes=False).loads(
        'o {\n include "inc.conf"\n}\nplain = p\n'
    )
    assert result == {"o": {}, "plain": "p"}
    assert pyhocon_tripwire == []


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_unbalanced_closing_brace_does_not_crash_the_scanner(hocon_includes):
    # A stray `}` with nothing open must leave the bracket stack alone; the malformed
    # input still reaches pyhocon and comes back as a BackendError.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=hocon_includes).loads("}\n")
