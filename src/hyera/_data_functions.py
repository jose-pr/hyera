"""Puppet's ``dig()``/``get()``/``getvar()`` functions, ported onto an
already-resolved value (``dig``/``get``) or the bound ``Scope``
(``getvar``): ``functions/dig.rb``, ``functions/get.rb``,
``functions/getvar.rb``.

Original code; no phiera/Puppet-source header (see ``core.Hiera.dig``/
``.get``/``.getvar``, the thin methods wrapping this module).
"""

import re

from ._interpolation import _ruby_inspect
from ._navigation import split_key
from ._types import infer
from .exceptions import HieraLookupError

#: getvar.rb:50 -- must start with a valid (optionally ``::``-qualified)
#: Puppet variable name, immediately followed by ``.`` or the end of the
#: string. ``(?a)`` restricts Puppet's ASCII-only ``\w`` (Python's default
#: ``\w`` also matches Unicode word characters).
_VALID_START_RE = re.compile(r"(?a)\A(?:::)?(?:[a-z]\w*::)*[a-z_]\w*(?:\.|\Z)")
#: getvar.rb:69 -- splits the leading variable name from the rest.
_NAME_RE = re.compile(r"(?a)^((?:::)?(?:\w+::)*\w+)")


class _DigError(HieraLookupError):
    """Puppet's ``Puppet::ErrorWithData`` (``dig.rb``): the two walk errors
    -- a non-collection reached mid-walk, or a non-integer index against a
    list. Only these reach a ``get()`` block; every other error (the
    top-level data-parameter check, a dotted-navigation syntax error) always
    raises."""


def dig(data, keys):
    """Puppet's ``dig()`` (``functions/dig.rb:33-65``): walk ``keys`` into
    ``data``, Ruby ``Hash#dig``/``Array#dig`` style.

    ``data`` must be ``None``, a ``list`` or a ``dict``, else
    :class:`~hyera.HieraLookupError` (the dispatcher's own message).
    ``None`` (as the starting value, or met partway through) or a ``None``
    key stops the walk with ``None`` -- not an error. A non-collection value
    that still has keys left to dig, or a non-``int`` key against a
    ``list`` (a ``bool`` does not count as an ``int`` here, matching Ruby),
    raises :class:`_DigError` naming the path walked so far
    (``_ruby_inspect``) and the offending value's/key's Puppet type. A
    ``list`` index follows Ruby's negative-index/out-of-range rules
    (negative counts from the end, out of range is ``None``); a ``dict``
    key matches only a key of the exact same kind, as Ruby ``eql?`` does
    (``True`` is never ``1``, ``1`` is never ``1.0``).
    """
    if data is not None and not isinstance(data, (list, dict)):
        raise HieraLookupError(
            "'dig' parameter 'data' expects a value of type Undef or "
            "Collection, got {}".format(infer(data).generalize())
        )
    value = data
    walked = []
    for key in keys:
        if value is None or key is None:
            return None
        if not isinstance(value, (list, dict)):
            raise _DigError(
                "The given data does not contain a Collection at {}, "
                "got '{}'".format(_ruby_inspect(walked), infer(value))
            )
        walked.append(key)
        if isinstance(value, list):
            if isinstance(key, bool) or not isinstance(key, int):
                raise _DigError(
                    "The given data requires an Integer index at {}, "
                    "got '{}'".format(_ruby_inspect(walked), infer(key))
                )
            if -len(value) <= key < len(value):
                value = value[key]
            else:
                value = None
            continue
        # dict: match only a key of the identical kind (Ruby eql?).
        key_type = type(key)
        found = None
        hit = False
        for k, v in value.items():
            if type(k) is key_type and k == key:
                found = v
                hit = True
                break
        value = found if hit else None
    return value


def get_segments(value, segments, default_value, block):
    """Puppet's shared tail of ``get()``/``getvar()``
    (``functions/get.rb:135-146``): dig ``segments`` out of ``value``,
    substituting ``default_value`` for ``None``/a miss.

    A :class:`_DigError` reaches ``block(error)`` when a block is given
    (its return value is used as-is), otherwise it propagates.

    Returns ``(result, subject)``: ``subject`` is Puppet's own name for
    where ``result`` came from ("Found value", "Default value" or "Value
    returned from block"), for a caller that asserts a ``value_type``
    against it (``core.Hiera.get``); a caller with no such use (``get()``/
    ``getvar()`` themselves) reads just ``result``.
    """
    if value is None:
        return default_value, "Default value"
    if not segments:
        return value, "Found value"
    try:
        result = dig(value, segments)
    except _DigError as e:
        if block is not None:
            return block(e), "Value returned from block"
        raise
    if result is None:
        return default_value, "Default value"
    return result, "Found value"


def get(value, navigation, default_value=None, block=None):
    """Puppet's ``get()`` (``functions/get.rb:118-146``) over an
    already-looked-up ``value``: dig Puppet's dotted-navigation string
    ``navigation`` out of it.

    An empty ``navigation`` returns ``value`` itself, untouched. Otherwise
    ``navigation`` is parsed with the same dotted-key grammar a lookup key
    uses (quoted segments, numeric segments as ``int``), raising
    ``HieraLookupError("Syntax error in dotted-navigation string")`` on a
    malformed one.
    """
    if navigation == "":
        return value
    segments = split_key(
        "x." + navigation,
        lambda _problem: HieraLookupError("Syntax error in dotted-navigation string"),
    )[1:]
    return get_segments(value, segments, default_value, block)[0]


def getvar(scope, navigation, default_value=None, block=None):
    """Puppet's ``getvar()`` (``functions/getvar.rb:48-85``): ``get()``
    over a scope variable's value instead of a looked-up one.

    ``navigation`` must start with a valid (optionally ``::``-qualified)
    Puppet variable name immediately followed by ``.`` or the string's end,
    else ``HieraLookupError("'getvar' The given string does not start with
    a valid variable name")``. An undefined variable returns
    ``default_value`` regardless of the scope's ``strict`` (Puppet's own
    ``catch(:undefined_variable)``) -- never raises for that reason alone.
    """
    if not _VALID_START_RE.search(navigation):
        raise HieraLookupError(
            "'getvar' The given string does not start with a valid variable name"
        )
    name_match = _NAME_RE.match(navigation)
    name = name_match.group(1)
    rest = navigation[name_match.end() :]
    if rest and not rest.startswith("."):
        raise HieraLookupError(
            "First character after var name in get string must be a "
            "'.' - got {}".format(rest[0])
        )
    value = scope.lookup(name)
    if value is scope.UNDEFINED:
        return default_value
    return get(value, rest[1:], default_value, block)
