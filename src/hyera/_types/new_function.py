# Ported from Puppet 8 lib/puppet/functions/new.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Puppet's ``new()``: ``functions/new.rb`` plus each type's own
``new_function`` this subset ports.

``new_instance(type_, value, *args)``: one argument already an instance of
``type_`` (and no ``*args``) is returned unchanged; else the type's own
conversion runs, and the result is asserted against ``type_`` with the
subject ``"Converted value from <type_>.new()"`` (``_types.mismatch``).
Types outside this subset's new()-capable tier (SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI, Type,
Object) raise our own "hyera does not support new()" text -- a deliberate
deviation, not a bug, since Puppet itself does support new() for several
of them.
"""

import re

from ..exceptions import HieraLookupError
from .string_converter import UNSET as _NO_FORMAT
from .string_converter import convert as _string_convert
from .mismatch import assert_instance_of, short_name
from .types import (
    Any,
    Array,
    Boolean,
    Float,
    Hash,
    Integer,
    NotUndef,
    Numeric,
    Optional,
    Regexp,
    SensitiveType,
    String,
    Struct,
    Tuple,
    Sensitive,
    _PNamedType,
    infer_generic,
    infer_set,
)

__all__ = ["new_instance"]

#: Puppet has a new_function for these but this
#: subset deliberately does not implement one.
_OUR_UNSUPPORTED_NAMES = frozenset(
    [
        "SemVer",
        "SemVerRange",
        "Timespan",
        "Timestamp",
        "Binary",
        "URI",
        "Type",
        "Object",
    ]
)

# Puppet's ``INTEGER_PATTERN_LENIENT`` and ``FLOAT_PATTERN`` (``types.rb``),
# anchored as Ruby's ``\A...\z`` over ASCII digits and blanks.
_WS = r"[ \t\n\r\f\v]*"
_LENIENT_INT_RE = re.compile(
    r"\A[+-]?" + _WS + r"(?:[0-9]+|0[xX][0-9A-Fa-f]+|0[bB][01]+)\Z"
)
_FLOAT_RE = re.compile(
    r"\A[+-]?" + _WS + r"(?:(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE]-?[0-9]+)?"
    r"|0[xX][0-9A-Fa-f]+|0[0-7]+|0[bB][01]+)\Z"
)
#: ``Puppet::Pops::Patterns::NUMERIC``: ``Numeric`` strings are read with it.
_NUMERIC_RE = re.compile(
    r"\A[ \t]*([-+]?)[ \t]*((0[xX][0-9A-Fa-f]+)|(0?[0-9]+)"
    r"((?:\.[0-9]+)?(?:[eE]-?[0-9]+)?))[ \t]*\Z"
)
_RADICES = (2, 8, 10, 16)
_BLANKS = " \t"
_WHITESPACE = " \t\n\r\f\v"
_BOOL_WORDS = {
    "true": True,
    "yes": True,
    "y": True,
    "false": False,
    "no": False,
    "n": False,
}
_NO_RADIX = object()


def new_instance(type_, value, *args):
    """Puppet's ``new_instance(t, *args)`` (``functions/new.rb``)."""
    if not isinstance(type_, Any):
        inferred = infer_set(type_)
        raise HieraLookupError(
            "'new' parameter 'type' expects a Type value, got {}".format(
                short_name(inferred)
            )
        )
    if not args and type_.instance(value):
        return value
    result = _dispatch(type_, value, args)
    return assert_instance_of(
        "Converted value from {}.new()".format(type_), type_, result
    )


