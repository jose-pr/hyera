# Ported from Puppet 8 lib/puppet/pops/types/string_converter.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Puppet's value-to-string conversion: ``String.new()``'s engine.

Ports the ``string_converter.rb`` subset hiera's data can reach: the
default format per value type, a single ``%<flags><width>.<prec><format>``
directive per value, and Ruby's ``Kernel#format`` for the numeric and string
directives Puppet delegates to (``d x X o b B e E f g G a A s p c``).

Out of the subset, with an error instead of a silent default: a format given
as a type map, the container flags (delimiters, ``#`` indentation, width and
precision on an Array or Hash), a precision on ``%a``/``%A``, and the Binary,
Timestamp, URI and Object value types.
"""

import math
import re

from ..exceptions import HieraLookupError
from .types import Sensitive, infer_set

__all__ = ["convert", "puppet_quote"]

#: Puppet's ``Format::FMT_PATTERN_STR``.
_FMT_RE = re.compile(r"%([\s\[+#0{<(|-]*)([1-9][0-9]*)?(?:\.([0-9]+))?([a-zA-Z])$")
_DELIMITERS = "[{(<|"
_DELIMITER_PAIRS = {"[": "[]", "{": "{}", "(": "()", "<": "<>", "|": "||"}

#: Puppet's own text for a ``string_formats`` argument of the wrong type.
_FORMATS_TYPE = (
    "Formats = Variant[Default, String[1], TypeMap = Hash[Type, Variant["
    "Format = Pattern[/^%([\\s\\[+#0{<(|-]*)([1-9][0-9]*)?(?:\\.([0-9]+))?"
    "([a-zA-Z])$/], ContainerFormat = Struct[{Optional['format'] => Format = "
    "Pattern[/^%([\\s\\[+#0{<(|-]*)([1-9][0-9]*)?(?:\\.([0-9]+))?([a-zA-Z])$/], "
    "Optional['separator'] => String, Optional['separator2'] => String, "
    "Optional['string_formats'] => Hash[Type, Format = Pattern[/^%([\\s\\[+#0{<"
    "(|-]*)([1-9][0-9]*)?(?:\\.([0-9]+))?([a-zA-Z])$/]]}]]]]"
)

#: The directives Puppet accepts per value type, in its own order.
_INTEGER_CHARS = "dxXobBeEfgGaAspc"
_FLOAT_CHARS = "dxXobBeEfgGaAsp"
_STRING_CHARS = "cCudspt"
_BOOLEAN_CHARS = "tTyYdxXobBeEfgGaAsp"
_UNDEF_CHARS = "nudxXobBeEfgGaAvVsp"
_ARRAY_CHARS = "asp"
_HASH_CHARS = "hasp"
_REGEXP_CHARS = "sp"


class FormatError(HieraLookupError):
    """``Illegal format`` for a directive the value's type does not take, or a
    format string that is not one directive."""


class _Format:
    """One parsed ``%<flags><width>.<prec><format>`` directive."""

    __slots__ = ("orig", "flags", "width", "prec", "char", "tail")

    def __init__(self, orig):
        m = _FMT_RE.match(orig)
        if not m:
            raise FormatError(
                "The format '{}' is not a valid format on the form "
                "'%<flags><width>.<prec><format>'".format(orig)
            )
        flags = m.group(1)
        if len(set(flags)) != len(flags):
            raise FormatError(
                "The same flag can only be used once, got '{}'".format(orig)
            )
        if sum(f in _DELIMITERS for f in flags) > 1:
            raise FormatError(
                "Only one of the delimiters [ { ( < | can be given in the "
                "format flags, got '{}'".format(orig)
            )
        self.orig = orig
        self.flags = flags
        self.width = int(m.group(2)) if m.group(2) else None
        self.prec = int(m.group(3)) if m.group(3) is not None else None
        self.char = m.group(4)
        #: ``$`` also matches before one trailing newline, which Ruby's
        #: ``format`` then keeps in its output.
        self.tail = orig[m.end() :]

    @property
    def alt(self):
        return "#" in self.flags

    @property
    def left(self):
        return "-" in self.flags

    def check(self, type_name, allowed):
        if self.char not in allowed:
            raise FormatError(
                "Illegal format '{}' specified for value of {} type - expected "
                "one of the characters '{}'".format(self.char, type_name, allowed)
            )

    def delimiters(self, default):
        """The opening and closing delimiters of a container: the one a
        delimiter flag names, none for a space flag, else ``default``."""
        for flag in self.flags:
            if flag in _DELIMITER_PAIRS:
                return _DELIMITER_PAIRS[flag]
        return "" if " " in self.flags else default


_DEFAULT = {"Integer": _Format("%d"), "Float": _Format("%f")}
_STRING_DEFAULT = _Format("%s")


def puppet_double_quote(s):
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
    when ``s`` holds a control character (``string_converter.rb:862-925``)."""
    if enforce_double_quotes:
        return puppet_double_quote(s)
    if any(ord(c) < 0x20 for c in s):
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


