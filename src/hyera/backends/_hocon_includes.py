"""Scanning HOCON text for ``include`` directives: refuse them or allow them.

Original code written to match Puppet's ``hocon_data``, not translated
from its source: each directive is found outside strings and comments, then
blanked or rejected, without needing ``pyhocon``.
"""

from __future__ import annotations

import re

from ..exceptions import BackendError

_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _find_hocon_string_end(text: str, start: int) -> "tuple[int, str]":
    """``text[start]`` is a double quote. Return ``(end, content)`` for a
    plain or triple-quoted HOCON string: ``end`` is the index just past the
    closing quote(s), ``content`` is the (still escaped) text between them.
    """
    n = len(text)
    if text.startswith('"""', start):
        content_start = start + 3
        close = text.find('"""', content_start)
        if close == -1:
            return n, text[content_start:n]
        # HOCON's triple-quoted string ends at the LAST quote of a run of 3+ quotes:
        # ``"""x""""`` is the string "x" plus a stray quote that pyhocon folds into the
        # terminator. Extend over every extra trailing quote to stay in sync.
        end = close + 3
        while end < n and text[end] == '"':
            end += 1
        return end, text[content_start:close]
    i = start + 1
    while i < n and text[i] != '"':
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    content = text[start + 1 : i]
    return (i + 1 if i < n else n), content


_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9A-Fa-f]{4})")
_DECODED_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _decode_unicode_escapes(text: str, out: "list[str]", start: int, content: str):
    """Rewrite each ``\\uXXXX`` in the quoted string at ``text[start]`` to the
    character it names, in ``out`` (the scanner's per-character copy of
    ``text``): the first slot takes the character and the other five become
    empty, so no other index shifts. Triple-quoted strings have no escapes,
    and a surrogate code unit is left as written.
    """
    if text.startswith('"""', start):
        return
    pos = 0
    while pos < len(content):
        if content[pos] != "\\":
            pos += 1
            continue
        match = _UNICODE_ESCAPE_RE.match(content, pos)
        if match is None:
            pos += 2  # any other escape pair, including ``\\``
            continue
        code = int(match.group(1), 16)
        if not 0xD800 <= code <= 0xDFFF:
            slot = start + 1 + pos
            char = chr(code)
            out[slot] = _DECODED_ESCAPES.get(char, char)
            for k in range(slot + 1, slot + 6):
                out[k] = ""
        pos += 6


