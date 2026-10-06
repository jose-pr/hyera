"""The include scanner on adversarial inputs: unterminated tokens, substitutions and bracket nesting."""

import pytest

from hyera import BackendError
from hyera.backends import HOCONBackend
from hocon_support import (  # noqa: F401
    http_server,
    pyhocon_tripwire,
)

# -- Adversarial forms: 17 inputs
# where the text scanner missed a directive pyhocon's own grammar honours
# caselessly, across a triple-quoted string, a comment, or a substitution.
# All 17 form a regression suite for the opt-in guard (each raises). Under
# the default, each one falls into one of
# three buckets, confirmed against hyera's own code (not assumed) and
# reasoned from the same case-sensitivity/position rules the tests
# in test_hocon.py establish directly against the oracle:
#   A. key position, case-mismatched keyword -> still raises unconditionally
#      (case-mismatch handling never depends on `hocon_includes`; letting
#      pyhocon's own *caseless* grammar run a real read here would be an
#      unintended extra capability, not a documented one);
#   B. key position, exactly lowercase "include file(...)" once the
#      scanner correctly finds the true end of a string/comment/
#      substitution -- resolves for real (a missing target contributes
#      nothing, as in every other file() test in test_hocon.py);
#   C. value position, any spelling/case -- defangs to literal text, same
#      rule as the dedicated value-position tests in test_hocon.py.
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
    # the opt-in refusal this never surfaces, because the scanner always
    # raises on `include` itself first; under the default it still ends
    # in a BackendError, just for pyhocon's own, unrelated reason.
    content = [
        c
        for c, _l, label in _ADVERSARIAL_INCLUDE_FORMS
        if label == "escaped-substitution"
    ][0]
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BackendError):
        HOCONBackend().loads(content)


# -- scanner edge cases: unterminated tokens, substitutions, brackets ------
#
# These do not exercise the include rules themselves (covered in test_hocon.py); they
# exercise the scanner's own bookkeeping -- finding the end of a string,
# skipping a `${...}` substitution, and tracking `{`/`[` nesting -- in
# situations an include-focused test never reaches (either because the
# scanner would already have raised before getting there, or because
# nothing include-shaped needs to be present at all).


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_unterminated_triple_quoted_string_reaches_end_of_text(hocon_includes):
    # No closing `"""` anywhere in the text -- `_find_hocon_string_end`
    # must stop at end-of-text rather than scanning past it looking for a
    # terminator that will never appear. Invalid HOCON either way; this
    # only proves the scanner itself terminates cleanly and the eventual
    # pyhocon parse failure surfaces as a BackendError, not some scanner-
    # internal error.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=hocon_includes).loads('a = """abc\n')


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_uppercase_include_without_directive_argument_is_ordinary_key(
    hocon_includes, pyhocon_tripwire
):
    # `INCLUDE` case-matches the keyword caselessly, but nothing
    # directive-shaped follows it (`= 1`, not a quoted string or a
    # `name(...)` call) -- `_hocon_directive_follows` correctly says no,
    # and both scanners fall through to treating this as an entirely
    # ordinary key, in either mode. Puppet's own case-sensitive match would
    # do the same (`INCLUDE` is simply not its `include` keyword either).
    result = HOCONBackend(hocon_includes=hocon_includes).loads(
        "INCLUDE = 1\nplain = p\n"
    )
    assert result == {"INCLUDE": 1, "plain": "p"}
    assert pyhocon_tripwire == []


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_substitution_reference_before_an_include_is_skipped(
    hocon_includes, pyhocon_tripwire
):
    # A `${...}` substitution reference is skipped wholesale (up to its
    # closing `}`) rather than scanned character by character -- this
    # proves that skip runs at all, and that scanning is still correctly
    # positioned to find a *real* include right after it.
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
    # A plain quoted include's target can itself be a triple-quoted string
    # spanning multiple lines. Blanking it out (it "contributes nothing"
    # in either mode) must skip over the embedded newlines instead of
    # overwriting them with spaces, so line numbers after it still line up
    # with the original text for any later pyhocon parse error.
    result = HOCONBackend(hocon_includes=hocon_includes).loads(
        'include """a\nb"""\nplain = p\n'
    )
    assert result == {"plain": "p"}
    assert pyhocon_tripwire == []


def test_bare_value_position_include_without_argument_stays_literal():
    # In value position, `include` with nothing directive-shaped after it
    # (just end-of-line) is not defanged at all -- pyhocon's own unquoted-
    # value grammar already treats it as an ordinary bareword, so there is
    # nothing here that needs quoting to keep pyhocon's tokenizer from
    # seeing a keyword.
    result = HOCONBackend().loads("msg = foo include\n")
    assert result == {"msg": "foo include"}


def test_object_with_plain_include_scans_matching_close_brace(pyhocon_tripwire):
    # The bracket-stack bookkeeping (`{`/`[` pushed, `}`/`]` popped) has to
    # actually run and find a match on its stack -- a plain include that
    # raises before ever reaching a closing bracket (as with the
    # case-mismatched/space-before-paren forms above) never exercises this.
    result = HOCONBackend(hocon_includes=False).loads(
        'o {\n include "inc.conf"\n}\nplain = p\n'
    )
    assert result == {"o": {}, "plain": "p"}
    assert pyhocon_tripwire == []


@pytest.mark.parametrize("hocon_includes", [True, False], ids=["default", "refuse"])
def test_unbalanced_closing_brace_does_not_crash_the_scanner(hocon_includes):
    # A stray `}` with nothing open on the bracket stack -- the scanner
    # must not pop from an empty stack; it just leaves the stack alone and
    # keeps going. The text is not valid HOCON either way, so this only
    # proves the scanner survives it and the malformed input still reaches
    # pyhocon and comes back as an ordinary BackendError.
    with pytest.raises(BackendError):
        HOCONBackend(hocon_includes=hocon_includes).loads("}\n")
