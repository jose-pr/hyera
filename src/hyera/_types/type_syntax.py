# Ported from Puppet 8 lib/puppet/pops/types/type_parser.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The lexer and recursive-descent reader of Puppet type expressions.

Ports the expression grammar ``type_parser.rb`` reads: access expressions,
hash/array literals, quoted and bare strings, numbers, regex literals and
unary minus. It yields the argument nodes the builders in
:mod:`hyera._types.parser` interpret.
"""

from __future__ import annotations

import re

from ..exceptions import HieraLookupError


class _SyntaxError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


# --------------------------------------------------------------------- lexer

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<farrow>=>)
    | (?P<lbrack>\[)
    | (?P<rbrack>\])
    | (?P<lbrace>\{)
    | (?P<rbrace>\})
    | (?P<comma>,)
    | (?P<minus>-)
    | (?P<dqstring>"(?:\\.|[^"\\])*")
    | (?P<sqstring>'(?:\\.|[^'\\])*')
    | (?P<regex>/(?:\\.|[^/\\])*/)
    | (?P<hex>0[xX][0-9A-Fa-f]+(?!\w))
    | (?P<float>\d+\.\d+(?:[eE][+-]?\d+)?(?!\w)|\d+[eE][+-]?\d+(?!\w))
    | (?P<int>\d+(?!\w))
    | (?P<badnum>\d\w*)
    | (?P<ident>(?:::)?[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"true", "false", "undef", "default"}

#: The deepest nesting of brackets a type expression may have; beyond it the
#: parser would exhaust the interpreter's stack.
_MAX_NESTING = 200


class _Tok:
    __slots__ = ("kind", "text", "start", "end", "ws_before", "value")

    def __init__(self, kind, text, start, end, ws_before, value=None):
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end
        self.ws_before = ws_before
        self.value = value


def _tokenize(text):
    tokens = []
    pos = 0
    n = len(text)
    ws_before = True  # leading whitespace never matters for access-parsing
    while pos < n:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise _SyntaxError(
                "Syntax error at '{}' (line: 1, column: {})".format(text[pos], pos + 1)
            )
        kind = m.lastgroup
        tok_text = m.group()
        start, end = m.start(), m.end()
        if kind == "badnum":
            raise _SyntaxError("Illegal number '{}'".format(tok_text))
        if kind == "ws":
            ws_before = True
            pos = end
            continue
        if kind == "ident":
            body = tok_text.lstrip(":")
            if body and body[0].isupper():
                kind = "qref"
            elif body in _KEYWORDS:
                kind = body
            else:
                kind = "name"
        tokens.append(_Tok(kind, tok_text, start, end, ws_before))
        ws_before = False
        pos = end
    tokens.append(_Tok("eof", "", n, n, False))
    return tokens


def _number_value(tok):
    """The value of an int, hex or float token; an octal literal has a
    leading zero (``010`` is 8), and a float beyond double range is
    rejected the way Puppet's lexer does."""
    text = tok.text
    if tok.kind == "hex":
        return int(text, 16)
    if tok.kind == "int":
        if len(text) > 1 and text[0] == "0":
            try:
                return int(text, 8)
            except ValueError:
                raise _SyntaxError("Illegal number '{}'".format(text)) from None
        return int(text)
    value = float(text)
    if value in (float("inf"), float("-inf")):
        raise HieraLookupError(
            "Internal Error, NUMBER token does not contain a valid number, "
            "{}".format(text)
        )
    return value


