# Ported from Puppet 8 lib/puppet/pops/types/types.rb, type_calculator.rb,
# type_formatter.rb, p_sensitive_type.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Puppet type system: the type model, Sensitive, convert_to.

Ports Puppet's ``pops/types`` (``types.rb``, ``type_calculator.rb``,
``type_formatter.rb``, ``p_sensitive_type.rb``). This module is a leaf
(imports only :mod:`hyera.exceptions`); :mod:`hyera._types.parser` builds
type instances from a Puppet type-expression string via ``parse_type``.

Each class here is Puppet's own type-expression name (``Any``, ``Integer``,
``Optional``, ...) rather than Ruby's ``P<Name>Type`` (``PAnyType``,
``PIntegerType``, ``POptionalType``, ...); the one exception is
:class:`SensitiveType`, kept out of Ruby's bare ``Sensitive`` name because
:class:`hyera.Sensitive` (this module's own value wrapper) already has it.
The classes stay private.
"""

import re
import sys
import typing as _ty
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
        "hiera does not support {} in the Ruby regular expression /{}/".format(
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


def puppet_double_quote(s):
    """Puppet's double-quoted string literal (``puppet_double_quote``)."""
    out = ['"']
    mapping = {
        0x09: "\\t",
        0x0A: "\\n",
        0x0D: "\\r",
        0x22: '\\"',
        0x24: "\\$",
        0x5C: "\\\\",
    }
    for c in s:
        cp = ord(c)
        if cp in mapping:
            out.append(mapping[cp])
        elif cp < 0x20:
            out.append("\\u{{{:X}}}".format(cp))
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def puppet_quote(s, enforce_double_quotes=False):
    """Puppet's single-quoted string literal, falling back to double quotes
    when ``s`` holds a control character (``string_converter.rb`` ``puppet_quote``)."""
    s = str(s)
    if enforce_double_quotes or any(ord(c) < 0x20 for c in s):
        return puppet_double_quote(s)
    out = ["'"]
    escaped = False
    for c in s:
        cp = ord(c)
        if escaped:
            out.append("\\")
            out.append(c)
            escaped = False
        elif cp == 0x27:
            out.append("\\'")
        elif cp == 0x5C:
            escaped = True
        else:
            out.append(c)
    if escaped:
        out.append("\\")
    out.append("'")
    return "".join(out)


def _num_str(value):
    """Render a range bound: ``None`` -> ``"default"``, else Puppet's number
    literal form (a float always shows at least one decimal digit)."""
    if value is None:
        return "default"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return repr(value)
        if value == int(value) and abs(value) < 1e16:
            return "%.1f" % value
        text = repr(value)
        if "e" in text and "." not in text.split("e")[0]:
            mantissa, exp = text.split("e")
            text = mantissa + ".0e" + exp
        return text
    return str(value)


def _literal_str(value):
    """Puppet's literal rendering of a value used as a bareword type
    argument (Enum values, a struct key's literal form, etc.)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return puppet_quote(value)
    if isinstance(value, float):
        return _num_str(value)
    return str(value)


def _render_size_args(from_, to_):
    """The trailing size-constraint arguments for a rendered type, e.g.
    ``Array[String, 1]`` or ``Array[String, 1, 3]``. Empty when unconstrained."""
    if from_ is None and to_ is None:
        return []
    if to_ is None:
        return [_num_str(from_)]
    return [_num_str(from_), _num_str(to_)]


class _Sealing(type):
    """Metaclass of the type model: an instance is read-only once its
    constructor returns, so a shared (cached) type object cannot be changed
    through any reference to it."""

    def __call__(cls, *args: _ty.Any, **kwargs: _ty.Any) -> _ty.Any:
        obj = super().__call__(*args, **kwargs)
        object.__setattr__(obj, "_sealed", True)
        return obj


class Any(metaclass=_Sealing):
    """Base of the ported Puppet type model (``types.rb``)."""

    __slots__ = ("_sealed",)

    def __setattr__(self, name: str, value: _ty.Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("type objects are immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("type objects are immutable")

    #: Puppet class name, without the leading ``P``/trailing ``Type``.
    TYPE_NAME = "Any"

    def instance(self, value: _ty.Any) -> bool:
        """Whether ``value`` is a Puppet instance of this type."""
        return True

    def generalize(self) -> "Any":
        """This type with any literal narrowing removed (e.g. a literal
        ``String`` loses its ``.literal``), Puppet's own ``generalize``."""
        return self

    @property
    def name(self) -> str:
        """This type's own Puppet name, with any parameters."""
        return self.TYPE_NAME

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        """Puppet's own type text, the same as :meth:`__str__`
        (``Integer[1, 3]``), so a type object reads back as what it means."""
        return str(self)

    def _key(self):
        return ()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Any):
            return NotImplemented
        return type(self) is type(other) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash((type(self), self._key()))

    def __instancecheck__(self, value: _ty.Any) -> bool:
        """Lets a type *object* stand in directly as ``isinstance()``'s
        second argument (``isinstance(5, Integer[1, 3])``): defined on the
        class body, so it is found via ``type(<this instance>).
        __instancecheck__`` -- Python's normal dunder lookup for the
        instance used as the ``isinstance`` class argument -- without
        touching how ``isinstance(x, Integer)`` (the bare *class*) resolves,
        which still goes through ``type(Integer).__instancecheck__`` (the
        builtin ``type.__instancecheck__``, untouched). See
        :mod:`hyera.types`."""
        return self.instance(value)

    def __call__(self, *args: _ty.Any) -> _ty.Any:
        """A type *object* called directly (``Integer[1, 10]("5")``) is
        Puppet's ``new()`` against this exact type, asserting any
        parameters (a range, a size) the same way `.new()` on the bare type
        would. Importing :mod:`hyera._types.new_function` lazily avoids a
        circular import (it imports this module to dispatch on these
        classes). See :mod:`hyera.types`, whose facade classes delegate a
        *bare* call (``Integer("5")``) to this same method on their own
        default type object."""
        from .new_function import new_instance

        return new_instance(self, *args)


