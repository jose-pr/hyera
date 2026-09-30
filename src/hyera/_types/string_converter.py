# Ported from Puppet 8 lib/puppet/pops/types/string_converter.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Puppet's value-to-string conversion: ``String.new()``'s engine.

Ports the ``string_converter.rb`` subset hiera's data can reach: the
default format per value type, ``%p`` (Puppet's "programmatic"/quoted
form -- what every container renders its elements with), and the small
``Kernel#format`` subset (``d x X o b B e E f g G a A s p c``, flags
``- + space 0 #``, width/precision) real conversions use.
"""

import re

from ..exceptions import HieraLookupError
from .types import Sensitive

__all__ = ["convert", "puppet_quote"]

_FMT_RE = re.compile(
    r"%(?P<flags>[-+ 0#]*)(?P<width>\d+)?(?:\.(?P<prec>\d+))?(?P<conv>[a-zA-Z])"
)

#: Puppet's own alphabet for "a valid format character" error text.
_ALL_FORMAT_CHARS = "dxXobBeEfgGaAspc"


class FormatError(HieraLookupError):
    """``'new_string' parameter 'string_formats'``/``Illegal format``."""


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
    if "e" in text:
        mantissa, exp = text.split("e")
        if "." not in mantissa:
            mantissa += ".0"
        if not exp.startswith(("+", "-")):
            exp = "+" + exp
        text = mantissa + "e" + exp
    return text


def _int_body(directive, flags, width, prec, value):
    neg = value < 0
    mag = -value if neg else value
    if directive in ("d", "s"):
        body = str(mag)
    elif directive in ("x", "X"):
        body = format(mag, "x" if directive == "x" else "X")
        if "#" in flags and mag != 0:
            body = ("0x" if directive == "x" else "0X") + body
    elif directive == "o":
        body = format(mag, "o")
        if "#" in flags and mag != 0:
            body = "0" + body
    elif directive in ("b", "B"):
        body = format(mag, "b")
        if "#" in flags and mag != 0:
            body = ("0b" if directive == "b" else "0B") + body
    elif directive in ("e", "E", "f", "g", "G", "a", "A"):
        return _float_body(directive, flags, width, prec, float(value))
    else:  # directive == "c" -- the only remaining member of _ALL_FORMAT_CHARS
        # once _render already handled "p"/"s" itself before ever calling
        # here; every other member is one of the elif branches above, so
        # this is never reached for anything but "c" (never a directive
        # _check_char would have rejected already).
        body = chr(mag)
    sign = "-" if neg else ("+" if "+" in flags else (" " if " " in flags else ""))
    body = sign + body
    if width and len(body) < width:
        pad = "0" if "0" in flags and "-" not in flags else " "
        if pad == "0" and sign:
            body = sign + body[len(sign) :].rjust(width - len(sign), "0")
        elif "-" in flags:
            body = body.ljust(width)
        else:
            body = body.rjust(width, pad)
    return body


def _float_body(directive, flags, width, prec, value):
    p = int(prec) if prec is not None else 6
    conv = directive.lower()
    if conv == "f":
        body = "%.*f" % (p, value)
    elif conv == "e":
        body = "%.*e" % (p, value)
        body = re.sub(r"e([+-])0*(\d\d+)", r"e\1\2", body)
        body = re.sub(r"e([+-])(\d)$", r"e\g<1>0\2", body)
    elif conv in ("g",):
        body = "%.*g" % (p if prec is not None else 6, value)
    else:
        body = repr(value)
    if directive.isupper():
        body = body.upper()
    if "+" in flags and value >= 0:
        body = "+" + body
    if width and len(body) < width:
        body = body.rjust(width) if "-" not in flags else body.ljust(width)
    return body


def _new_boolean_word(value, directive):
    if directive in ("y", "Y"):
        return "yes" if value else "no"
    return "true" if value else "false"


def _check_char(directive, type_name, allowed):
    if directive not in allowed:
        raise FormatError(
            "Illegal format '{}' specified for value of {} type - expected "
            "one of the characters '{}'".format(directive, type_name, allowed)
        )


def _parse_directive(fmt):
    m = _FMT_RE.match(fmt)
    if not m:
        return None
    return m.group("flags") or "", m.group("width"), m.group("prec"), m.group("conv")


def convert(value, string_formats=None):
    """Puppet's ``String.new(value, string_formats)``: render ``value`` as
    a string. ``string_formats`` is ``None`` (Puppet's ``:default``) or a
    single ``%``-format string; Array/Hash elements are always rendered
    with ``%p`` regardless of the top-level format."""
    if string_formats is not None and not isinstance(string_formats, str):
        raise HieraLookupError(
            "'new_string' parameter 'string_formats' expects a Formats value, "
            "got {}".format(_short(string_formats))
        )
    directive = None
    flags = ""
    width = prec = None
    if string_formats:
        parsed = _parse_directive(string_formats)
        if parsed is None:
            directive = "s"
        else:
            flags, width, prec, directive = parsed
            width = int(width) if width else None
    return _render(value, directive, flags, width, prec)


def _short(value):
    from .types import infer

    return infer(value).name


def _render(value, directive, flags, width, prec):
    if value is None:
        # string_PUndefType: the default format is the empty string;
        # only an explicit %p (or %s) renders the literal "undef".
        return "undef" if directive in ("p", "s") else ""
    if isinstance(value, bool):
        d = directive or ""
        return _new_boolean_word(value, d)
    if isinstance(value, int):
        d = directive or "d"
        _check_char(d, "Integer", _ALL_FORMAT_CHARS)
        if d == "p" or d == "s":
            return str(value)
        return _int_body(d, flags, width, prec, value)
    if isinstance(value, float):
        d = directive or "f"
        if d == "p":
            return _ruby_float_inspect(value)
        if d == "s":
            return _ruby_float_inspect(value)
        return _float_body(d, flags, width, prec, value)
    if isinstance(value, str):
        d = directive or "s"
        if d == "p":
            return puppet_quote(value)
        return value
    if isinstance(value, Sensitive):
        return "Sensitive [value redacted]"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render(v, "p", "", None, None) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                # The AIO/Ruby-3.2 form ("k"=>"v"), not this box's Ruby
                # oracle ("k" => "v").
                "{}=>{}".format(
                    _render(k, "p", "", None, None), _render(v, "p", "", None, None)
                )
                for k, v in value.items()
            )
            + "}"
        )
    return str(value)