def _ruby_float_inspect(f):
    """Ruby's ``Float#inspect``: like Python's ``repr``, but the mantissa
    of an exponent form always carries a decimal point (``1.0e+20``, not
    ``1e+20``)."""
    if f != f:  # NaN
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "Infinity" if f > 0 else "-Infinity"
    text = repr(f)
    if "e" not in text and abs(f) >= 1e15:
        # Ruby moves to exponent form from 1e15, Python from 1e16.
        whole, _, frac = text.lstrip("-").partition(".")
        digits = (whole + frac).rstrip("0")
        sign = "-" if f < 0 else ""
        return "{}{}.{}e+{:02d}".format(
            sign, digits[0], digits[1:] or "0", len(whole) - 1
        )
    if "e" in text:
        mantissa, exp = text.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        if not exp.startswith(("+", "-")):
            exp = "+" + exp
        text = mantissa + "e" + exp
    return text


# ------------------------------------------------------- Ruby's Kernel#format


def _pad(body, flags, width):
    if width is not None and len(body) < width:
        return body.ljust(width) if "-" in flags else body.rjust(width)
    return body


def _sprintf_str(flags, width, prec, text):
    """``%s``/``%p`` over an already-rendered ``text``: precision truncates,
    width pads; numeric flags do not apply."""
    if prec is not None:
        text = text[:prec]
    return _pad(text, flags, width)


def _two_complement(n, base_bits):
    """The digits of a negative ``n`` in base ``2 ** base_bits`` with the
    leading run of the fill digit collapsed to one, as Ruby prints it."""
    base = 1 << base_bits
    length = len(_digits(-n, base)) + 1
    digits = _digits(base**length + n, base).rjust(length, "0")
    fill = _digits(base - 1, base)
    while len(digits) > 1 and digits[0] == fill and digits[1] == fill:
        digits = digits[1:]
    return digits, fill


def _digits(n, base):
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, base)
        out.append("0123456789abcdef"[r])
    return "".join(reversed(out))


def _sprintf_int(conv, flags, width, prec, n):
    plus, space, alt, left, zero = (c in flags for c in "+ #-0")
    lower = conv.lower()
    base_bits = {"x": 4, "o": 3, "b": 1}.get(lower)
    upper = conv in "XB"
    prefix = ""
    if lower == "d" or plus or space or n >= 0:
        sign = "-" if n < 0 else "+" if plus else " " if space else ""
        mag = abs(n)
        digits = str(mag) if base_bits is None else _digits(mag, 1 << base_bits)
        if alt and mag != 0:
            prefix = {"x": "0x", "o": "0", "b": "0b"}.get(lower, "")
        if prec is not None:
            digits = "" if prec == 0 and mag == 0 else digits.rjust(prec, "0")
        elif zero and not left and width is not None:
            room = width - len(sign) - len(prefix)
            digits = digits.rjust(room, "0")
        body = sign + prefix + digits
    else:
        digits, fill_digit = _two_complement(n, base_bits)
        if alt and lower in ("x", "b"):
            prefix = "0" + lower
        if prec is not None:
            digits = digits.rjust(prec - 2, fill_digit)
        elif zero and not left and width is not None:
            digits = digits.rjust(width - len(prefix) - 2, fill_digit)
        body = prefix + ".." + digits
    if upper:
        body = body.upper()
    return _pad(body, flags, width)