class Undef(Any):
    __slots__ = ()
    TYPE_NAME = "Undef"

    def instance(self, value):
        return value is None


class NotUndef(Any):
    __slots__ = ("contained",)
    TYPE_NAME = "NotUndef"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if value is None:
            return False
        if self.contained is None:
            return True
        return _type_instance(self.contained, value)

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("NotUndef", self.contained, show_literal=True)


class Optional(Any):
    __slots__ = ("contained",)
    TYPE_NAME = "Optional"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if value is None:
            return True
        if self.contained is None:
            return False
        return _type_instance(self.contained, value)

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("Optional", self.contained, show_literal=True)


def _is_nan(value):
    return isinstance(value, float) and value != value


class Scalar(Any):
    __slots__ = ()
    TYPE_NAME = "Scalar"

    def instance(self, value):
        return isinstance(value, (bool, int, float, str, re.Pattern))


class ScalarData(Scalar):
    __slots__ = ()
    TYPE_NAME = "ScalarData"

    def instance(self, value):
        return isinstance(value, (bool, int, float, str))


class Numeric(Any):
    __slots__ = ()
    TYPE_NAME = "Numeric"

    def instance(self, value):
        if isinstance(value, bool) or _is_nan(value):
            return False
        return isinstance(value, (int, float))


