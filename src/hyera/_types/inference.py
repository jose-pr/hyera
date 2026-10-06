# Ported from Puppet 8 lib/puppet/pops/types/type_calculator.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Inferring the Puppet type of a Python value.

Ports ``TypeCalculator#infer`` and its ``infer_set`` and generalizing
variants from Puppet's ``pops/types/type_calculator.rb``.
"""

from __future__ import annotations

import re

from .types import (
    UNDEF,
    Boolean,
    Float,
    Integer,
    Regexp,
    Sensitive,
    SensitiveType,
    String,
    generalize,
)
from .compound_types import (
    Array,
    Hash,
    Runtime,
    Struct,
    StructElement,
    Tuple,
    Variant,
)


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


def _generalized_common(types):
    types = list(types)
    uniq = []
    for t in types:
        if not any(t == u for u in uniq):
            uniq.append(t)
    if len(uniq) == 1:
        return uniq[0]
    return Variant(uniq)
