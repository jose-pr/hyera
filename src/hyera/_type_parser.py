"""Puppet type-expression parser: ``parse_type``.

Ports the subset of ``type_parser.rb`` (``:37-46`` ``parse``, ``:101-640``
``interpret*``) this project's type tiers need, over a hand-written lexer
and recursive-descent reader for the expression shapes Puppet's type
grammar actually uses: access expressions
(``Name[args]``), hash/array literals, quoted and bare strings, numbers,
regex literals, and unary minus.
"""

import functools
import re

from .exceptions import HieraLookupError
from ._types import (
    ALIASES,
    ANY,
    BOOLEAN,
    COLLECTION,
    NAMED_ONLY_TYPES,
    NUMERIC,
    REGEXP,
    SCALAR,
    SCALAR_DATA,
    UNDEF,
    PArrayType,
    PBooleanType,
    PCollectionType,
    PEnumType,
    PFloatType,
    PHashType,
    PIntegerType,
    PNotUndefType,
    POptionalType,
    PPatternType,
    PRegexpType,
    PSensitiveType,
    PStringType,
    PStructElement,
    PStructType,
    PTupleType,
    PTypeReferenceType,
    PVariantType,
    _PNamedType,
)

__all__ = ["parse_type"]

#: Names never accepted with parameters, whether or not they are otherwise
#: parameterizable elsewhere (``type_parser.rb`` ``when 'any', 'data', ...``).
_NEVER_PARAMETERIZED = frozenset(
    [
        "any",
        "data",
        "catalogentry",
        "scalar",
        "undef",
        "numeric",
        "default",
        "semverrange",
    ]
)

#: The third tier: never modeled at all, bare or
#: parameterized.
_UNSUPPORTED_NAMES = frozenset(["iterable", "iterator", "init", "unit"])

#: Bare (zero-argument) type names -> a constructor taking no arguments.
_BARE_TYPES = {
    "any": lambda: ANY,
    "undef": lambda: UNDEF,
    "notundef": lambda: PNotUndefType(),
    "optional": lambda: POptionalType(),
    "scalar": lambda: SCALAR,
    "scalardata": lambda: SCALAR_DATA,
    "string": lambda: PStringType.DEFAULT,
    "integer": lambda: PIntegerType.DEFAULT,
    "float": lambda: PFloatType.DEFAULT,
    "numeric": lambda: NUMERIC,
    "boolean": lambda: BOOLEAN,
    "array": lambda: PArrayType(),
    "hash": lambda: PHashType(),
    "collection": lambda: COLLECTION,
    "tuple": lambda: PTupleType([]),
    "struct": lambda: PStructType([]),
    "variant": lambda: PVariantType([]),
    "sensitive": lambda: PSensitiveType(),
    "regexp": lambda: REGEXP,
}
#: Names whose Puppet-cased spelling ``str.capitalize()`` gets wrong.
_PRETTY_NAMES = {
    "uri": "URI",
    "semver": "SemVer",
    "semverrange": "SemVerRange",
    "typeset": "TypeSet",
    "catalogentry": "CatalogEntry",
    "notundef": "NotUndef",
    "scalardata": "ScalarData",
}
for _n in NAMED_ONLY_TYPES:
    _pretty_n = _PRETTY_NAMES.get(_n, _n.capitalize())
    _BARE_TYPES.setdefault(_n, (lambda n: (lambda: _PNamedType(n)))(_pretty_n))


