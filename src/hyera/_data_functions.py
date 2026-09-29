"""Puppet's ``dig()`` function, ported onto an already-resolved value:
``functions/dig.rb``.

Original code; no phiera/Puppet-source header (see ``core.Hiera.dig``, the
thin method wrapping this module).
"""

from ._interpolation import _ruby_inspect
from ._types import infer
from .exceptions import HieraLookupError


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