def _float_special(conv, flags, width, value):
    if value != value:
        text = "NaN"
    else:
        text = "Inf"
    sign = "-" if (value < 0 or math.copysign(1, value) < 0) and value == value else ""
    if not sign and "+" in flags:
        sign = "+"
    elif not sign and " " in flags:
        sign = " "
    return _pad(sign + text, flags, width)


def _hex_float(value, upper):
    text = float(value).hex()
    sign = "-" if text.startswith("-") else ""
    text = text.lstrip("-")
    mantissa, exp = text[2:].split("p")
    whole, _, frac = mantissa.partition(".")
    frac = frac.rstrip("0")
    exp = int(exp)
    body = "0x{}{}p{}{}".format(
        whole, "." + frac if frac else "", "+" if exp >= 0 else "-", abs(exp)
    )
    body = sign + body
    return body.upper() if upper else body


def _sprintf_float(conv, flags, width, prec, value):
    if value != value or value in (float("inf"), float("-inf")):
        return _float_special(conv, flags, width, value)
    if conv in "aA":
        if prec is not None:
            raise FormatError(
                "hiera does not support a precision with the %{} format".format(conv)
            )
        sign_less = _hex_float(abs(value), conv == "A")
        sign = (
            "-"
            if math.copysign(1, value) < 0
            else "+" if "+" in flags else (" " if " " in flags else "")
        )
        body = sign + sign_less
        if "0" in flags and "-" not in flags and width is not None:
            pad = width - len(body)
            if pad > 0:
                head = sign + sign_less[:2]
                body = head + "0" * pad + sign_less[2:]
        return _pad(body, flags, width)
    py_flags = "".join(f for f in flags if f in "-+ #0")
    spec = "%" + py_flags
    if width is not None:
        spec += str(width)
    if prec is not None:
        spec += "." + str(prec)
    return (spec + conv) % value


def _sprintf_directive(char, flags, width, prec, value):
    """Ruby's ``Kernel#format(<one directive>, value)`` for a rendered
    ``value``: a Python int, float, str or ``Sensitive``."""
    if isinstance(value, Sensitive):
        if char == "c":
            raise FormatError(
                "no implicit conversion of Puppet::Pops::Types::PSensitiveType"
                "::Sensitive into Integer"
            )
        if char in "dxXobBiu":
            raise FormatError(
                "can't convert Puppet::Pops::Types::PSensitiveType::Sensitive "
                "into Integer"
            )
        if char in "eEfgGaA":
            raise FormatError(
                "can't convert Puppet::Pops::Types::PSensitiveType::Sensitive "
                "into Float"
            )
        text = "Sensitive [value redacted]"
        if char == "p":
            text = "#<" + text + ">"
        return _sprintf_str(flags, width, prec, text)
    if char in "dxXobB":
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise FormatError(
                    "NaN" if value != value else _ruby_float_inspect(value)
                )
            value = int(value)
        return _sprintf_int(char, flags, width, prec, value)
    if char in "eEfgGaA":
        return _sprintf_float(char, flags, width, prec, float(value))
    if char == "c":
        return _sprintf_str(flags, width, None, chr(value))
    raise AssertionError(char)  # pragma: no cover


# ---------------------------------------------------------------- rendering


def _inspect(value):
    """What ``%p`` prints for ``value`` (Ruby ``inspect`` for scalars,
    Puppet's own quoting for strings)."""
    if value is None:
        return "undef"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _ruby_float_inspect(value)
    if isinstance(value, str):
        return puppet_quote(value)
    if isinstance(value, Sensitive):
        return "#<Sensitive [value redacted]>"
    if isinstance(value, re.Pattern):
        return "/{}/".format(value.pattern)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_inspect(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                "{} => {}".format(_inspect(k), _inspect(v)) for k, v in value.items()
            )
            + "}"
        )
    return str(value)


