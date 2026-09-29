"""Puppet type system: Sensitive, convert_to.

Ports Puppet's ``pops/types`` (``p_sensitive_type.rb``).
"""

import logging

_LOGGER = logging.getLogger(__name__)


class Sensitive(object):
    """Thin marker wrapping a value flagged ``Sensitive`` via ``convert_to``.

    ``str()`` redacts; ``.unwrap()`` returns the real value. Mirrors Puppet's
    Sensitive type without pulling in a dependency.
    """

    def __init__(self, value):
        self._value = value

    def unwrap(self):
        return self._value

    def __repr__(self):
        return "Sensitive(<redacted>)"

    __str__ = __repr__


def _convert_to(value, spec):
    """Best-effort ``convert_to`` cast. Unknown types leave the value as-is.

    ``spec`` is a type name (``"Integer"``) or ``[name, *args]``. Kept
    dependency-free and non-raising so unattended lookups never crash on a
    cast; a failed/unknown cast logs at debug and returns the original value.
    """
    args = []
    if isinstance(spec, (list, tuple)):
        name, args = spec[0], list(spec[1:])
    else:
        name = spec
    try:
        if name == "Integer":
            return int(value, *(args or []))
        if name == "Float":
            return float(value)
        if name == "String":
            return str(value)
        if name == "Boolean":
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "1", "on")
            return bool(value)
        if name == "Array":
            if isinstance(value, list):
                return value
            return [value]
        if name == "Sensitive":
            return Sensitive(value)
    except (ValueError, TypeError) as e:
        _LOGGER.debug("convert_to %s failed for %r: %s", name, value, e)
        return value
    _LOGGER.debug("convert_to: unknown type %r; leaving value unchanged", name)
    return value