class Integer(Any):
    __slots__ = ("from_", "to")
    TYPE_NAME = "Integer"

    def __init__(self, from_=None, to=None):
        self.from_ = from_
        self.to = to

    DEFAULT = None  # set below

    def instance(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if self.from_ is not None and value < self.from_:
            return False
        if self.to is not None and value > self.to:
            return False
        return True

    def generalize(self):
        return Integer.DEFAULT

    def _key(self):
        return (self.from_, self.to)

    def __str__(self):
        if self.from_ is None and self.to is None:
            return "Integer"
        if self.to is None:
            return "Integer[{}]".format(_num_str(self.from_))
        return "Integer[{}, {}]".format(_num_str(self.from_), _num_str(self.to))


class Float(Any):
    __slots__ = ("from_", "to")
    TYPE_NAME = "Float"

    def __init__(self, from_=None, to=None):
        self.from_ = from_
        self.to = to

    DEFAULT = None  # set below

    def instance(self, value):
        if not isinstance(value, float) or value != value:
            return False
        if self.from_ is not None and value < self.from_:
            return False
        if self.to is not None and value > self.to:
            return False
        return True

    def generalize(self):
        return Float.DEFAULT

    def _key(self):
        return (self.from_, self.to)

    def __str__(self):
        if self.from_ is None and self.to is None:
            return "Float"
        if self.to is None:
            return "Float[{}]".format(_num_str(self.from_))
        return "Float[{}, {}]".format(_num_str(self.from_), _num_str(self.to))


class String(Any):
    __slots__ = ("size_from", "size_to", "literal")
    TYPE_NAME = "String"

    def __init__(self, size_from=None, size_to=None, literal=None):
        self.size_from = size_from
        self.size_to = size_to
        #: set only for the "value" flavor (``TypeFactory.string(literal)``),
        #: used by Optional/NotUndef to render the raw literal (see
        #: ``_render_container``); everywhere else it renders as bare
        #: ``String`` (``string_PStringType`` never shows ``.value``
        #: outside Puppet's debug formatter).
        self.literal = literal

    DEFAULT = None  # set below

    def instance(self, value):
        if not isinstance(value, str):
            return False
        if self.literal is not None:
            return value == self.literal
        if self.size_from is None and self.size_to is None:
            return True
        n = len(value)
        if self.size_from is not None and n < self.size_from:
            return False
        if self.size_to is not None and n > self.size_to:
            return False
        return True

    def generalize(self):
        return String.DEFAULT

    def _key(self):
        return (self.size_from, self.size_to, self.literal)

    def __str__(self):
        if self.literal is not None:
            return "String"
        if self.size_from is None and self.size_to is None:
            return "String"
        if self.size_to is None:
            return "String[{}]".format(_num_str(self.size_from))
        return "String[{}, {}]".format(_num_str(self.size_from), _num_str(self.size_to))


class Boolean(Any):
    __slots__ = ("value",)
    TYPE_NAME = "Boolean"

    def __init__(self, value=None):
        #: ``None`` = unconstrained; ``True``/``False`` = exactly that value
        #: (``Boolean.new(true)``/``new(false)``, what ``infer()``
        #: gives a literal ``bool``).
        self.value = value

    def instance(self, value):
        if not isinstance(value, bool):
            return False
        return self.value is None or value == self.value

    def generalize(self):
        return BOOLEAN

    def _key(self):
        return (self.value,)

    def __str__(self):
        if self.value is None:
            return "Boolean"
        return "Boolean[{}]".format("true" if self.value else "false")


class Regexp(Any):
    __slots__ = ("source",)
    TYPE_NAME = "Regexp"

    def __init__(self, source=None):
        self.source = source

    def instance(self, value):
        if not isinstance(value, re.Pattern):
            return False
        if self.source is None:
            return True
        return value.pattern == self.source

    def _key(self):
        return (self.source,)

    def __str__(self):
        if self.source is None:
            return "Regexp"
        return "Regexp[/{}/]".format(self.source)


class Pattern(Any):
    __slots__ = ("sources", "_compiled")
    TYPE_NAME = "Pattern"

    def __init__(self, sources):
        self.sources = tuple(dict.fromkeys(sources))
        self._compiled = tuple(_ruby_regex(s) for s in self.sources)

    def instance(self, value):
        if not isinstance(value, str):
            return False
        if not self._compiled:
            return True
        return any(p.search(value) for p in self._compiled)

    def _key(self):
        return tuple(self.sources)

    def __str__(self):
        if not self.sources:
            return "Pattern"
        return "Pattern[{}]".format(", ".join("/{}/".format(s) for s in self.sources))


_ASCII_FOLD = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


class Enum(Any):
    __slots__ = ("values", "case_insensitive")
    TYPE_NAME = "Enum"

    def __init__(self, values, case_insensitive=False):
        self.values = tuple(dict.fromkeys(values))
        self.case_insensitive = bool(case_insensitive)

    def instance(self, value):
        if not isinstance(value, str):
            return False
        if not self.values:
            return True
        if self.case_insensitive:
            folded = value.translate(_ASCII_FOLD)
            return any(v.translate(_ASCII_FOLD) == folded for v in self.values)
        return value in self.values

    def _key(self):
        return (tuple(self.values), self.case_insensitive)

    def __str__(self):
        if not self.values:
            return "Enum"
        parts = [_literal_str(v) for v in self.values]
        if self.case_insensitive:
            parts.append("true")
        return "Enum[{}]".format(", ".join(parts))


class Collection(Any):
    __slots__ = ("size_from", "size_to")
    TYPE_NAME = "Collection"

    def __init__(self, size_from=None, size_to=None):
        self.size_from = size_from
        self.size_to = size_to

    def instance(self, value):
        if not isinstance(value, (list, dict)):
            return False
        n = len(value)
        if self.size_from is not None and n < self.size_from:
            return False
        if self.size_to is not None and n > self.size_to:
            return False
        return True

    def generalize(self):
        return COLLECTION

    def _key(self):
        return (self.size_from, self.size_to)

    def __str__(self):
        args = _render_size_args(self.size_from, self.size_to)
        if not args:
            return "Collection"
        return "Collection[{}]".format(", ".join(args))


class Array(Any):
    __slots__ = ("element_type", "size_from", "size_to")
    TYPE_NAME = "Array"

    def __init__(self, element_type=None, size_from=None, size_to=None):
        self.element_type = element_type
        self.size_from = size_from
        self.size_to = size_to

    def instance(self, value):
        if not isinstance(value, (list, tuple)):
            return False
        n = len(value)
        if self.size_from is not None and n < self.size_from:
            return False
        if self.size_to is not None and n > self.size_to:
            return False
        if self.element_type is None:
            return True
        return all(_type_instance(self.element_type, v) for v in value)

    def generalize(self):
        elem = generalize(self.element_type) if self.element_type is not None else None
        return Array(elem)

    def _key(self):
        return (_key_of(self.element_type), self.size_from, self.size_to)

    def __str__(self):
        if self.size_from == 0 and self.size_to == 0:
            return "Array[0, 0]"
        if (
            self.element_type is None
            and self.size_from is None
            and self.size_to is None
        ):
            return "Array"
        parts = [str(self.element_type) if self.element_type is not None else "Any"]
        parts += _render_size_args(self.size_from, self.size_to)
        return "Array[{}]".format(", ".join(parts))


class Hash(Any):
    __slots__ = ("key_type", "value_type", "size_from", "size_to")
    TYPE_NAME = "Hash"

    def __init__(self, key_type=None, value_type=None, size_from=None, size_to=None):
        self.key_type = key_type
        self.value_type = value_type
        self.size_from = size_from
        self.size_to = size_to

    def instance(self, value):
        if not isinstance(value, dict):
            return False
        n = len(value)
        if self.size_from is not None and n < self.size_from:
            return False
        if self.size_to is not None and n > self.size_to:
            return False
        for k, v in value.items():
            if self.key_type is not None and not _type_instance(self.key_type, k):
                return False
            if self.value_type is not None and not _type_instance(self.value_type, v):
                return False
        return True

    def generalize(self):
        key = generalize(self.key_type) if self.key_type is not None else None
        val = generalize(self.value_type) if self.value_type is not None else None
        return Hash(key, val)

    def _key(self):
        return (
            _key_of(self.key_type),
            _key_of(self.value_type),
            self.size_from,
            self.size_to,
        )

    def __str__(self):
        if self.size_from == 0 and self.size_to == 0:
            return "Hash[0, 0]"
        if (
            self.key_type is None
            and self.value_type is None
            and self.size_from is None
            and self.size_to is None
        ):
            return "Hash"
        parts = [
            str(self.key_type) if self.key_type is not None else "Any",
            str(self.value_type) if self.value_type is not None else "Any",
        ]
        parts += _render_size_args(self.size_from, self.size_to)
        return "Hash[{}]".format(", ".join(parts))


class Tuple(Any):
    __slots__ = ("types", "size_from", "size_to")
    TYPE_NAME = "Tuple"

    def __init__(self, types, size_from=None, size_to=None):
        self.types = tuple(types)
        self.size_from = size_from
        self.size_to = size_to

    def _bounds(self):
        """The accepted element counts ``(min, max)``; ``max`` is ``None``
        when unbounded (no types at all, or a size with only a minimum)."""
        n = len(self.types)
        if self.size_from is None and self.size_to is None:
            return (n, n) if n else (0, None)
        return (self.size_from or 0), self.size_to

    def instance(self, value):
        if not isinstance(value, (list, tuple)):
            return False
        lo, hi = self._bounds()
        n = len(value)
        if n < lo or (hi is not None and n > hi):
            return False
        for i, v in enumerate(value):
            t = (
                self.types[i]
                if i < len(self.types)
                else (self.types[-1] if self.types else None)
            )
            if t is not None and not _type_instance(t, v):
                return False
        return True

    def generalize(self):
        return Tuple([generalize(t) for t in self.types])

    def _key(self):
        return (tuple(_key_of(t) for t in self.types), self.size_from, self.size_to)

    def __str__(self):
        if not self.types:
            return "Tuple"
        parts = [str(t) for t in self.types]
        parts += _render_size_args(self.size_from, self.size_to)
        return "Tuple[{}]".format(", ".join(parts))


class StructElement:
    """One ``Struct`` entry. ``optional`` is the key's own optionality: a
    key written ``Optional[k]``, or a plain key whose value type accepts
    undef (``NotUndef[k]`` forces it required)."""

    __slots__ = ("key", "optional", "value_type")

    def __init__(self, key, optional, value_type):
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "optional", optional)
        object.__setattr__(self, "value_type", value_type)

    def __setattr__(self, name, value):
        raise AttributeError("struct elements are immutable")

    def render_key(self):
        value_optional = _type_instance(self.value_type, None)
        quoted = puppet_quote(self.key)
        if self.optional:
            return quoted if value_optional else "Optional[{}]".format(quoted)
        return "NotUndef[{}]".format(quoted) if value_optional else quoted


