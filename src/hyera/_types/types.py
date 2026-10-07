# Ported from Puppet 8 lib/puppet/pops/types/{types,type_calculator,type_formatter,
# p_sensitive_type}.rb (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by
# jose-pr. See NOTICE.
"""Puppet type system: the type model, Sensitive, convert_to.

Ports Puppet's ``pops/types`` (``types.rb``, ``type_calculator.rb``,
``type_formatter.rb``, ``p_sensitive_type.rb``). This module imports only
:mod:`hyera._types.literal_format` and :mod:`hyera._types.ruby_regexp`;
:mod:`hyera._types.parser` builds type instances from a Puppet type-expression
string via ``parse_type``.

Each class here is Puppet's own type-expression name (``Any``, ``Integer``,
``Optional``, ...) rather than Ruby's ``P<Name>Type`` (``PAnyType``,
``PIntegerType``, ``POptionalType``, ...); the one exception is
:class:`SensitiveType`, kept out of Ruby's bare ``Sensitive`` name because
:class:`hyera.Sensitive` (this module's own value wrapper) already has it.
The classes stay private.
"""

from __future__ import annotations

import re
import typing as _ty

from ..exceptions import HieraLookupError
from .literal_format import _literal_str, _num_str, puppet_quote
from .ruby_regexp import _ruby_regex


class _Sealing(type):
    """Metaclass of the type model: an instance is read-only once its
    constructor returns, so a shared (cached) type object cannot be changed
    through any reference to it."""

    def __call__(cls, *args: _ty.Any, **kwargs: _ty.Any) -> _ty.Any:
        obj = super().__call__(*args, **kwargs)
        object.__setattr__(obj, "_sealed", True)
        return obj


class Any(metaclass=_Sealing):
    """Base of the ported Puppet type model (``types.rb``)."""

    __slots__ = ("_sealed",)

    def __setattr__(self, name: str, value: _ty.Any) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("type objects are immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("type objects are immutable")

    #: Puppet class name, without the leading ``P``/trailing ``Type``.
    TYPE_NAME = "Any"

    def instance(self, value: _ty.Any) -> bool:
        """Whether ``value`` is a Puppet instance of this type."""
        return True

    def generalize(self) -> "Any":
        """This type with any literal narrowing removed (e.g. a literal
        ``String`` loses its ``.literal``), Puppet's own ``generalize``."""
        return self

    @property
    def name(self) -> str:
        """This type's own Puppet name, with any parameters."""
        return self.TYPE_NAME

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        """Puppet's own type text, the same as :meth:`__str__`
        (``Integer[1, 3]``), so a type object reads back as what it means."""
        return str(self)

    def _key(self):
        return ()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Any):
            return NotImplemented
        return type(self) is type(other) and self._key() == other._key()

    def __hash__(self) -> int:
        return hash((type(self), self._key()))

    def __instancecheck__(self, value: _ty.Any) -> bool:
        """Lets a type *object* stand in directly as ``isinstance()``'s
        second argument (``isinstance(5, Integer[1, 3])``): defined on the
        class body, so it is found via ``type(<this instance>).
        __instancecheck__`` -- Python's normal dunder lookup for the
        instance used as the ``isinstance`` class argument -- without
        touching how ``isinstance(x, Integer)`` (the bare *class*) resolves,
        which still goes through ``type(Integer).__instancecheck__`` (the
        builtin ``type.__instancecheck__``, untouched). See
        :mod:`hyera.types`."""
        return self.instance(value)

    def __call__(self, *args: _ty.Any) -> _ty.Any:
        """A type *object* called directly (``Integer[1, 10]("5")``) is
        Puppet's ``new()`` against this exact type, asserting any
        parameters (a range, a size) the same way `.new()` on the bare type
        would. Importing :mod:`hyera._types.new_function` lazily avoids a
        circular import (it imports this module to dispatch on these
        classes). See :mod:`hyera.types`, whose facade classes delegate a
        *bare* call (``Integer("5")``) to this same method on their own
        default type object."""
        from .new_function import new_instance

        try:
            return new_instance(self, *args)
        except HieraLookupError as e:
            raise ValueError(str(e)) from None