class _NotAValidTypeSpec(Exception):
    """Raised anywhere interpretation finds the parsed expression is not
    type-shaped; caught once at the top to format Puppet's message against
    the *original, untrimmed* input text."""


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
    | (?P<float>\d+\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)
    | (?P<int>\d+)
    | (?P<ident>(?:::)?[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)
    """,
    re.VERBOSE,
)

_KEYWORDS = {"true", "false", "undef", "default"}


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


def _describe(tok):
    if tok.kind == "eof":
        return "end of input"
    return "'{}'".format(tok.text)


class _Parser:
    def __init__(self, text):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0

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
        tok = self.peek()
        if tok.kind == "qref":
            self.advance()
            node = ("qref", tok.text, tok.start, tok.end)
            nxt = self.peek()
            if nxt.kind == "lbrack" and not nxt.ws_before:
                self.advance()
                args = self.parse_args()
                rbrack = self.expect("rbrack")
                node = ("access", node, args, tok.start, rbrack.end)
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
        if tok.kind == "int":
            self.advance()
            return ("number", int(tok.text), False, tok.start, tok.end)
        if tok.kind == "float":
            self.advance()
            return ("number", float(tok.text), True, tok.start, tok.end)
        if tok.kind == "minus":
            self.advance()
            nxt = self.peek()
            if nxt.kind not in ("int", "float"):
                self._error(nxt)
            self.advance()
            is_float = nxt.kind == "float"
            value = -(float(nxt.text) if is_float else int(nxt.text))
            return ("number", value, is_float, tok.start, nxt.end)
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
        # A hash literal's own ``k => v`` is already explicit; using
        # parse_arg_expr (which greedily collapses a *bare* ``k => v`` into
        # one pair-argument, for the access-list shorthand) here would eat
        # the key AND the value as a single "pair" node, leaving nothing
        # before the expected ``=>``.
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


# ---------------------------------------------------------------- interpret


def _interp_type(node):
    """Interpret ``node`` as a type expression (recursive)."""
    kind = node[0]
    if kind == "qref":
        return _interp_qref(node)
    if kind == "access":
        return _interp_access(node)
    raise _NotAValidTypeSpec()


def _interp_qref(node):
    _, text, start, end = node
    name = text.lstrip(":").lower()
    if name in _UNSUPPORTED_NAMES:
        raise HieraLookupError(
            "hiera does not support the Puppet type '{}'".format(text)
        )
    if name in ALIASES:
        return ALIASES[name]
    if name in _BARE_TYPES:
        return _BARE_TYPES[name]()
    return PTypeReferenceType(text)


def _pretty(name_lower):
    return _PRETTY_NAMES.get(name_lower, name_lower[:1].upper() + name_lower[1:])


def _interp_access(node):
    _, base, args, start, end = node
    _, base_text, _, _ = base
    name = base_text.lstrip(":").lower()

    if name in _NEVER_PARAMETERIZED or name in ALIASES:
        raise HieraLookupError("Not a parameterized type <{}>".format(_pretty(name)))
    if name in _UNSUPPORTED_NAMES or name in NAMED_ONLY_TYPES:
        raise HieraLookupError(
            "hiera does not support the Puppet type '{}'".format(node_text(start, end))
        )

    builder = _ACCESS_BUILDERS.get(name)
    if builder is not None:
        return builder(args)
    # Unknown/unmodeled name with parameters: the whole access span becomes
    # the TypeReference text (parent Q2/Q5).
    return PTypeReferenceType(node_text(start, end))


#: Set by :func:`parse_type` for the duration of one parse, so the
#: (recursive) interpreters can slice the original source for a
#: TypeReference/unsupported-type span without threading it as an argument
#: through every builder.
node_text = None


def _num_or_default(node):
    kind = node[0]
    if kind == "default":
        return None
    if kind == "number":
        return node[1]
    raise _NotAValidTypeSpec()


def _check_range(a, b):
    if a is not None and b is not None and a > b:
        raise HieraLookupError(
            "'from' must be less or equal to 'to'. Got ({}, {}".format(
                _fmt_num(a), _fmt_num(b)
            )
        )


def _fmt_num(x):
    if isinstance(x, float) and x == int(x):
        return str(int(x))
    return str(x)


def _int_or_default(node):
    v = _num_or_default(node)
    if v is not None and not isinstance(v, int):
        raise _NotAValidTypeSpec()
    return v


def _build_integer(args):
    if len(args) not in (1, 2):
        raise HieraLookupError(
            "Invalid number of type parameters specified: Integer requires 1 or 2, {} provided".format(
                len(args)
            )
        )
    from_ = _int_or_default(args[0])
    to = _int_or_default(args[1]) if len(args) == 2 else None
    _check_range(from_, to)
    return PIntegerType(from_, to)


def _build_float(args):
    if len(args) not in (1, 2):
        raise HieraLookupError(
            "Invalid number of type parameters specified: Float requires 1 or 2, {} provided".format(
                len(args)
            )
        )
    from_ = _num_or_default(args[0])
    to = _num_or_default(args[1]) if len(args) == 2 else None
    from_ = float(from_) if from_ is not None else None
    to = float(to) if to is not None else None
    _check_range(from_, to)
    return PFloatType(from_, to)


def _build_string(args):
    if len(args) not in (1, 2):
        raise HieraLookupError(
            "Invalid number of type parameters specified: String requires 1 to 2, {} provided".format(
                len(args)
            )
        )
    if len(args) == 1:
        a = _num_or_default(args[0])
        return PStringType(a, None)
    a = _num_or_default(args[0])
    b = _num_or_default(args[1])
    _check_range(a, b)
    return PStringType(a, b)


def _build_boolean(args):
    if len(args) != 1:
        raise HieraLookupError(
            "'new_boolean' expects 1 argument, got {}".format(len(args))
        )
    node = args[0]
    if node[0] == "bool":
        return PBooleanType(node[1])
    raise HieraLookupError("Boolean parameter must be true or false")


def _build_array(args):
    if len(args) > 3:
        raise HieraLookupError(
            "Invalid number of type parameters specified: Array requires 0 to 3, {} provided".format(
                len(args)
            )
        )
    if not args:
        return PArrayType()
    if args[0][0] in ("qref", "access"):
        elem = _interp_type(args[0])
        size_args = args[1:]
    else:
        elem = None
        size_args = args
    if len(size_args) > 2:
        raise _NotAValidTypeSpec()
    if not size_args:
        return PArrayType(elem)
    if len(size_args) == 1:
        a = _num_or_default(size_args[0])
        return PArrayType(elem, a, None)
    a = _num_or_default(size_args[0])
    b = _num_or_default(size_args[1])
    _check_range(a, b)
    return PArrayType(elem, a, b)


def _build_hash(args):
    if len(args) not in (2, 3, 4):
        raise HieraLookupError(
            "Invalid number of type parameters specified: Hash requires 2 to 4, {} provided".format(
                len(args)
            )
        )
    key = _interp_type(args[0])
    val = _interp_type(args[1])
    size_args = args[2:]
    if not size_args:
        return PHashType(key, val)
    if len(size_args) == 1:
        a = _num_or_default(size_args[0])
        return PHashType(key, val, a, None)
    a = _num_or_default(size_args[0])
    b = _num_or_default(size_args[1])
    _check_range(a, b)
    return PHashType(key, val, a, b)


def _build_collection(args):
    if len(args) > 2:
        raise _NotAValidTypeSpec()
    if not args:
        return COLLECTION
    if len(args) == 1:
        a = _num_or_default(args[0])
        return PCollectionType(a, None)
    a = _num_or_default(args[0])
    b = _num_or_default(args[1])
    _check_range(a, b)
    return PCollectionType(a, b)


def _build_tuple(args):
    if not args:
        raise _NotAValidTypeSpec()
    types = list(args)
    size_from = size_to = None
    if len(types) >= 1 and types[-1][0] == "number":
        if len(types) >= 2 and types[-2][0] == "number":
            size_to = _num_or_default(types.pop())
            size_from = _num_or_default(types.pop())
        else:
            size_from = _num_or_default(types.pop())
        _check_range(size_from, size_to)
    if not types:
        raise _NotAValidTypeSpec()
    interpreted = [_interp_type(t) for t in types]
    return PTupleType(interpreted, size_from, size_to)


def _build_struct(args):
    if len(args) != 1 or args[0][0] != "hash":
        raise _NotAValidTypeSpec()
    _, pairs, _, _ = args[0]
    elements = []
    for k_node, v_node in pairs:
        key, optional = _struct_key(k_node)
        value_type = _interp_type(v_node)
        elements.append(PStructElement(key, optional, value_type))
    return PStructType(elements)


def _struct_key(node):
    if node[0] == "string":
        return node[1], False
    if node[0] == "access":
        _, base, args, _, _ = node
        base_name = base[1].lstrip(":").lower()
        if base_name == "optional" and len(args) == 1 and args[0][0] == "string":
            return args[0][1], True
    raise _NotAValidTypeSpec()


def _build_variant(args):
    if not args:
        raise _NotAValidTypeSpec()
    return PVariantType([_interp_type(a) for a in args])


def _build_enum(args):
    if not args:
        raise _NotAValidTypeSpec()
    values = []
    for a in args:
        if a[0] == "string":
            values.append(a[1])
        elif a[0] == "bool":
            values.append(a[1])
        else:
            raise _NotAValidTypeSpec()
    return PEnumType(values)


def _build_pattern(args):
    sources = []
    for a in args:
        if a[0] == "regex":
            sources.append(a[1])
        elif a[0] == "string":
            # A quoted string argument is itself a regex *source*, not a
            # literal to escape (``Pattern['^a']`` is ``Pattern[/^a/]``).
            sources.append(a[1])
        else:
            raise _NotAValidTypeSpec()
    return PPatternType(sources)


def _build_regexp(args):
    if len(args) > 1:
        raise _NotAValidTypeSpec()
    if not args:
        return REGEXP
    a = args[0]
    if a[0] == "regex":
        return PRegexpType(a[1])
    if a[0] == "string":
        return PRegexpType(a[1])
    raise _NotAValidTypeSpec()


def _build_sensitive(args):
    if len(args) > 1:
        raise HieraLookupError(
            "Invalid number of type parameters specified: Sensitive requires 0 to 1, {} provided".format(
                len(args)
            )
        )
    if not args:
        return PSensitiveType()
    return PSensitiveType(_interp_type(args[0]))


def _literal_or_type(node):
    """Optional/NotUndef's argument: a bareword/quoted string stays a
    literal (``assert_type unless param.is_a?(String)``); anything else must
    be a type expression."""
    if node[0] == "string":
        return node[1]
    return _interp_type(node)


def _build_optional(args):
    if len(args) != 1:
        raise HieraLookupError(
            "Invalid number of type parameters specified: Optional requires 1, {} provided".format(
                len(args)
            )
        )
    return POptionalType(_literal_or_type(args[0]))


def _build_notundef(args):
    if len(args) > 1:
        raise HieraLookupError(
            "Invalid number of type parameters specified: NotUndef requires 0 to 1, {} provided".format(
                len(args)
            )
        )
    if not args:
        return PNotUndefType()
    return PNotUndefType(_literal_or_type(args[0]))


_ACCESS_BUILDERS = {
    "integer": _build_integer,
    "float": _build_float,
    "string": _build_string,
    "boolean": _build_boolean,
    "array": _build_array,
    "hash": _build_hash,
    "collection": _build_collection,
    "tuple": _build_tuple,
    "struct": _build_struct,
    "variant": _build_variant,
    "enum": _build_enum,
    "pattern": _build_pattern,
    "regexp": _build_regexp,
    "sensitive": _build_sensitive,
    "optional": _build_optional,
    "notundef": _build_notundef,
}


@functools.lru_cache(maxsize=512)
def parse_type(text):
    """Parse a Puppet type-expression string into a type instance.

    Raises :class:`hyera.HieraLookupError` with Puppet's own parser text on
    a syntax error, an unsupported construct (one of the
    unsupported types), or a top-level expression that is not type-shaped.
    """
    global node_text
    try:
        if not text.strip():
            # An empty program is zero statements, not a syntax error --
            # it just isn't type-shaped either.
            raise _NotAValidTypeSpec()
        parser = _Parser(text)
        node_text = lambda s, e: text[s:e]  # noqa: E731
        try:
            expr = parser.parse_primary()
            if parser.peek().kind != "eof":
                # Leftover input: only a *second*, independently valid
                # expression (e.g. "Integer [1]" is a bare Integer followed
                # by a separate array literal) makes this the generic
                # not-a-type-spec case; a genuinely malformed tail (a stray
                # "]", say) is its own syntax error and must propagate as
                # one, not be swallowed into the generic message. A bare
                # trailing comma ("Integer,") reads as Puppet expecting a
                # continuation list, not a stray token -- with nothing
                # after it, that surfaces as "end of input", same as any
                # other primary hitting EOF.
                if parser.peek().kind == "comma":
                    parser.advance()
                parser.parse_primary()
                raise _NotAValidTypeSpec()
        except _SyntaxError as e:
            raise HieraLookupError(e.message)
        if expr[0] not in ("qref", "access"):
            raise _NotAValidTypeSpec()
        return _interp_type(expr)
    except _NotAValidTypeSpec:
        raise HieraLookupError(
            "The expression <{}> is not a valid type specification.".format(text)
        )