class Struct(Any):
    __slots__ = ("elements",)
    TYPE_NAME = "Struct"

    def __init__(self, elements):
        self.elements = tuple(elements)

    def instance(self, value):
        if not isinstance(value, dict):
            return False
        keys = {e.key for e in self.elements}
        for k in value:
            if k not in keys:
                return False
        for e in self.elements:
            if e.key in value:
                if not _type_instance(e.value_type, value[e.key]):
                    return False
            elif not e.optional:
                return False
        return True

    def _key(self):
        return tuple((e.key, e.optional, _key_of(e.value_type)) for e in self.elements)

    def __str__(self):
        if not self.elements:
            return "Struct"
        parts = [
            "{} => {}".format(e.render_key(), str(e.value_type)) for e in self.elements
        ]
        return "Struct[{{{}}}]".format(", ".join(parts))


class Variant(Any):
    __slots__ = ("types",)
    TYPE_NAME = "Variant"

    def __init__(self, types):
        self.types = tuple(types)

    def instance(self, value):
        return any(_type_instance(t, value) for t in self.types)

    def generalize(self):
        return Variant([generalize(t) for t in self.types])

    def _key(self):
        return tuple(_key_of(t) for t in self.types)

    def __str__(self):
        if not self.types:
            return "Variant"
        return "Variant[{}]".format(", ".join(str(t) for t in self.types))