def _skip_hocon_blanks(text: str, i: int) -> int:
    """Advance past spaces/tabs only (not newlines) from ``i``."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    return i


def _hocon_directive_follows(text: str, i: int) -> bool:
    """True if, starting at ``i``, the text looks like an attempted include
    argument -- a quoted string, or an identifier possibly followed by
    spaces/tabs and then ``(`` -- valid or not.
    """
    n = len(text)
    j = _skip_hocon_blanks(text, i)
    if j < n and text[j] == '"':
        return True
    if j < n and text[j].isalpha():
        k = j
        while k < n and (text[k].isalnum() or text[k] == "_"):
            k += 1
        k = _skip_hocon_blanks(text, k)
        return k < n and text[k] == "("
    return False


def _preceded_by_odd_backslashes(text: str, i: int) -> bool:
    """True if an odd number of consecutive ``\\`` immediately precede
    ``text[i]`` -- i.e. ``text[i]`` is itself escaped (``\\"``: escaped,
    ``\\\\"``: not, the first backslash is what's escaped). Mirrors
    pyhocon's own unquoted-value regex (``(?:[^...]|\\.)+``), which accepts
    a backslash-escaped ``"``, ``#``/``//`` or ``${`` as an ordinary
    character rather than the start of a string/comment/substitution.
    """
    count = 0
    j = i - 1
    while j >= 0 and text[j] == "\\":
        count += 1
        j -= 1
    return count % 2 == 1


def _refuse_hocon_includes(text: str) -> str:
    """Blank the plain HOCON ``include "..."`` directives Puppet ignores,
    and raise :class:`BackendError` for every other include form --
    ``include file(...)``, ``url(...)``, ``classpath(...)``,
    ``required(...)``, ``package(...)``, any other ``name(...)``, a
    directive in value position (including inside a ``[...]`` array), a
    case-mismatched or code-point-mismatched keyword (pyhocon's own
    ``Keyword("include", caseless=True)`` matches by ``.upper()``, which
    also folds a dotless-i ``ınclude`` to ``INCLUDE``), or a bare
    ``include`` with nothing valid after it -- before pyhocon ever parses
    the text. This keeps pyhocon's own include machinery (which reads
    files off the process cwd and fetches ``http(s)``/``file`` URLs) from
    ever running.

    **This is not the default.** It refuses more
    than Puppet's own ``hocon_data`` does (Puppet really does read
    ``file(...)`` and keeps a value-position directive as literal text --
    see :func:`_allow_hocon_includes`), so it is reachable only as the
    opt-in restriction (:class:`HOCONBackend`'s ``hocon_includes=False``).
    See ``AGENTS.md`` for the exact rule.

    A :func:`_install_hocon_include_guard`-installed backstop still applies
    even if this scanner has a gap: pyhocon's own include-resolution
    entry points raise unconditionally for the duration of
    :meth:`HOCONBackend.loads`, so a missed directive fails closed instead
    of silently reading a file or reaching the network.

    Blanked spans replace every non-newline character with a space, so
    line/column numbers in any later pyhocon parse error still line up
    with the original file.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    brackets = []  # stack of open '{'/'[' seen so far
    while i < n:
        c = text[i]
        if c in ('"', "#", "$") and _preceded_by_odd_backslashes(text, i):
            # An escaped quote, hash or substitution start in unquoted text (``x\"``,
            # ``x\#``, ``x\${``) is ordinary to pyhocon; unescaped ``"``, ``#`` and
            # ``$`` are excluded from unquoted values and always significant.
            last_sig = c
            i += 1
            continue
        if c == '"':
            end, content = _find_hocon_string_end(text, i)
            _decode_unicode_escapes(text, out, i, content)
            i = end
            last_sig = '"'
            continue
        if c == "#":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # pyhocon allows ``/`` in unquoted values, so ``//`` starts a comment only
            # at a token boundary (``http://h``, ``x//y`` and ``\//`` are text); ``:``
            # is no boundary, it sits in a token or splits key and value.
            prev = text[i - 1] if i > 0 else None
            at_boundary = prev is None or prev in ' \t\r\n{}[],="'
            if at_boundary and not _preceded_by_odd_backslashes(text, i):
                j = i
                while j < n and text[j] not in "\r\n":
                    j += 1
                i = j
                continue
            last_sig = c
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}", i + 2)
            i = (j + 1) if j != -1 else n
            last_sig = "}"
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\n" or c == "\r":
            # HOCON's line ending is any run of ``\n``/``\r`` (pyhocon: ``eol =
            # Word('\n\r')``): a lone ``\r`` ends a ``#``/``//`` comment and starts a
            # new key-position line as a newline does.
            last_sig = "\n"
            i += 1
            continue
        if c in "{[":
            brackets.append(c)
            last_sig = c
            i += 1
            continue
        if c in "}]":
            if brackets:
                brackets.pop()
            last_sig = c
            i += 1
            continue
        if c.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.upper() != "INCLUDE":
                last_sig = word[-1]
                i = j
                continue

            # Inside a `[...]` array every position is a value, never a key: Puppet
            # keeps a value-position include as literal text, and hyera (which raises
            # for value position) must not blank it away.
            in_array = bool(brackets) and brackets[-1] == "["
            key_position = (not in_array) and last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + text.count("\r", 0, i) + 1
            # A lone `\r` and a `\n` of one CRLF pair are both counted above; CRLF is
            # one logical newline elsewhere in this scanner, so undo the double count
            # for every CRLF pair before this position.
            line -= text.count("\r\n", 0, i)

            if key_position and word == "include":
                after = _skip_hocon_blanks(text, j)
                if after < n and text[after] == '"':
                    end, content = _find_hocon_string_end(text, after)
                    scheme = _URL_SCHEME_RE.match(content)
                    if scheme and scheme.group(0)[:-3].lower() != "file":
                        raise BackendError(
                            "HOCON include of a non-file URL is not "
                            "supported (line {})".format(line)
                        )
                    for k in range(i, end):
                        if out[k] not in ("\n", "\r"):
                            out[k] = " "
                    i = end
                    last_sig = " "
                    continue
                raise BackendError(
                    "HOCON include is only supported as a plain quoted "
                    "string; file()/url()/classpath()/required()/package() "
                    "and similar forms are not supported (line {})".format(line)
                )

            if _hocon_directive_follows(text, j):
                raise BackendError(
                    "HOCON include directive is not supported here "
                    "(line {})".format(line)
                )

            last_sig = word[-1]
            i = j
            continue

        last_sig = c
        i += 1

    return "".join(out)


