"""Translating a Ruby (Onigmo) regular expression into Python's ``re``."""

from __future__ import annotations

import re
import sys
import warnings

from ..exceptions import HieraLookupError

# ------------------------------------------------------------ Ruby regexes
#
# Ruby (Onigmo) syntax differs from Python's ``re``: ``\A``/``\z``/``\Z``/
# ``\h``, POSIX bracket classes, ``(?m)`` meaning dot-all, ``(?i)`` applying
# only to the rest of its group, ASCII-only ``\w \d \s \b``, and ``^``/``$``
# always matching at line boundaries. ``_ruby_regex`` translates one source in
# a single pass and raises ``HieraLookupError`` for anything it cannot.

_WORD = "A-Za-z0-9_"

#: Ruby escapes inside and outside a class that map to a fixed Python form.
#: ``\w \d \s`` are ASCII in Ruby, while ``\b`` follows Unicode word
#: characters, as Python's does.
_ESCAPE_OUTSIDE = {
    "A": r"\A",
    "z": r"\Z",
    "Z": r"(?=\n?\Z)",
    "h": "[0-9a-fA-F]",
    "H": "[^0-9a-fA-F]",
    "d": "[0-9]",
    "D": "[^0-9]",
    "w": "[" + _WORD + "]",
    "W": "[^" + _WORD + "]",
    "s": r"[ \t\n\r\f\v]",
    "S": r"[^ \t\n\r\f\v]",
    "b": r"\b",
    "B": r"\B",
}
_ESCAPE_INSIDE = {
    "h": "0-9a-fA-F",
    "d": "0-9",
    "w": _WORD,
    "s": r" \t\n\r\f\v",
    "b": r"\x08",
}
#: Escapes that mean the same in Ruby and Python, in or out of a class.
_ESCAPE_SAME = {"f": r"\f", "n": r"\n", "r": r"\r", "t": r"\t", "v": r"\v"}
_ESCAPE_UNSUPPORTED = frozenset("gGKRXpPCMc")
_NEGATED_INSIDE = frozenset("HDWS")

_POSIX = {
    "alpha": "a-zA-Z",
    "digit": "0-9",
    "alnum": "a-zA-Z0-9",
    "upper": "A-Z",
    "lower": "a-z",
    "space": r" \t\n\r\f\v",
    "blank": r" \t",
    "punct": r"!-/:-@\[-`{-~",
    "print": r" -~",
    "graph": r"!-~",
    "cntrl": r"\x00-\x1f\x7f",
    "xdigit": "0-9A-Fa-f",
    "word": _WORD,
    "ascii": r"\x00-\x7f",
}

#: Python ``re`` messages mapped to Ruby's text for the same mistake.
_RUBY_ERROR_TEXT = (
    ("nothing to repeat", "target of repeat operator is not specified"),
    ("bad character range", "empty range in char class"),
)

_HEX = "0123456789abcdefABCDEF"
_POSSESSIVE_NATIVE = sys.version_info >= (3, 11)


def _unsupported(construct, source):
    return HieraLookupError(
        "hyera does not support {} in the Ruby regular expression /{}/".format(
            construct, source
        )
    )