class SensitiveType(Any):
    """Puppet's ``Sensitive`` type: an instance is a :class:`Sensitive`
    value whose wrapped value matches the (optional) contained type."""

    __slots__ = ("contained",)
    TYPE_NAME = "Sensitive"
    contained: "_ty.Optional[Any]"

    def __init__(self, contained: "_ty.Optional[Any]" = None) -> None:
        # Puppet keeps only the generalized contained type (a range or size
        # is dropped), and ``Sensitive[Any]`` is plain ``Sensitive``.
        general: "_ty.Optional[Any]" = None
        if contained is not None:
            candidate: _ty.Any = generalize(contained)
            general = None if type(candidate) is Any else candidate
        self.contained = general

    def instance(self, value: _ty.Any) -> bool:
        """Whether ``value`` is a :class:`Sensitive` wrapping an instance
        of this type's own contained type (any ``Sensitive`` at all, when
        unparameterized)."""
        if not isinstance(value, Sensitive):
            return False
        if self.contained is None:
            return True
        return _type_instance(self.contained, value.unwrap())

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self) -> str:
        return _render_container("Sensitive", self.contained)


class TypeReference(Any):
    """An unresolved type name (unknown to the static loader), Puppet's
    ``TypeReference``. Never an instance of anything."""

    __slots__ = ("text",)
    TYPE_NAME = "TypeReference"

    def __init__(self, text):
        self.text = text

    def instance(self, value):
        return False

    def _key(self):
        return (self.text,)

    def __str__(self):
        return "TypeReference[{}]".format(puppet_quote(self.text))