class _Parser:
    def __init__(self, text):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0
        self.depth = 0

    def peek(self):
        return self.tokens[self.pos]

    def advance(self):
        tok = self.tokens[self.pos]
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.peek()
        if tok.kind != kind:
            self._error(tok)
        return self.advance()

    def _error(self, tok):
        if tok.kind == "eof":
            raise _SyntaxError("Syntax error at end of input")
        raise _SyntaxError(
            "Syntax error at '{}' (line: 1, column: {})".format(tok.text, tok.start + 1)
        )

    # -- expressions -----------------------------------------------------

    def parse_primary(self):
        self.depth += 1
        try:
            if self.depth > _MAX_NESTING:
                raise HieraLookupError(
                    "The type expression is nested more than {} levels "
                    "deep".format(_MAX_NESTING)
                )
            return self._parse_primary()
        finally:
            self.depth -= 1

    def _parse_primary(self):
        tok = self.peek()
        if tok.kind == "qref":
            self.advance()
            node = ("qref", tok.text, tok.start, tok.end)
            nxt = self.peek()
            if nxt.kind == "lbrack" and not nxt.ws_before:
                self.advance()
                args = self.parse_args()
                rbrack = self.expect("rbrack")
                node = (
                    "access",
                    node,
                    args,
                    tok.start,
                    rbrack.end,
                    self.text[tok.start : rbrack.end],
                )
            return node
        if tok.kind == "name":
            self.advance()
            return ("string", tok.text, tok.start, tok.end)
        if tok.kind in ("true", "false"):
            self.advance()
            return ("bool", tok.kind == "true", tok.start, tok.end)
        if tok.kind == "undef":
            self.advance()
            return ("undef", None, tok.start, tok.end)
        if tok.kind == "default":
            self.advance()
            return ("default", None, tok.start, tok.end)
        if tok.kind == "dqstring":
            self.advance()
            return ("string", _unescape_dq(tok.text[1:-1]), tok.start, tok.end)
        if tok.kind == "sqstring":
            self.advance()
            return ("string", _unescape_sq(tok.text[1:-1]), tok.start, tok.end)
        if tok.kind == "regex":
            self.advance()
            return ("regex", tok.text[1:-1], tok.start, tok.end)
        if tok.kind in ("int", "hex", "float"):
            self.advance()
            return (
                "number",
                _number_value(tok),
                tok.kind == "float",
                tok.start,
                tok.end,
            )
        if tok.kind == "minus":
            self.advance()
            nxt = self.peek()
            if nxt.kind not in ("int", "hex", "float", "minus"):
                self._error(nxt)
            operand = self.parse_primary()
            return ("number", -operand[1], operand[2], tok.start, operand[4])
        if tok.kind == "lbrack":
            self.advance()
            elems = []
            if self.peek().kind != "rbrack":
                elems.append(self.parse_arg_expr())
                while self.peek().kind == "comma":
                    self.advance()
                    if self.peek().kind == "rbrack":
                        break
                    elems.append(self.parse_arg_expr())
            end = self.expect("rbrack")
            return ("array", elems, tok.start, end.end)
        if tok.kind == "lbrace":
            return self.parse_hash()
        self._error(tok)

    def parse_hash(self):
        tok = self.expect("lbrace")
        pairs = []
        if self.peek().kind != "rbrace":
            pairs.append(self.parse_pair())
            while self.peek().kind == "comma":
                self.advance()
                if self.peek().kind == "rbrace":
                    break
                pairs.append(self.parse_pair())
        end = self.expect("rbrace")
        return ("hash", pairs, tok.start, end.end)

    def parse_pair(self):
        # A hash literal's own ``k => v`` is already explicit; parse_arg_expr would greedily collapse
        # a bare ``k => v`` into one pair node, leaving nothing before the expected ``=>``.
        k = self.parse_primary()
        self.expect("farrow")
        v = self.parse_primary()
        return (k, v)

    def parse_arg_expr(self):
        """One access-list argument: a primary, optionally collapsed into a
        bare ``k => v`` pair (Puppet's shorthand for a one-pair hash
        argument, e.g. ``Hash[String => Integer]``, which counts as ONE
        argument towards the type's parameter count)."""
        first = self.parse_primary()
        if self.peek().kind == "farrow":
            self.advance()
            second = self.parse_primary()
            start = first[2]
            end = second[3]
            return ("pair", (first, second), start, end)
        return first

    def parse_args(self):
        args = []
        if self.peek().kind == "rbrack":
            self._error(self.peek())
        args.append(self.parse_arg_expr())
        while self.peek().kind == "comma":
            self.advance()
            if self.peek().kind == "rbrack":
                break
            args.append(self.parse_arg_expr())
        return args


def _unescape_dq(body):
    out = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            nxt = body[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "$": "$"}
            out.append(mapping.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _unescape_sq(body):
    return body.replace("\\'", "'").replace("\\\\", "\\")