def _ruby_escape(source, i, in_class):
    """Translate the escape at ``source[i]`` (a backslash); returns the Python
    text and the index after the escape."""
    if i + 1 >= len(source):
        raise HieraLookupError("too short escape sequence: /{}/".format(source))
    c = source[i + 1]
    after = i + 2
    if in_class:
        if c in _NEGATED_INSIDE:
            raise _unsupported("\\" + c + " in a character class", source)
        if c in _ESCAPE_INSIDE:
            return _ESCAPE_INSIDE[c], after
    elif c in _ESCAPE_OUTSIDE:
        return _ESCAPE_OUTSIDE[c], after
    if c in _ESCAPE_SAME:
        return _ESCAPE_SAME[c], after
    if c in _ESCAPE_UNSUPPORTED:
        raise _unsupported("\\" + c, source)
    if c == "a":
        return r"\x07", after
    if c == "e":
        return r"\x1b", after
    if c == "x":
        end = after
        while end < len(source) and end < after + 2 and source[end] in _HEX:
            end += 1
        if end == after:
            raise HieraLookupError("invalid hex escape: /{}/".format(source))
        return "\\x{:02x}".format(int(source[after:end], 16)), end
    if c == "u":
        return _unicode_escape(source, after)
    if c == "k" and not in_class:
        m = re.match(r"<([A-Za-z_]\w*)>|'([A-Za-z_]\w*)'", source[after:])
        if not m:
            raise _unsupported("\\k with a numbered or relative reference", source)
        return "(?P={})".format(m.group(1) or m.group(2)), after + m.end()
    if c.isdigit():
        digits = re.match(r"\d+", source[i + 1 :]).group()
        if c != "0" and len(digits) == 1:
            return "\\" + c, after
        octal = re.match(r"[0-7]{1,3}", source[i + 1 :])
        if not octal:
            raise HieraLookupError("invalid backref number/name: /{}/".format(source))
        return "\\x{:02x}".format(int(octal.group(), 8) & 0xFF), i + 1 + octal.end()
    if c.isascii() and c.isalpha():
        return re.escape(c), after
    return "\\" + c if c.isascii() else c, after


def _unicode_escape(source, after):
    """``\\uHHHH`` or ``\\u{H+ H+ ...}`` after the ``\\u``."""
    if source.startswith("{", after):
        close = source.find("}", after)
        digits = source[after + 1 : close].split() if close != -1 else []
        if not digits or any(set(d) - set(_HEX) for d in digits):
            raise HieraLookupError("invalid Unicode escape: /{}/".format(source))
        return "".join("\\U{:08x}".format(int(d, 16)) for d in digits), close + 1
    digits = source[after : after + 4]
    if len(digits) != 4 or set(digits) - set(_HEX):
        raise HieraLookupError("invalid Unicode escape: /{}/".format(source))
    return "\\u" + digits, after + 4


def _ruby_class(source, i):
    """Translate the bracket class opening at ``source[i]``; returns the Python
    class text and the index after its closing ``]``."""
    j = i + 1
    out = ["["]
    if source.startswith("^", j):
        out.append("^")
        j += 1
    start = len(out)
    while True:
        if j >= len(source):
            raise HieraLookupError("premature end of char-class: /{}/".format(source))
        c = source[j]
        if c == "]" and len(out) > start:
            break
        if c == "[":
            m = re.match(r"\[:(\^?)([a-z]+):\]", source[j:])
            if not m:
                raise _unsupported("nested character class", source)
            if m.group(1):
                raise _unsupported("a negated POSIX bracket class", source)
            if m.group(2) not in _POSIX:
                raise HieraLookupError(
                    "invalid POSIX bracket type: /{}/".format(source)
                )
            out.append(_POSIX[m.group(2)])
            j += m.end()
        elif c == "&" and source.startswith("&&", j):
            raise _unsupported("&& in a character class", source)
        elif c == "\\":
            text, j = _ruby_escape(source, j, True)
            out.append(text)
        else:
            out.append("\\]" if c == "]" else c)
            j += 1
    out.append("]")
    return "".join(out), j + 1


_GROUP_FLAGS = re.compile(r"\(\?([a-z]*)(?:-([a-z]*))?([:)])")
_FLAG_TO_PYTHON = {"i": "i", "m": "s", "x": "x"}


def _python_flags(on, off, source):
    if set(on + off) - set("imx"):
        raise HieraLookupError("undefined group option: /{}/".format(source))
    text = "".join(_FLAG_TO_PYTHON[f] for f in on)
    if off:
        text += "-" + "".join(_FLAG_TO_PYTHON[f] for f in off)
    return text