class Undef(Any):
    __slots__ = ()
    TYPE_NAME = "Undef"

    def instance(self, value):
        return value is None


class NotUndef(Any):
    __slots__ = ("contained",)
    TYPE_NAME = "NotUndef"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if value is None:
            return False
        if self.contained is None:
            return True
        return _type_instance(self.contained, value)

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("NotUndef", self.contained, show_literal=True)


class Optional(Any):
    __slots__ = ("contained",)
    TYPE_NAME = "Optional"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if value is None:
            return True
        if self.contained is None:
            return False
        return _type_instance(self.contained, value)

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("Optional", self.contained, show_literal=True)


def _is_nan(value):
    return isinstance(value, float) and value != value


class Scalar(Any):
    __slots__ = ()
    TYPE_NAME = "Scalar"

    def instance(self, value):
        return isinstance(value, (bool, int, float, str, re.Pattern))


class ScalarData(Scalar):
    __slots__ = ()
    TYPE_NAME = "ScalarData"

    def instance(self, value):
        return isinstance(value, (bool, int, float, str))


class Numeric(Any):
    __slots__ = ()
    TYPE_NAME = "Numeric"

    def instance(self, value):
        if isinstance(value, bool) or _is_nan(value):
            return False
        return isinstance(value, (int, float))


class Integer(Any):
    __slots__ = ("from_", "to")
    TYPE_NAME = "Integer"

    def __init__(self, from_=None, to=None):
        self.from_ = from_
        self.to = to

    DEFAULT = None  # set below

    def instance(self, value):
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if self.from_ is not None and value < self.from_:
            return False
        if self.to is not None and value > self.to:
            return False
        return True

    def generalize(self):
        return Integer.DEFAULT

    def _key(self):
        return (self.from_, self.to)

    def __str__(self):
        if self.from_ is None and self.to is None:
            return "Integer"
        if self.to is None:
            return "Integer[{}]".format(_num_str(self.from_))
        return "Integer[{}, {}]".format(_num_str(self.from_), _num_str(self.to))


class Float(Any):
    __slots__ = ("from_", "to")
    TYPE_NAME = "Float"

    def __init__(self, from_=None, to=None):
        self.from_ = from_
        self.to = to

    DEFAULT = None  # set below

    def instance(self, value):
        if not isinstance(value, float) or value != value:
            return False
        if self.from_ is not None and value < self.from_:
            return False
        if self.to is not None and value > self.to:
            return False
        return True

    def generalize(self):
        return Float.DEFAULT

    def _key(self):
        return (self.from_, self.to)

    def __str__(self):
        if self.from_ is None and self.to is None:
            return "Float"
        if self.to is None:
            return "Float[{}]".format(_num_str(self.from_))
        return "Float[{}, {}]".format(_num_str(self.from_), _num_str(self.to))


class String(Any):
    __slots__ = ("size_from", "size_to", "literal")
    TYPE_NAME = "String"

    def __init__(self, size_from=None, size_to=None, literal=None):
        self.size_from = size_from
        self.size_to = size_to
        # : set only for the "value" flavor (``TypeFactory.string(literal)``):
        # Optional/NotUndef render the : raw literal (see ``_render_container``);
        # everywhere else it renders as bare ``String``.
        self.literal = literal

    DEFAULT = None  # set below

    def instance(self, value):
        if not isinstance(value, str):
            return False
        if self.literal is not None:
            return value == self.literal
        if self.size_from is None and self.size_to is None:
            return True
        n = len(value)
        if self.size_from is not None and n < self.size_from:
            return False
        if self.size_to is not None and n > self.size_to:
            return False
        return True

    def generalize(self):
        return String.DEFAULT

    def _key(self):
        return (self.size_from, self.size_to, self.literal)

    def __str__(self):
        if self.literal is not None:
            return "String"
        if self.size_from is None and self.size_to is None:
            return "String"
        if self.size_to is None:
            return "String[{}]".format(_num_str(self.size_from))
        return "String[{}, {}]".format(_num_str(self.size_from), _num_str(self.size_to))