class _PNamedType(Any):
    """A named-only type this subset does not model in full: no value hiera
    can hold is ever an instance, so ``instance`` is always ``False`` and
    Puppet's mismatch text ("expects a Timestamp value, got String") is
    still correct."""

    __slots__ = ("TYPE_NAME",)

    def __init__(self, name):
        self.TYPE_NAME = name

    def instance(self, value):
        return False

    def _key(self):
        return (self.TYPE_NAME,)


class Runtime(Any):
    """``Runtime[<runtime>, '<name>']``. Only ``Runtime['ruby', 'Symbol']``
    is meaningful here: it is the inferred type of a
    :class:`hyera.backends.RubySymbol` (never imported directly -- matched
    by class name/module to avoid a dependency on ``backends``). The class
    is *defined* in ``_psych`` and re-exported through ``backends``;
    ``__module__`` names the former, not the latter."""

    __slots__ = ("runtime", "runtime_name")
    TYPE_NAME = "Runtime"

    def __init__(self, runtime, name):
        self.runtime = runtime
        self.runtime_name = name

    def instance(self, value):
        cls = type(value)
        return (
            self.runtime == "ruby"
            and self.runtime_name == "Symbol"
            and cls.__name__ == "RubySymbol"
            and cls.__module__ == "hyera.backends._psych"
        )

    def _key(self):
        return (self.runtime, self.runtime_name)

    def __str__(self):
        return "Runtime[{}, {}]".format(self.runtime, puppet_quote(self.runtime_name))


class TypeAlias(Any):
    """One of Puppet's five static-loader aliases (``Data``, ``RichDataKey``,
    ``RichData``, ``Puppet::LookupKey``, ``Puppet::LookupValue``). Resolved
    lazily (and memoized) against its own body text, so a self-referencing
    body (``Data`` -> ``...Array[Data]``) terminates: resolution reuses this
    same cached instance rather than re-parsing."""

    __slots__ = ("alias_name", "_body_text", "_resolved")
    TYPE_NAME = "TypeAlias"

    def __init__(self, name, body_text):
        self.alias_name = name
        self._body_text = body_text
        self._resolved = None

    @property
    def resolved_type(self):
        if self._resolved is None:
            from .parser import parse_type as _parse

            # The one memo a sealed type object keeps.
            object.__setattr__(self, "_resolved", _parse(self._body_text))
        return self._resolved

    def instance(self, value):
        return _type_instance(self.resolved_type, value)

    @property
    def name(self):
        return self.alias_name

    def _key(self):
        return (self.alias_name,)

    def __str__(self):
        return self.alias_name