def _dispatch(type_, value, args):
    if isinstance(type_, Integer):
        return _new_integer(value, *args)
    if isinstance(type_, Float):
        return _new_float(value, *args)
    if isinstance(type_, Numeric):
        return _new_numeric(value, *args)
    if isinstance(type_, String):
        return _new_string(value, *args)
    if isinstance(type_, Boolean):
        return _new_boolean(value, *args)
    if isinstance(type_, SensitiveType):
        if args:
            _arity("new_sensitive", "1 argument", 1 + len(args))
        return value if isinstance(value, Sensitive) else Sensitive(value)
    if isinstance(type_, (Tuple, Array)):
        return _new_array(value, *args)
    if isinstance(type_, (Struct, Hash)):
        return _new_hash(value, *args)
    if isinstance(type_, (Optional, NotUndef)):
        if type_.contained is None:
            _not_supported(type_)
        if isinstance(type_.contained, str):
            # A literal string argument (``Optional['x']``/``Optional[x]``,
            # as Puppet does): Puppet's own new() dispatches through the
            # literal's generalized data type (String), then the caller's
            # assert_instance_of checks the result against the literal type
            # itself -- so a value equal to the literal converts cleanly and
            # anything else is a wrong-type mismatch, never "not supported".
            return _dispatch(String(), value, args)
        return _dispatch(type_.contained, value, args)
    if isinstance(type_, Regexp) or (
        isinstance(type_, _PNamedType) and type_.TYPE_NAME in _OUR_UNSUPPORTED_NAMES
    ):
        raise HieraLookupError(
            "hyera does not support new() for the Puppet type '{}'".format(type_)
        )
    _not_supported(type_)


def _not_supported(type_):
    raise HieraLookupError(
        "Creation of new instance of type '{}' is not supported".format(type_)
    )


def _arity(function, expected, given):
    raise HieraLookupError("'{}' expects {}, got {}".format(function, expected, given))


def _is_nan(value):
    return isinstance(value, float) and value != value


def _ruby_text(value):
    """How Ruby interpolates a value into an error message."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cannot_convert(function, value, target):
    if isinstance(value, str):
        text = "The string '{}' cannot be converted to {}".format(value, target)
    else:
        text = "Value of type {} cannot be converted to {}".format(
            infer_generic(value), target
        )
    return HieraLookupError("'{}' {}".format(function, text))


def _is_named_arguments(value, args):
    """Whether ``new()`` got the ``{from, abs}`` hash form of ``Float`` or
    ``Numeric``: a hash with ``from`` and at most ``abs``, of the right types.
    Any other hash is just a value that cannot be converted."""
    return (
        isinstance(value, dict)
        and not args
        and "from" in value
        and set(value) <= {"from", "abs"}
        and isinstance(value.get("abs", False), bool)
        and _float_convertible(value["from"])
    )


def _named_arguments(function, target, hash_, allowed):
    """The ``{from, ...}`` form of ``Integer``/``Float``/``Numeric``'s
    ``new()``: returns the arguments in positional order."""
    for key in hash_:
        if key not in allowed:
            raise HieraLookupError(
                "{}.new has wrong type, unrecognized key '{}'".format(target, key)
            )
    if "from" not in hash_:
        raise HieraLookupError(
            "{}.new has wrong type, expects a value for key 'from'".format(target)
        )
    positional = [hash_["from"]]
    for key in allowed[1:]:
        if key in hash_ and hash_[key] is not None:
            positional.append(hash_[key])
        else:
            positional.append(_NO_RADIX if key == "radix" else False)
    return positional


# ------------------------------------------------------------------ Integer


def _parse_int_auto(s, radix=None):
    """``s`` must already match ``_LENIENT_INT_RE`` -- callers check that
    (and, for Integer specifically, the radix) before calling this."""
    sign = ""
    body = s.lstrip(_WHITESPACE)
    if body and body[0] in "+-":
        sign = body[0]
        body = body[1:]
    body = body.lstrip(_WHITESPACE)
    if radix is not None:
        value = int(body, radix)
    elif body[:2].lower() == "0x":
        value = int(body, 16)
    elif body[:2].lower() == "0b":
        value = int(body, 2)
    elif body.startswith("0") and len(body) > 1:
        value = int(body, 8)
    else:
        value = int(body, 10)
    return -value if sign == "-" else value


def _integer_convertible(value):
    if isinstance(value, bool) or isinstance(value, int):
        return True
    if isinstance(value, float):
        return not _is_nan(value)
    return isinstance(value, str) and _LENIENT_INT_RE.match(value) is not None


def _radix_error(radix):
    return HieraLookupError(
        "Illegal radix: {}, expected 2, 8, 10, 16, or default".format(_ruby_text(radix))
    )


def _integer_named_arguments(hash_):
    """``Integer.new({from => ..., radix => ..., abs => ...})``: the
    positional arguments, or Puppet's error for a hash that is not one."""
    from .parser import parse_type

    if "from" in hash_ and not _integer_convertible(hash_["from"]):
        raise _cannot_convert("new", hash_["from"], "Integer")
    radix = hash_.get("radix")
    if radix is not None and not (
        isinstance(radix, int) and not isinstance(radix, bool) and radix in _RADICES
    ):
        raise _radix_error(radix)
    assert_instance_of("Integer.new", parse_type(_INTEGER_NAMED_ARGS), hash_)
    return [
        hash_["from"],
        _NO_RADIX if radix is None else radix,
        hash_.get("abs") or False,
    ]