class Boolean(Any):
    __slots__ = ("value",)
    TYPE_NAME = "Boolean"

    def __init__(self, value=None):
        #: ``None`` = unconstrained; ``True``/``False`` = exactly that value
        #: (``Boolean.new(true)``/``new(false)``, what ``infer()``
        #: gives a literal ``bool``).
        self.value = value

    def instance(self, value):
        if not isinstance(value, bool):
            return False
        return self.value is None or value == self.value

    def generalize(self):
        return BOOLEAN

    def _key(self):
        return (self.value,)

    def __str__(self):
        if self.value is None:
            return "Boolean"
        return "Boolean[{}]".format("true" if self.value else "false")


class Regexp(Any):
    __slots__ = ("source",)
    TYPE_NAME = "Regexp"

    def __init__(self, source=None):
        self.source = source

    def instance(self, value):
        if not isinstance(value, re.Pattern):
            return False
        if self.source is None:
            return True
        return value.pattern == self.source

    def _key(self):
        return (self.source,)

    def __str__(self):
        if self.source is None:
            return "Regexp"
        return "Regexp[/{}/]".format(self.source)


class Pattern(Any):
    __slots__ = ("sources", "_compiled")
    TYPE_NAME = "Pattern"

    def __init__(self, sources):
        self.sources = tuple(dict.fromkeys(sources))
        self._compiled = tuple(_ruby_regex(s) for s in self.sources)

    def instance(self, value):
        if not isinstance(value, str):
            return False
        if not self._compiled:
            return True
        return any(p.search(value) for p in self._compiled)

    def _key(self):
        return tuple(self.sources)

    def __str__(self):
        if not self.sources:
            return "Pattern"
        return "Pattern[{}]".format(", ".join("/{}/".format(s) for s in self.sources))


_ASCII_FOLD = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


class Enum(Any):
    __slots__ = ("values", "case_insensitive")
    TYPE_NAME = "Enum"

    def __init__(self, values, case_insensitive=False):
        self.values = tuple(dict.fromkeys(values))
        self.case_insensitive = bool(case_insensitive)

    def instance(self, value):
        if not isinstance(value, str):
            return False
        # An Enum with no values matches no string (types.rb:812).
        if self.case_insensitive:
            folded = value.translate(_ASCII_FOLD)
            return any(v.translate(_ASCII_FOLD) == folded for v in self.values)
        return value in self.values

    def _key(self):
        return (tuple(self.values), self.case_insensitive)

    def __str__(self):
        if not self.values:
            return "Enum"
        parts = [_literal_str(v) for v in self.values]
        if self.case_insensitive:
            parts.append("true")
        return "Enum[{}]".format(", ".join(parts))


class SensitiveType(Any):
    """Puppet's ``Sensitive`` type: an instance is a :class:`Sensitive`
    value whose wrapped value matches the (optional) contained type."""

    __slots__ = ("contained",)
    TYPE_NAME = "Sensitive"
    contained: "_ty.Optional[Any]"

    def __init__(self, contained: "_ty.Optional[Any]" = None) -> None:
        # Puppet keeps only the generalized contained type (a range or size
        # is dropped), and ``Sensitive[Any]`` is plain ``Sensitive``.
        general: "_ty.Optional[Any]" = None
        if contained is not None:
            candidate: _ty.Any = generalize(contained)
            general = None if type(candidate) is Any else candidate
        self.contained = general

    def instance(self, value: _ty.Any) -> bool:
        """Whether ``value`` is a :class:`Sensitive` wrapping an instance
        of this type's own contained type (any ``Sensitive`` at all, when
        unparameterized)."""
        if not isinstance(value, Sensitive):
            return False
        if self.contained is None:
            return True
        return _type_instance(self.contained, value.unwrap())

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self) -> str:
        return _render_container("Sensitive", self.contained)


#: Named-only types (second tier): full detail in
#: ``_types.parser.TYPE_MAP``; instances are always ``False``.
NAMED_ONLY_TYPES = (
    "default",
    "type",
    "typeset",
    "uri",
    "object",
    "binary",
    "semver",
    "semverrange",
    "timespan",
    "timestamp",
    "callable",
    "catalogentry",
    "class",
    "resource",
)

Integer.DEFAULT = Integer()
Float.DEFAULT = Float()
String.DEFAULT = String()
ANY = Any()
UNDEF = Undef()
SCALAR = Scalar()
SCALAR_DATA = ScalarData()
NUMERIC = Numeric()
BOOLEAN = Boolean()
REGEXP = Regexp()