#: Puppet's five static-loader type aliases (``static_loader.rb:30-36``).
ALIASES = {
    "data": TypeAlias(
        "Data", "Variant[ScalarData,Undef,Hash[String,Data],Array[Data]]"
    ),
    "richdatakey": TypeAlias("RichDataKey", "Variant[String,Numeric]"),
    "richdata": TypeAlias(
        "RichData",
        "Variant[Scalar,SemVerRange,Binary,Sensitive,Type,TypeSet,URI,Object,"
        "Undef,Default,Hash[RichDataKey,RichData],Array[RichData]]",
    ),
}
ALIASES["puppet::lookupkey"] = TypeAlias("Puppet::LookupKey", "RichDataKey")
ALIASES["puppet::lookupvalue"] = TypeAlias("Puppet::LookupValue", "RichData")

#: Named-only types (second tier): full detail in
#: ``_types.parser.TYPE_MAP``; instances are always ``False``.
NAMED_ONLY_TYPES = (
    "default",
    "type",
    "typeset",
    "uri",
    "object",
    "binary",
    "semver",
    "semverrange",
    "timespan",
    "timestamp",
    "callable",
    "catalogentry",
    "class",
    "resource",
)

Integer.DEFAULT = Integer()
Float.DEFAULT = Float()
String.DEFAULT = String()
ANY = Any()
UNDEF = Undef()
SCALAR = Scalar()
SCALAR_DATA = ScalarData()
NUMERIC = Numeric()
BOOLEAN = Boolean()
COLLECTION = Collection()
REGEXP = Regexp()


def _render_container(name, contained, show_literal=False):
    """Optional/NotUndef/Sensitive's own formatter (``type_formatter.rb``
    ``string_POptionalType``/``string_PNotUndefType``/``string_PSensitiveType``):
    the contained type renders in full. ``show_literal`` is Optional/
    NotUndef's own special case (not Sensitive's): a literal ``String``
    child prints its quoted value."""
    if contained is None or (name == "NotUndef" and type(contained) is Any):
        return name
    if show_literal and isinstance(contained, String) and contained.literal is not None:
        return "{}[{}]".format(name, puppet_quote(contained.literal))
    if isinstance(contained, str):
        return "{}[{}]".format(name, puppet_quote(contained))
    return "{}[{}]".format(name, contained)


def _key_of(t):
    if t is None:
        return None
    if isinstance(t, Any):
        return t._key() + (type(t).__name__,)
    return t


def _type_instance(t, value):
    """``t.instance(value)``, where ``t`` may be a raw literal string (an
    Optional/NotUndef contained "type" that is really a literal, per Ruby's
    ``assert_type(ast, param) unless param.is_a?(String)``)."""
    if isinstance(t, str):
        return value == t
    return t.instance(value)


def infer(value):
    """Puppet's ``TypeCalculator#infer`` (``type_calculator.rb``)."""
    if value is None:
        return UNDEF
    if isinstance(value, bool):
        return Boolean(value)
    if isinstance(value, str):
        return String(literal=value)
    if isinstance(value, int):
        return Integer(value, value)
    if isinstance(value, float):
        return Float(value, value)
    if isinstance(value, Sensitive):
        return SensitiveType(infer(value.unwrap()))
    if isinstance(value, re.Pattern):
        return Regexp(value.pattern)
    if isinstance(value, (list, tuple)):
        return _infer_array(value)
    if isinstance(value, dict):
        return _infer_hash(value)
    cls = type(value)
    if cls.__name__ == "RubySymbol" and cls.__module__ == "hyera.backends._psych":
        return Runtime("ruby", "Symbol")
    raise TypeError("no Puppet type for {!r}".format(value))