def _ruby_translate(source):
    out = []
    # One entry per open group: [extra parentheses to close with it, x flag].
    frames = [[0, False]]
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c == "\\":
            text, i = _ruby_escape(source, i, False)
            out.append(text)
        elif c == "[":
            text, i = _ruby_class(source, i)
            out.append(text)
        elif c == "(":
            i = _ruby_group(source, i, out, frames)
        elif c == ")":
            if len(frames) == 1:
                raise HieraLookupError(
                    "unmatched close parenthesis: /{}/".format(source)
                )
            out.append(")" * (frames.pop()[0] + 1))
            i += 1
        elif c in "*+?":
            nxt = source[i + 1 : i + 2]
            if nxt == "+":
                if not _POSSESSIVE_NATIVE:
                    raise _unsupported("a possessive quantifier", source)
                out.append(c + "+")
                i += 2
            elif nxt == "?":
                out.append(c + "?")
                i += 2
            elif nxt == "*":
                raise _unsupported("a nested repeat operator", source)
            else:
                out.append(c)
                i += 1
        elif c == "#" and frames[-1][1]:
            end = source.find("\n", i)
            end = n if end == -1 else end + 1
            # A comment ends at its newline; one cut short by the end of the
            # source must not swallow the parentheses closed after it.
            out.append(source[i:end].rstrip("\n") + "\n")
            i = end
        else:
            out.append(c)
            i += 1
    if len(frames) > 1:
        raise HieraLookupError(
            "end pattern with unmatched parenthesis: /{}/".format(source)
        )
    out.append(")" * frames[0][0])
    return "".join(out)


def _ruby_group(source, i, out, frames):
    """Translate the group opening at ``source[i]``, pushing a frame when it
    holds a body; returns the index after the opening."""
    if not source.startswith("(?", i):
        out.append("(")
        frames.append([0, frames[-1][1]])
        return i + 1
    kind = source[i + 2 : i + 3]
    if kind == "#":
        end = source.find(")", i)
        if end == -1:
            raise HieraLookupError(
                "end pattern with unmatched parenthesis: /{}/".format(source)
            )
        return end + 1
    if kind in (":", "=", "!"):
        out.append("(?" + kind)
        frames.append([0, frames[-1][1]])
        return i + 3
    if kind == ">":
        if not _POSSESSIVE_NATIVE:
            raise _unsupported("an atomic group", source)
        out.append("(?>")
        frames.append([0, frames[-1][1]])
        return i + 3
    if kind == "<" and source[i + 3 : i + 4] in ("=", "!"):
        out.append("(?<" + source[i + 3])
        frames.append([0, frames[-1][1]])
        return i + 4
    if kind in ("<", "'"):
        m = re.match(r"\(\?(?:<([A-Za-z_]\w*)>|'([A-Za-z_]\w*)')", source[i:])
        if not m:
            name = re.match(r"[^>')]*", source[i + 3 :]).group()
            raise HieraLookupError("invalid group name <{}>: /{}/".format(name, source))
        out.append("(?P<{}>".format(m.group(1) or m.group(2)))
        frames.append([0, frames[-1][1]])
        return i + m.end()
    if kind in ("~", "("):
        raise _unsupported("(?" + kind, source)
    m = _GROUP_FLAGS.match(source, i)
    if not m:
        raise HieraLookupError("undefined group option: /{}/".format(source))
    on, off, terminator = m.group(1), m.group(2) or "", m.group(3)
    flags = _python_flags(on, off, source)
    x_flag = frames[-1][1]
    if "x" in on:
        x_flag = True
    if "x" in off:
        x_flag = False
    out.append("(?" + flags + ":")
    if terminator == ")":
        # ``(?i)`` applies to the rest of the enclosing group: scope it there.
        frames[-1][0] += 1
        frames[-1][1] = x_flag
    else:
        frames.append([0, x_flag])
    return m.end()


def _ruby_regex(source):
    """Compile a Ruby regex source into a Python ``re.Pattern``.

    :raises HieraLookupError: the source is invalid in Ruby, or uses a Ruby
        construct with no Python translation (the message names it).
    """
    translated = _ruby_translate(source)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return re.compile(translated, re.MULTILINE)
    except re.error as e:
        text = next((r for p, r in _RUBY_ERROR_TEXT if e.msg.startswith(p)), e.msg)
        raise HieraLookupError("{}: /{}/".format(text, source)) from None
