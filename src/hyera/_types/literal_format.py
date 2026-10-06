# Ported from Puppet 8 lib/puppet/pops/types/string_converter.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Rendering Puppet literals: quoted strings, range bounds and size arguments.

Ports ``puppet_quote`` of Puppet's ``pops/types/string_converter.rb``.
"""

from __future__ import annotations


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