def infer_set(value):
    """Puppet's ``TypeCalculator#infer_set``: like :func:`infer`, but Array
    and Hash get the precise Tuple/Struct shape used for mismatch
    reporting (``infer_set_Array``/``infer_set_Hash``)."""
    if isinstance(value, (list, tuple)):
        if not value:
            return Array(None, 0, 0)
        return Tuple([infer_set(v) for v in value])
    if isinstance(value, dict):
        if value and all(isinstance(k, str) and k for k in value):
            return Struct(
                [StructElement(k, False, infer_set(v)) for k, v in value.items()]
            )
        return _infer_hash(value)
    return infer(value)


def _infer_array(value):
    if not value:
        return Array(None, 0, 0)
    elem = _generalized_common(infer_generic(v) for v in value)
    return Array(elem, len(value), len(value))


def _infer_hash(value):
    if not value:
        return Hash(None, None, 0, 0)
    keys = _generalized_common(infer_generic(k) for k in value)
    vals = _generalized_common(infer_generic(v) for v in value.values())
    return Hash(keys, vals, len(value), len(value))


def infer_generic(value):
    return generalize(infer(value))


def generalize(t):
    if isinstance(t, Any):
        return t.generalize()
    return t


def _generalized_common(types):
    types = list(types)
    uniq = []
    for t in types:
        if not any(t == u for u in uniq):
            uniq.append(t)
    if len(uniq) == 1:
        return uniq[0]
    return Variant(uniq)


def _eql_key(value):
    """A recursive, hashable, type-tagged key implementing Ruby ``eql?``.

    Ruby's ``hash``/``eql?`` distinguish ``1``, ``1.0`` and ``true`` (unlike
    Python, where ``hash(1) == hash(1.0) == hash(True)`` and ``1 == 1.0 ==
    True``); a list or dict compares by content, a dict in any key order.
    Tag every value with its Ruby-relevant type before hashing/comparing so
    two values Ruby would consider unequal never collide.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (list, tuple)):
        return ("array", tuple(_eql_key(v) for v in value))
    if isinstance(value, dict):
        return ("hash", frozenset((_eql_key(k), _eql_key(v)) for k, v in value.items()))
    if value is None:
        return ("undef", None)
    if isinstance(value, Sensitive):
        return ("sensitive", _eql_key(value.unwrap()))
    # Anything else (an object with no Ruby equivalent): keyed by identity,
    # matching Ruby's default Object#hash/#eql?.
    return ("id", id(value))


class Sensitive:
    """Puppet's ``Sensitive`` value wrapper (``p_sensitive_type.rb:11-37``).

    ``str()``/``repr()`` both redact (Puppet's ``to_s``: "Sensitive [value
    redacted]"). Equality and hashing follow Puppet: two ``Sensitive``
    values are equal, and hash equal, exactly when their wrapped values are
    Ruby-``eql?`` -- so a list or dict payload hashes despite being
    unhashable in plain Python. ``.unwrap()`` returns the real value.

    :param value: the value to wrap.
    """

    __slots__ = ("_value",)

    def __init__(self, value: _ty.Any) -> None:
        self._value = value

    def __class_getitem__(cls, item: _ty.Any) -> "SensitiveType":
        """``Sensitive[T]``: the Puppet *type* ``Sensitive[T]`` (see
        :mod:`hyera.types`) -- a type object, never a value. Calling stays
        the value wrapper (``Sensitive("x")``), unaffected by this.

        :param item: a type object, a ``hyera.types`` class, or a Puppet
            type-expression string, for the contained type.
        :returns: the ``Sensitive[T]`` type object.
        """
        from .parser import as_type

        return SensitiveType(as_type(item))

    def unwrap(self) -> _ty.Any:
        """The wrapped value, unredacted.

        :returns: the wrapped value.
        """
        return self._value

    def __repr__(self) -> str:
        return "Sensitive [value redacted]"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sensitive):
            return NotImplemented
        return _eql_key(self._value) == _eql_key(other._value)

    def __hash__(self) -> int:
        return hash(_eql_key(self._value))