def _allow_hocon_includes(text: str) -> str:
    """Scan HOCON ``include`` directives to match Puppet's own
    ``hocon_data`` (the default; :func:`_refuse_hocon_includes`
    is the opt-in restriction). Behaviour of Ruby hocon 1.4.0
    (Puppet 8.10.0) for every form:

    - a plain quoted ``include "..."`` (relative, absolute, or a
      ``file://`` URL) always contributes nothing -- Puppet's own bare
      quoted-string form is never a real file/URL read, in either mode;
    - a plain quoted ``include "<scheme>://...">`` for any *other* scheme
      always raises -- Puppet's own ``include_url_without_fallback``/
      ``include_url`` methods are simply missing (measured: a real
      ``http://`` target is never even requested);
    - ``include file(...)`` (relative or absolute, also inside a nested
      object or after another key) is left untouched here, so pyhocon's
      own resolution -- which already reads the file relative to the
      process cwd or absolute, exactly as Ruby hocon does -- runs for
      real;
    - every other key-position directive (``url(...)``, ``classpath(...)``,
      ``required(...)`` whether or not its target exists, ``package(...)``,
      any other ``name(...)``, a space before the paren, or a bare
      ``include`` with nothing valid after it) raises, matching Puppet's
      own parse/method errors for those forms -- Ruby hocon implements
      none of them;
    - an occurrence in value position (including inside a ``[...]``
      array), of *any* spelling/case that looks like a directive, is
      defanged into an ordinary quoted token instead of raising or being
      resolved, so the concatenated result is the exact literal text
      Puppet keeps (measured: ``msg = please include file("x")`` gives
      the literal string ``"please include file(x)"``, quotes and all
      stripped exactly as HOCON string concatenation does for any other
      quoted segment) -- Puppet's ``include`` is a member-level statement
      only, never special in value position, in any spelling;
    - a key-position spelling/case other than the exact lowercase keyword
      ``include`` is never Puppet's own directive either (Ruby's keyword
      match is case-sensitive) and always raises when a directive-shaped
      argument follows -- refusing rather than letting pyhocon's own
      *caseless* keyword matching run a real file/URL read Puppet's own
      hocon_data never would.

    One measured, accepted divergence: Puppet's ``include file("*.conf")``
    never globs (documented "contributes nothing"); pyhocon's own
    resolution *does* glob and includes any match -- kept as a documented
    extension (hyera may do more than Puppet, never less).

    A :func:`_install_hocon_include_guard`-installed backstop still
    applies for the forms this mode does not intend to resolve for real
    (``url``/``package`` resolution; see :meth:`HOCONBackend.loads`): a
    scanner gap in one of the forms above still fails closed instead of
    silently reaching the network. ``file(...)`` resolution is
    deliberately NOT guarded here -- letting it run for real is the
    entire point of this mode.

    Blanking and defanging both preserve the original text's length and
    line numbering exactly as :func:`_refuse_hocon_includes` does (see its
    docstring); a defanged value-position word grows by its two quote
    characters in its own first slot, offset by shrinking its remaining
    slots to the empty string, so nothing else in ``out`` shifts.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    brackets = []  # stack of open '{'/'[' seen so far
    while i < n:
        c = text[i]
        if c in ('"', "#", "$") and _preceded_by_odd_backslashes(text, i):
            last_sig = c
            i += 1
            continue
        if c == '"':
            end, content = _find_hocon_string_end(text, i)
            _decode_unicode_escapes(text, out, i, content)
            i = end
            last_sig = '"'
            continue
        if c == "#":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            prev = text[i - 1] if i > 0 else None
            at_boundary = prev is None or prev in ' \t\r\n{}[],="'
            if at_boundary and not _preceded_by_odd_backslashes(text, i):
                j = i
                while j < n and text[j] not in "\r\n":
                    j += 1
                i = j
                continue
            last_sig = c
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}", i + 2)
            i = (j + 1) if j != -1 else n
            last_sig = "}"
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\n" or c == "\r":
            last_sig = "\n"
            i += 1
            continue
        if c in "{[":
            brackets.append(c)
            last_sig = c
            i += 1
            continue
        if c in "}]":
            if brackets:
                brackets.pop()
            last_sig = c
            i += 1
            continue
        if c.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.upper() != "INCLUDE":
                last_sig = word[-1]
                i = j
                continue

            in_array = bool(brackets) and brackets[-1] == "["
            key_position = (not in_array) and last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + text.count("\r", 0, i) + 1
            line -= text.count("\r\n", 0, i)

            if not key_position:
                # Defang, don't raise: Puppet keeps this as literal text (`include` is
                # special only in statement position in Ruby), but pyhocon takes
                # `include_expr` in value position, caselessly. Quote the bareword.
                if _hocon_directive_follows(text, j):
                    out[i] = '"' + word + '"'
                    for k in range(i + 1, j):
                        out[k] = ""
                last_sig = word[-1]
                i = j
                continue

            if word != "include":
                # Any case but lowercase `include` is never Puppet's directive (Ruby's
                # match is case-sensitive): refuse rather than let pyhocon's caseless
                # grammar read a file. `INCLUDE = 1` is left alone.
                if _hocon_directive_follows(text, j):
                    raise BackendError(
                        "HOCON include directive is not supported here "
                        "(line {})".format(line)
                    )
                last_sig = word[-1]
                i = j
                continue

            after = _skip_hocon_blanks(text, j)
            if after < n and text[after] == '"':
                end, content = _find_hocon_string_end(text, after)
                scheme = _URL_SCHEME_RE.match(content)
                if scheme and scheme.group(0)[:-3].lower() != "file":
                    raise BackendError(
                        "HOCON include of a non-file URL is not "
                        "supported (line {})".format(line)
                    )
                # Puppet's own hocon_data never implements the bare
                # quoted-string form as a real file/URL read -- it
                # always contributes nothing, whatever the target.
                for k in range(i, end):
                    if out[k] not in ("\n", "\r"):
                        out[k] = " "
                i = end
                last_sig = " "
                continue

            if text[after : after + 5] == "file(":
                # Puppet's `include file(...)` reads the file (cwd-relative or
                # absolute): leave the text untouched so pyhocon's own identical
                # resolution runs for real.
                last_sig = word[-1]
                i = j
                continue

            raise BackendError(
                "HOCON include is only supported as a plain quoted "
                "string or file(...); url()/classpath()/required()/"
                "package() and similar forms are not supported "
                "(line {})".format(line)
            )

        last_sig = c
        i += 1

    return "".join(out)
