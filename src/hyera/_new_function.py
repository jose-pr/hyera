"""Puppet's ``new()``: ``functions/new.rb`` plus each type's own
``new_function`` this subset ports.

``new_instance(type_, value, *args)``: one argument already an instance of
``type_`` (and no ``*args``) is returned unchanged; else the type's own
conversion runs, and the result is asserted against ``type_`` with the
subject ``"Converted value from <type_>.new()"`` (``_type_mismatch``).
Types outside this subset's new()-capable tier (SemVer, SemVerRange, Timespan, Timestamp, Regexp, Binary, URI, Type,
Object) raise our own "hiera does not support new()" text -- a deliberate
deviation, not a bug, since Puppet itself does support new() for several
of them.
"""

import re

from .exceptions import HieraLookupError
from ._string_converter import convert as _string_convert
from ._type_mismatch import assert_instance_of, short_name
from ._types import (
    PAnyType,
    PArrayType,
    PBooleanType,
    PFloatType,
    PHashType,
    PIntegerType,
    PNotUndefType,
    PNumericType,
    POptionalType,
    PRegexpType,
    PSensitiveType,
    PStringType,
    PStructType,
    PTupleType,
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

_LENIENT_INT_RE = re.compile(r"^[+-]?\s*(?:\d+|0[xX][0-9A-Fa-f]+|0[bB][01]+)$")
_HEXBIN_RE = re.compile(r"^0[xX][0-9A-Fa-f]+$|^0[bB][01]+$")
_BOOL_WORDS = {
    "true": True,
    "yes": True,
    "y": True,
    "false": False,
    "no": False,
    "n": False,
}


def new_instance(type_, value, *args):
    """Puppet's ``new_instance(t, *args)`` (``functions/new.rb``)."""
    if not isinstance(type_, PAnyType):
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
    if isinstance(type_, PIntegerType):
        return _new_integer(value, *args)
    if isinstance(type_, PFloatType):
        return _new_float(value, *args)
    if isinstance(type_, PNumericType):
        return _new_numeric(value, *args)
    if isinstance(type_, PStringType):
        return _new_string(value, *args)
    if isinstance(type_, PBooleanType):
        return _new_boolean(value, *args)
    if isinstance(type_, PSensitiveType):
        return Sensitive(value)
    if isinstance(type_, (PTupleType, PArrayType)):
        wrap = args[0] if args else False
        return _new_array(value, wrap)
    if isinstance(type_, (PStructType, PHashType)):
        return _new_hash(value)
    if isinstance(type_, (POptionalType, PNotUndefType)):
        if type_.contained is None:
            _not_supported(type_)
        if isinstance(type_.contained, str):
            # A literal string argument (``Optional['x']``/``Optional[x]``,
            # as Puppet does): Puppet's own new() dispatches through the
            # literal's generalized data type (String), then the caller's
            # assert_instance_of checks the result against the literal type
            # itself -- so a value equal to the literal converts cleanly and
            # anything else is a wrong-type mismatch, never "not supported".
            return _dispatch(PStringType(), value, args)
        return _dispatch(type_.contained, value, args)
    if isinstance(type_, PRegexpType) or (
        isinstance(type_, _PNamedType) and type_.TYPE_NAME in _OUR_UNSUPPORTED_NAMES
    ):
        raise HieraLookupError(
            "hiera does not support new() for the Puppet type '{}'".format(type_)
        )
    _not_supported(type_)


def _not_supported(type_):
    raise HieraLookupError(
        "Creation of new instance of type '{}' is not supported".format(type_)
    )


# ------------------------------------------------------------------ Integer


def _parse_int_auto(s, radix=None):
    """``s`` must already match ``_LENIENT_INT_RE`` -- callers check that
    (and, for Integer specifically, the radix) before calling this."""
    sign = ""
    body = s
    if body and body[0] in "+-":
        sign = body[0]
        body = body[1:]
    body = body.lstrip()
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


def _new_integer(value, *args):
    if len(args) > 2:
        raise HieraLookupError(
            "'new' expects between 1 and 3 arguments, got {}".format(1 + len(args))
        )
    radix = args[0] if args else None
    if radix is not None and (isinstance(radix, bool) or not isinstance(radix, int)):
        # no signature/argument_mismatch accepts this shape;
        # ours, not Puppet's own multi-line dispatch listing.
        names = ", ".join(infer_generic(v).name for v in (value,) + tuple(args))
        raise HieraLookupError("'new' does not accept the arguments ({})".format(names))
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        if radix is not None and radix not in (2, 8, 10, 16):
            raise HieraLookupError(
                "Illegal radix: {}, expected 2, 8, 10, 16, or default".format(radix)
            )
        if not _LENIENT_INT_RE.match(value):
            raise HieraLookupError(
                "'new' The string '{}' cannot be converted to Integer".format(value)
            )
        try:
            result = _parse_int_auto(value, radix)
        except ValueError:
            raise HieraLookupError('invalid value for Integer(): "{}"'.format(value))
        return result
    if isinstance(value, dict):
        # Puppet's named-args form ({from, radix, abs}); we accept none of
        # its keys, so any key is "unrecognized" -- not otherwise ported.
        for k in value:
            raise HieraLookupError(
                "Integer.new has wrong type, unrecognized key '{}'".format(k)
            )
    raise HieraLookupError(
        "'new' Value of type {} cannot be converted to Integer".format(
            infer_generic(value)
        )
    )


# -------------------------------------------------------------------- Float


def _new_float(value, *args):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        sign = ""
        body = value
        if body and body[0] in "+-":
            sign = body[0]
            body = body[1:]
        body = body.strip()
        if _HEXBIN_RE.match(body):
            base = 16 if body[1] in "xX" else 2
            try:
                v = float(int(body, base))
            except ValueError:
                raise HieraLookupError(
                    "'new_float' The string '{}' cannot be converted to Float".format(
                        value
                    )
                )
            return -v if sign == "-" else v
        try:
            return float(sign + body)
        except ValueError:
            raise HieraLookupError(
                "'new_float' The string '{}' cannot be converted to Float".format(value)
            )
    raise HieraLookupError(
        "'new_float' Value of type {} cannot be converted to Float".format(
            infer_generic(value)
        )
    )


# ------------------------------------------------------------------ Numeric


def _new_numeric(value, *args):
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        if _LENIENT_INT_RE.match(value):
            return _parse_int_auto(value)
        try:
            return float(value.strip())
        except ValueError:
            raise HieraLookupError(
                "'new_numeric' The string '{}' cannot be converted to Numeric".format(
                    value
                )
            )
    raise HieraLookupError(
        "'new_numeric' Value of type {} cannot be converted to Numeric".format(
            infer_generic(value)
        )
    )


# ------------------------------------------------------------------ Boolean


def _new_boolean(value, *args):
    if args:
        raise HieraLookupError(
            "'new_boolean' expects 1 argument, got {}".format(1 + len(args))
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        w = value.lower()
        if w in _BOOL_WORDS:
            return _BOOL_WORDS[w]
        raise HieraLookupError(
            "'new_boolean' The string '{}' cannot be converted to Boolean".format(value)
        )
    raise HieraLookupError(
        "'new_boolean' Value of type {} cannot be converted to Boolean".format(
            infer_generic(value)
        )
    )


# -------------------------------------------------------------------- String


def _new_string(value, *args):
    fmt = args[0] if args else None
    return _string_convert(value, fmt)


# --------------------------------------------------------- Array iteration


def _iterate_to_list(value, target_name):
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return list(value)
    if isinstance(value, dict):
        return [[k, v] for k, v in value.items()]
    if isinstance(value, bool):
        raise HieraLookupError(
            "'new_{}' Value of type Boolean cannot be converted to {}".format(
                target_name.lower(), target_name
            )
        )
    if isinstance(value, int) and value >= 0:
        return list(range(value))
    raise HieraLookupError(
        "'new_{}' Value of type {} cannot be converted to {}".format(
            target_name.lower(), infer_generic(value), target_name
        )
    )


def _new_array(value, wrap=False):
    if isinstance(value, (list, tuple)):
        return list(value)
    if wrap:
        return [value]
    return _iterate_to_list(value, "Array")


def _new_hash(value):
    if isinstance(value, dict):
        return dict(value)
    items = _iterate_to_list(value, "Hash")
    # A list of already-paired [k, v] 2-element lists converts pair-wise
    # (Ruby's Hash[*array] on an array-of-pairs); anything else is a flat
    # list paired up sequentially.
    if items and all(isinstance(e, (list, tuple)) and len(e) == 2 for e in items):
        return {k: v for k, v in items}
    if len(items) % 2 != 0:
        raise HieraLookupError("odd number of arguments for Hash")
    return {items[i]: items[i + 1] for i in range(0, len(items), 2)}