_INTEGER_NAMED_ARGS = (
    "Struct[{from => Variant[Numeric, Boolean, "
    r"Pattern[/\A[+-]?\s*(?:[0-9]+|0[xX][0-9A-Fa-f]+|0[bB][01]+)\z/]], "
    "Optional[radix] => Variant[Integer[2, 2], Integer[8, 8], Integer[10, 10], "
    "Integer[16, 16]], Optional[abs] => Boolean}]"
)


def _new_integer(value, *args):
    if len(args) > 2:
        _arity("new", "between 1 and 3 arguments", 1 + len(args))
    if isinstance(value, dict) and not args:
        value, *args = _integer_named_arguments(value)
    radix = args[0] if args else _NO_RADIX
    absolute = args[1] if len(args) > 1 else False
    radix_ok = radix is _NO_RADIX or (
        isinstance(radix, int) and not isinstance(radix, bool) and radix in _RADICES
    )
    if not radix_ok:
        raise _radix_error(radix)
    if not isinstance(absolute, bool) or not _integer_convertible(value):
        raise _cannot_convert("new", value, "Integer")
    result = _integer_value(value, None if radix is _NO_RADIX else radix)
    return abs(result) if absolute else result


def _integer_value(value, radix):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value in (float("inf"), float("-inf")):
            raise HieraLookupError(_ruby_text(value).replace("inf", "Infinity"))
        return int(value)
    try:
        return _parse_int_auto(value, radix)
    except ValueError:
        raise HieraLookupError('invalid value for Integer(): "{}"'.format(value))


# -------------------------------------------------------------------- Float


def _float_convertible(value):
    if isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return not _is_nan(value)
    return isinstance(value, str) and _FLOAT_RE.match(value) is not None


def _new_float(value, *args):
    if len(args) > 1:
        _arity("new_float", "between 1 and 2 arguments", 1 + len(args))
    if _is_named_arguments(value, args):
        value, *args = _named_arguments("new_float", "Float", value, ("from", "abs"))
    absolute = args[0] if args else False
    if not isinstance(absolute, bool) or not _float_convertible(value):
        raise _cannot_convert("new_float", value, "Float")
    result = _float_value(value)
    return abs(result) if absolute else result


