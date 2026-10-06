# Ported from Puppet 8 lib/puppet/pops/types/types.rb,
# lib/puppet/pops/loader/static_loader.rb (https://github.com/puppetlabs/puppet),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Puppet's collection and reference types and the aliases built from them.

Ports ``PCollectionType``, ``PArrayType``, ``PHashType``, ``PTupleType``,
``PStructType``, ``PVariantType``, ``PRuntimeType`` and ``PTypeAliasType`` of
``pops/types/types.rb``, and the static loader's type aliases.
"""

from __future__ import annotations

from .types import (
    Any,
    _key_of,
    _render_size_args,
    _type_instance,
    generalize,
    puppet_quote,
)


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


COLLECTION = Collection()