def _apply_flags(f, text):
    """Puppet's ``apply_string_flags``: only ``-``, width and precision."""
    return _sprintf_str("-" if f.left else "", f.width, f.prec, text)


def _render_integer(f, value):
    f.check("Integer", _INTEGER_CHARS)
    c = f.char
    if c in "dxXobB":
        text = _sprintf_directive(c, f.flags, f.width, f.prec, value)
    elif c in "eEfgGaA":
        text = _sprintf_directive(c, f.flags, f.width, f.prec, float(value))
    elif c == "c":
        if value < 0 or value > 0x10FFFF:
            raise FormatError(
                "pack(U): value out of range"
                if value < 2**63
                else "bignum too big to convert into 'long'"
            )
        char = chr(value)
        if f.alt:
            char = '"' + char + '"'
        text = _sprintf_str(f.flags, f.width, f.prec, char)
    else:
        text = str(value)
        if f.alt and c == "s":
            text = '"' + text + '"'
        text = _sprintf_str(f.flags, f.width, f.prec, text)
    return text + f.tail


def _render_float(f, value):
    f.check("Float", _FLOAT_CHARS)
    c = f.char
    if c in "dxXobBeEfgGaA":
        text = _sprintf_directive(c, f.flags, f.width, f.prec, value)
    else:
        text = _ruby_float_inspect(value)
        if c == "s" and f.alt:
            text = '"' + text + '"'
        text = _sprintf_str(f.flags, f.width, f.prec, text)
    return text + f.tail


def _capitalize(s):
    return s[:1].upper() + s[1:].lower()


def _render_string(f, value):
    f.check("String", _STRING_CHARS)
    c = f.char
    if c == "s":
        return _sprintf_str(f.flags, f.width, f.prec, value) + f.tail
    if c == "p":
        return _apply_flags(f, puppet_quote(value, f.alt))
    if c == "c":
        text = _capitalize(value)
    elif c == "C":
        text = "::".join(_capitalize(p) for p in value.split("::"))
    elif c == "u":
        text = value.upper()
    elif c == "d":
        text = value.lower()
    else:
        text = value.strip()
    if f.alt:
        return _apply_flags(f, puppet_quote(text))
    return _sprintf_str(f.flags, f.width, f.prec, text) + f.tail


def _render_boolean(f, value):
    f.check("Boolean", _BOOLEAN_CHARS)
    c = f.char
    if c in "tT":
        text = "true" if value else "false"
        if c == "T":
            text = text.capitalize()
        return _apply_flags(f, text[0] if f.alt else text)
    if c in "yY":
        text = ("yes" if value else "no") if c == "y" else ("Yes" if value else "No")
        return _apply_flags(f, text[0] if f.alt else text)
    if c in "dxXobB":
        return _render_integer(f, 1 if value else 0)
    if c in "eEfgGaA":
        return _render_float(f, 1.0 if value else 0.0)
    return _apply_flags(f, "true" if value else "false")


def _render_undef(f):
    f.check("Undef", _UNDEF_CHARS)
    c = f.char
    if c == "n":
        text = "null" if f.alt else "nil"
    elif c == "u":
        text = "undefined" if f.alt else "undef"
    elif c in "dxXobBeEfgGaA":
        text = "NaN"
    elif c == "v":
        text = "n/a"
    elif c == "V":
        text = "N/A"
    elif c == "s":
        text = '""' if f.alt else ""
    else:
        text = '"undef"' if f.alt else "undef"
    return _apply_flags(f, text)


def _render_regexp(f, value):
    f.check("Regexp", _REGEXP_CHARS)
    if f.char == "p":
        text = "/{}/".format(value.pattern)
        return text if f.orig == "%p" else _sprintf_str(f.flags, f.width, f.prec, text)
    text = value.pattern
    if f.alt:
        text = puppet_quote(text)
    return text if f.orig == "%s" else _sprintf_str(f.flags, f.width, f.prec, text)