def _float_value(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    body = value.lstrip(_WHITESPACE)
    sign = ""
    if body[:1] in ("+", "-"):
        sign, body = body[0], body[1:].lstrip(_WHITESPACE)
    if value[:1] == "0" and value[1:2] in ("b", "B"):
        return float(int(value, 2))
    if body[:2] in ("0b", "0B"):
        raise HieraLookupError('invalid value for Float(): "{}"'.format(value))
    if body[:2] in ("0x", "0X"):
        number = float(int(body, 16))
    else:
        number = float(body)
    return -number if sign == "-" else number


# ------------------------------------------------------------------ Numeric


def _new_numeric(value, *args):
    if len(args) > 1:
        _arity("new_numeric", "between 1 and 2 arguments", 1 + len(args))
    if _is_named_arguments(value, args):
        value, *args = _named_arguments(
            "new_numeric", "Numeric", value, ("from", "abs")
        )
    absolute = args[0] if args else False
    if not isinstance(absolute, bool) or not _float_convertible(value):
        raise _cannot_convert("new_numeric", value, "Numeric")
    result = _numeric_value(value)
    return abs(result) if absolute and result is not None else result


def _numeric_value(value):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    if value[:1] == "0" and value[1:2] in ("b", "B", "x", "X"):
        try:
            return int(value, 0)
        except ValueError:
            raise HieraLookupError('invalid value for Integer(): "{}"'.format(value))
    match = _NUMERIC_RE.match(value)
    if not match:
        return None
    sign, number, _, integer, fraction = match.groups()
    if fraction:
        if int(integer) == 0 and re.match(r"\A\.?0*[eE]", fraction):
            return None
        result = float(number)
        if result in (float("inf"), float("-inf")):
            return None
        return -result if sign == "-" else result
    try:
        result = _parse_int_auto(number)
    except ValueError:
        return None
    return -result if sign == "-" else result


# ------------------------------------------------------------------ Boolean


def _new_boolean(value, *args):
    if args:
        _arity("new_boolean", "1 argument", 1 + len(args))
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not _is_nan(value):
        return value != 0
    if isinstance(value, str):
        w = value.lower()
        if w in _BOOL_WORDS:
            return _BOOL_WORDS[w]
    raise _cannot_convert("new_boolean", value, "Boolean")


# -------------------------------------------------------------------- String


def _new_string(value, *args):
    if len(args) > 1:
        _arity("new_string", "between 1 and 2 arguments", 1 + len(args))
    return _string_convert(value, args[0] if args else _NO_FORMAT)


# --------------------------------------------------------- Array iteration


def _iterate_to_list(value, target_name):
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return list(value)
    if isinstance(value, dict):
        return [[k, v] for k, v in value.items()]
    # Array's own dispatch rejects through argument_mismatch ("'new_array'
    # ..."); Hash's from_array raises plainly.
    prefix = "'new_array' " if target_name == "Array" else ""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return list(range(value))
    raise HieraLookupError(
        "{}Value of type {} cannot be converted to {}".format(
            prefix, infer_generic(value), target_name
        )
    )


def _new_array(value, *args):
    if len(args) > 1:
        _arity("new_array", "between 1 and 2 arguments", 1 + len(args))
    wrap = args[0] if args else False
    if not isinstance(wrap, bool):
        raise HieraLookupError(
            "'new_array' parameter 'wrap' expects a Boolean value, got {}".format(
                infer_set(wrap).name
            )
        )
    if wrap:
        return list(value) if isinstance(value, (list, tuple)) else [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return _iterate_to_list(value, "Array")


def _new_hash(value, *args):
    if args:
        raise HieraLookupError(
            "hyera does not support the build option of the Hash new() function"
        )
    if isinstance(value, dict):
        return dict(value)
    items = _iterate_to_list(value, "Hash")
    # A list of already-paired [k, v] 2-element lists converts pair-wise
    # (Ruby's Array#to_h); anything else is a flat list paired up
    # sequentially (Ruby's Hash[*array]).
    if items and all(isinstance(e, (list, tuple)) and len(e) == 2 for e in items):
        pairs = [(k, v) for k, v in items]
    elif len(items) % 2 != 0:
        raise HieraLookupError("odd number of arguments for Hash")
    else:
        pairs = [(items[i], items[i + 1]) for i in range(0, len(items), 2)]
    try:
        return dict(pairs)
    except TypeError as e:
        # Ruby allows an Array or Hash as a key; a Python dict does not.
        raise HieraLookupError("unusable Hash key: {}".format(e)) from None