def _render_container(name, contained, show_literal=False):
    """Optional/NotUndef/Sensitive's own formatter (``type_formatter.rb``
    ``string_POptionalType``/``string_PNotUndefType``/``string_PSensitiveType``):
    the contained type renders in full. ``show_literal`` is Optional/
    NotUndef's own special case (not Sensitive's): a literal ``String``
    child prints its quoted value."""
    if contained is None or (name == "NotUndef" and type(contained) is Any):
        return name
    if show_literal and isinstance(contained, String) and contained.literal is not None:
        return "{}[{}]".format(name, puppet_quote(contained.literal))
    if isinstance(contained, str):
        return "{}[{}]".format(name, puppet_quote(contained))
    return "{}[{}]".format(name, contained)


def _key_of(t):
    if t is None:
        return None
    if isinstance(t, Any):
        return t._key() + (type(t).__name__,)
    return t


def _type_instance(t, value):
    """``t.instance(value)``, where ``t`` may be a raw literal string (an
    Optional/NotUndef contained "type" that is really a literal, per Ruby's
    ``assert_type(ast, param) unless param.is_a?(String)``)."""
    if isinstance(t, str):
        return value == t
    return t.instance(value)


def generalize(t):
    if isinstance(t, Any):
        return t.generalize()
    return t


def _eql_key(value):
    """A recursive, hashable, type-tagged key implementing Ruby ``eql?``.

    Ruby's ``hash``/``eql?`` distinguish ``1``, ``1.0`` and ``true`` (unlike
    Python, where ``hash(1) == hash(1.0) == hash(True)`` and ``1 == 1.0 ==
    True``); a list or dict compares by content, a dict in any key order.
    Tag every value with its Ruby-relevant type before hashing/comparing so
    two values Ruby would consider unequal never collide.
    """
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (list, tuple)):
        return ("array", tuple(_eql_key(v) for v in value))
    if isinstance(value, dict):
        return ("hash", frozenset((_eql_key(k), _eql_key(v)) for k, v in value.items()))
    if value is None:
        return ("undef", None)
    if isinstance(value, Sensitive):
        return ("sensitive", _eql_key(value.unwrap()))
    # Anything else (an object with no Ruby equivalent): keyed by identity,
    # matching Ruby's default Object#hash/#eql?.
    return ("id", id(value))


class Sensitive:
    """Puppet's ``Sensitive`` value wrapper (``p_sensitive_type.rb:11-37``).

    ``str()``/``repr()`` both redact (Puppet's ``to_s``: "Sensitive [value
    redacted]"). Equality and hashing follow Puppet: two ``Sensitive``
    values are equal, and hash equal, exactly when their wrapped values are
    Ruby-``eql?`` -- so a list or dict payload hashes despite being
    unhashable in plain Python. ``.unwrap()`` returns the real value.

    :param value: the value to wrap.
    """

    __slots__ = ("_value",)

    def __init__(self, value: _ty.Any) -> None:
        self._value = value

    def __class_getitem__(cls, item: _ty.Any) -> "SensitiveType":
        """``Sensitive[T]``: the Puppet *type* ``Sensitive[T]`` (see
        :mod:`hyera.types`) -- a type object, never a value. Calling stays
        the value wrapper (``Sensitive("x")``), unaffected by this.

        :param item: a type object, a ``hyera.types`` class, or a Puppet
            type-expression string, for the contained type.
        :returns: the ``Sensitive[T]`` type object.
        :raises ValueError: ``item`` is ``None``, Puppet's ``default``.
        """
        from .parser import as_type

        if item is None:
            raise ValueError("Sensitive[] takes a type, not default")
        return SensitiveType(as_type(item))

    def unwrap(self) -> _ty.Any:
        """The wrapped value, unredacted.

        :returns: the wrapped value.
        """
        return self._value

    def __repr__(self) -> str:
        return "Sensitive [value redacted]"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Sensitive):
            return NotImplemented
        return _eql_key(self._value) == _eql_key(other._value)

    def __hash__(self) -> int:
        return hash(_eql_key(self._value))