def _container_format(f, type_name, allowed):
    f.check(type_name, allowed)
    if f.alt:
        raise FormatError(
            "hiera does not support the # (indenting) flag in a format for an "
            "{}: '{}'".format(type_name, f.orig)
        )


def _element(value, formats):
    """An element of a container: nested containers follow the container
    formats, everything else is rendered with ``%p``."""
    if isinstance(value, (list, tuple)):
        return _render_array(formats["Array"], value, formats)
    if isinstance(value, dict):
        return _render_hash(formats["Hash"], value, formats)
    return _inspect(value)


def _render_array(f, value, formats):
    _container_format(f, "Array", _ARRAY_CHARS)
    delims = f.delimiters("[]")
    left, right = delims[:1], delims[1:]
    return left + ", ".join(_element(v, formats) for v in value) + right


def _render_hash(f, value, formats):
    _container_format(f, "Hash", _HASH_CHARS)
    if f.char == "a":
        pairs = [[k, v] for k, v in value.items()]
        return _render_array(formats["Array"], pairs, formats)
    delims = f.delimiters("{}")
    body = ", ".join(
        "{} => {}".format(_element(k, formats), _element(v, formats))
        for k, v in value.items()
    )
    return delims[:1] + body + delims[1:]


def _render(value, f):
    if value is None:
        return _render_undef(f or _STRING_DEFAULT)
    if isinstance(value, bool):
        return _render_boolean(f or _STRING_DEFAULT, value)
    if isinstance(value, int):
        return _render_integer(f or _DEFAULT["Integer"], value)
    if isinstance(value, float):
        if value != value:
            # NaN is in no Float range, so Puppet falls back to the plain
            # string rule whatever format was asked for.
            return "NaN"
        return _render_float(f or _DEFAULT["Float"], value)
    if isinstance(value, str):
        return _render_string(f or _STRING_DEFAULT, value)
    if isinstance(value, (list, tuple, dict)):
        formats = dict(_DEFAULT_CONTAINER_FORMATS)
        kind = "Hash" if isinstance(value, dict) else "Array"
        formats[kind] = f or formats[kind]
        return _element_top(value, formats)
    if isinstance(value, re.Pattern):
        return _render_regexp(f or _STRING_DEFAULT, value)
    f = f or _STRING_DEFAULT
    if isinstance(value, Sensitive):
        if f.char not in "dxXobBeEfgGaAcspiu":
            raise FormatError("malformed format string - %{}".format(f.char))
        return _sprintf_directive(f.char, f.flags, f.width, f.prec, value)
    return str(value)


_DEFAULT_CONTAINER_FORMATS = {"Array": _Format("%a"), "Hash": _Format("%h")}


def _element_top(value, formats):
    if isinstance(value, dict):
        return _render_hash(formats["Hash"], value, formats)
    return _render_array(formats["Array"], value, formats)


#: Marks "no format argument" (Puppet's ``:default``), apart from a ``None``
#: argument, which is a type error.
UNSET = object()


def convert(value, string_formats=UNSET):
    """Puppet's ``String.new(value, string_formats)``: render ``value`` as
    a string. ``string_formats`` is :data:`UNSET`, an empty mapping, or a
    single ``%``-format string.

    :raises HieraLookupError: the format is not one directive, names a
        directive the value's type does not take (Puppet's own "Illegal
        format" text), or is outside the supported subset.
    """
    if string_formats is UNSET or (
        isinstance(string_formats, dict) and not string_formats
    ):
        return _render(value, None)
    if not isinstance(string_formats, str) or not string_formats:
        raise HieraLookupError(
            "'new_string' parameter 'string_formats' expects a {} value, "
            "got {}".format(_FORMATS_TYPE, infer_set(string_formats).name)
        )
    return _render(value, _Format(string_formats))
