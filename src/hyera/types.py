"""Public Puppet type objects: ``Integer[1, 2]``, ``Optional[String]``, ...

One class per Puppet type a user writes in a type expression. Three things
one of these classes can mean, by how it is used:

- **Bare** (``Integer``): the unparameterized type.
- **Subscripted** (``Integer[1, 10]``, ``Optional[String]``): a parameterized
  type, built exactly as :func:`hyera._types.parser.parse_type` would parse
  the equivalent Puppet text.
- **Called** (``Integer("42")``): Puppet's ``new()``, returning a plain
  value (``int`` for ``Integer``, ``str`` for ``String``, ...), never an
  instance of the class itself.

Every one of the first two forms answers ``isinstance`` the Puppet way:
``isinstance(5, Integer)`` and ``isinstance(5, Integer[1, 10])`` both work,
without ``Integer`` subclassing ``int`` (``bool``/``NoneType`` cannot be
subclassed, and a builtin subclass breaks ``yaml.safe_dump``). Every class
here is not re-exported from top-level ``hyera`` (``hyera.types.Integer``,
not ``hyera.Integer``) -- the one exception is :class:`hyera.Sensitive`,
already public there as the value wrapper; it gains ``Sensitive[T]`` here
(a type) alongside its existing ``Sensitive(x)`` (a value), both the same
object as ``hyera.Sensitive``.

Internal code must never rely on ``isinstance(type_obj, <one of these
classes>)`` to dispatch on a type object's kind -- these classes are never
the type of a type object (the private classes in
:mod:`hyera._types.types` are); that question means "is this value of
that Puppet type" here. Internal dispatch still uses the private classes
directly, unaffected by anything in this module.
"""

from __future__ import annotations

import re as _re
import typing as _ty

from ._types import types as _priv
from ._types.parser import as_type as _as_type
from ._types.parser import build_access as _build_access
from ._types.parser import parse_type as _parse_type
from .exceptions import HieraLookupError as _HieraLookupError

__all__ = [
    "Any",
    "Undef",
    "NotUndef",
    "Optional",
    "Scalar",
    "ScalarData",
    "Numeric",
    "Integer",
    "Float",
    "String",
    "Boolean",
    "Regexp",
    "Pattern",
    "Enum",
    "Collection",
    "Array",
    "Hash",
    "Tuple",
    "Struct",
    "Variant",
    "Sensitive",
    "Data",
    "RichData",
    "TypeLike",
]

#: A Puppet type wherever one is taken (``value_type``, a nested subscript argument): a
#: type object, a bare class from this module (``Integer``, not ``Integer[1, 2]``) or a
#: type-expression string; :func:`hyera._types.parser.as_type` normalizes all three.
TypeLike = _ty.Union[str, type, _priv.Any]


# ------------------------------------------------------------- arguments
# `__getitem__` maps its arguments (a type object stands for itself) onto `parse_type`'s
# builders: `Integer[1, 2] == parse_type("Integer[1, 2]")`, same arity and errors.


def _value_node(value: _ty.Any) -> tuple:
    """A value-position argument: ``None`` is Puppet's ``default``."""
    if value is None:
        return ("default", None, 0, 0)
    if isinstance(value, bool):
        return ("bool", value, 0, 0)
    if isinstance(value, str):
        return ("string", value, 0, 0)
    if isinstance(value, (int, float)):
        return ("number", value, isinstance(value, float), 0, 0)
    if isinstance(value, _re.Pattern):
        return ("regex", value.pattern, 0, 0)
    raise TypeError("unexpected literal argument: {!r}".format(value))


def _is_type_like(value: _ty.Any) -> bool:
    return isinstance(value, (str, _priv.Any, _TypeMeta))


def _type_node(value: _ty.Any) -> tuple:
    """A type-position argument: a type object, a ``hyera.types`` class or a
    type-expression ``str`` (``Array["Integer"]`` is ``Array[Integer]``)."""
    try:
        return ("type", _as_type(value), 0, 0)
    except TypeError:
        raise TypeError(
            "expected a type, a hyera.types class, or a type-expression str, "
            "not {}".format(type(value).__name__)
        ) from None


def _literal_or_type_node(value: _ty.Any) -> tuple:
    """``Optional``/``NotUndef``'s argument: a ``str`` stays a literal."""
    return _value_node(value) if isinstance(value, str) else _type_node(value)


def _nodes_literal(args: tuple) -> list:
    return [_value_node(a) for a in args]


def _nodes_types(args: tuple) -> list:
    return [_type_node(a) for a in args]


def _nodes_regex(args: tuple) -> list:
    nodes = []
    for a in args:
        if isinstance(a, (str, _re.Pattern)):
            nodes.append(_value_node(a))
        elif _is_type_like(a):
            nodes.append(_type_node(a))
        else:
            raise TypeError(
                "expected a str or re.Pattern, not {}".format(type(a).__name__)
            )
    return nodes


def _nodes_optional_like(args: tuple) -> list:
    return [_literal_or_type_node(a) for a in args]


def _nodes_array(args: tuple) -> list:
    if args and _is_type_like(args[0]):
        return [_type_node(args[0])] + _nodes_literal(args[1:])
    return _nodes_literal(args)


def _nodes_hash(args: tuple) -> list:
    return [_type_node(a) if i < 2 else _value_node(a) for i, a in enumerate(args)]


def _nodes_tuple(args: tuple) -> list:
    items = list(args)
    sizes: list = []
    while items and len(sizes) < 2 and type(items[-1]) is int:
        sizes.insert(0, items.pop())
    return _nodes_types(tuple(items)) + _nodes_literal(tuple(sizes))


def _struct_key_node(key: _ty.Any) -> tuple:
    if isinstance(key, str):
        return _value_node(key)
    for kind, name in ((_priv.Optional, "Optional"), (_priv.NotUndef, "NotUndef")):
        if isinstance(key, kind) and isinstance(key.contained, str):
            return (
                "access",
                ("qref", name, 0, 0),
                [_value_node(key.contained)],
                0,
                0,
                "{}[{!r}]".format(name, key.contained),
            )
    raise TypeError(
        "a Struct key must be a str, Optional[str] or NotUndef[str], "
        "not {!r}".format(key)
    )


def _nodes_struct(args: tuple) -> list:
    if len(args) == 1 and isinstance(args[0], dict):
        pairs = [(_struct_key_node(k), _type_node(v)) for k, v in args[0].items()]
        return [("hash", pairs, 0, 0)]
    # Not a single dict: the builder reports "not a valid type specification".
    return [_type_node(a) if _is_type_like(a) else _value_node(a) for a in args]


def _nodes_mixed(args: tuple) -> list:
    """Names with no parameter grammar of their own (``Any``, ``Undef``,
    ``Scalar``, ``Numeric``, the ``Data``/``RichData`` aliases): the parser
    raises its own "not a parameterized type" error."""
    return [_type_node(a) if _is_type_like(a) else _value_node(a) for a in args]


#: Puppet name (lowercased) -> the converter for that name's subscript
#: arguments, mirroring the parser's own builders.
_ARG_NODES: _ty.Dict[str, _ty.Callable[[tuple], list]] = {
    "notundef": _nodes_optional_like,
    "optional": _nodes_optional_like,
    "integer": _nodes_literal,
    "float": _nodes_literal,
    "string": _nodes_literal,
    "boolean": _nodes_literal,
    "collection": _nodes_literal,
    "enum": _nodes_literal,
    "pattern": _nodes_regex,
    "regexp": _nodes_regex,
    "array": _nodes_array,
    "hash": _nodes_hash,
    "tuple": _nodes_tuple,
    "struct": _nodes_struct,
    "variant": _nodes_types,
    "sensitive": _nodes_types,
}


def _args_source(name: str, args: tuple) -> str:
    """The subscript as Puppet text, for an error message."""
    parts = [str(a) if isinstance(a, (_priv.Any, _TypeMeta)) else repr(a) for a in args]
    return "{}[{}]".format(name, ", ".join(parts))


class _TypeMeta(type):
    """Metaclass behind every class in this module (``Sensitive`` is the
    one public type name that opts out, since :class:`hyera.Sensitive` is
    already a concrete value-wrapper class): subscripting builds a type
    object, the bare class answers ``isinstance`` as its own unparameterized
    type, and calling is Puppet's ``new()``.
    """

    _puppet_name: str

    def __new__(
        mcs,
        name: str,
        bases: _ty.Tuple[type, ...],
        namespace: _ty.Dict[str, _ty.Any],
        **kwargs: _ty.Any,
    ) -> "_TypeMeta":
        """Records the new class's own Puppet name (see ``_puppet_name``
        below) in the same step ``type.__new__`` builds it."""
        # Every class in this module is named after the Puppet type it represents, so
        # the class name doubles as `_puppet_name` (the exception, `hyera.Sensitive`,
        # does not use this metaclass: see the module docstring).
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
        cls._puppet_name = name
        return cls

    def _default(cls) -> _priv.Any:
        """This class's own unparameterized type object."""
        return _parse_type(cls._puppet_name)

    def __getitem__(cls, item: _ty.Any) -> _priv.Any:
        """``ClassName[item]``: builds the parameterized type object --
        see the module docstring's "Subscripted" paragraph.

        :param item: the subscript: one argument or a tuple of them.
        :returns: the type object.
        :raises TypeError: an argument is not a type, a literal or a pattern.
        :raises ValueError: the arguments do not make a valid type (a range
            whose bounds are reversed, the wrong number of parameters, a
            malformed pattern).
        """
        args = item if isinstance(item, tuple) else (item,)
        name = cls._puppet_name
        try:
            nodes = _ARG_NODES.get(name.lower(), _nodes_mixed)(args)
            return _build_access(name, nodes, _args_source(name, args))
        except _HieraLookupError as e:
            raise ValueError(str(e)) from None

    def __instancecheck__(cls, value: _ty.Any) -> bool:
        """``isinstance(value, ClassName)``: Puppet's own instance check
        for this class's unparameterized type."""
        return cls._default().instance(value)

    def __call__(cls, *args: _ty.Any, **kwargs: _ty.Any) -> _ty.Any:
        """Puppet's ``new()``: ``Integer("42") == 42``,
        ``type(Integer("42")) is int`` -- the same as calling this class's
        own default type object directly (``Integer._default()("42")``,
        also what a subscripted type's own call does, e.g. ``Integer[1,
        10]("42")``). Not expressible as a per-class return type from one
        shared metaclass method -- pyright sees ``Any`` here; typed call
        sites narrow it with an ordinary annotation or ``cast``.

        :param args: the value to convert, then any further ``new()`` arguments.
        :returns: the converted value.
        :raises TypeError: a keyword argument was given, or no value.
        :raises ValueError: the arguments cannot be converted to this type.
        """
        if kwargs:
            raise TypeError("{}() takes no keyword arguments".format(cls._puppet_name))
        if not args:
            raise TypeError(
                "{}() missing required argument: value".format(cls._puppet_name)
            )
        return cls._default()(*args)

    def __str__(cls) -> str:
        """Puppet's own rendering of this class's unparameterized type
        (``str(Integer) == "Integer"``)."""
        return str(cls._default())

    def __repr__(cls) -> str:
        """The ordinary Python class ``repr``, unaffected by Puppet's own
        type formatting (see ``__str__`` for that)."""
        return "<class 'hyera.types.{}'>".format(cls._puppet_name)


class Any(metaclass=_TypeMeta):
    """Puppet's ``Any``: every value is an instance. Not parameterizable."""


class Undef(metaclass=_TypeMeta):
    """Puppet's ``Undef``: only ``None``. Not parameterizable."""


class NotUndef(metaclass=_TypeMeta):
    """Puppet's ``NotUndef``: any value but ``None``, optionally of a
    contained type (``NotUndef[Integer]``) or matching a literal string."""


class Optional(metaclass=_TypeMeta):
    """Puppet's ``Optional``: ``None``, or an instance of the contained
    type (``Optional[Integer]``) or matching a literal string."""


class Scalar(metaclass=_TypeMeta):
    """Puppet's ``Scalar``: a bool, int, float, str or compiled regexp.
    Not parameterizable."""


class ScalarData(metaclass=_TypeMeta):
    """Puppet's ``ScalarData``: a bool/int/float/str, or an Array/Hash
    containing only those (and, for a Hash, str keys). Not parameterizable."""


class Numeric(metaclass=_TypeMeta):
    """Puppet's ``Numeric``: an int or a float, never a bool. Not
    parameterizable."""


class Integer(metaclass=_TypeMeta):
    """Puppet's ``Integer``, optionally range-bound (``Integer[1, 10]``,
    ``Integer[1]`` for "at least 1"); ``None`` as either bound is Puppet's
    ``default`` (unbounded)."""


class Float(metaclass=_TypeMeta):
    """Puppet's ``Float``, optionally range-bound (``Float[0.0, 1.0]``),
    exactly as :class:`Integer`."""


class String(metaclass=_TypeMeta):
    """Puppet's ``String``, optionally length-bound (``String[1, 10]``,
    ``String[1]`` for "at least 1 character")."""


class Boolean(metaclass=_TypeMeta):
    """Puppet's ``Boolean``, optionally fixed to one value
    (``Boolean[True]``)."""


class Regexp(metaclass=_TypeMeta):
    """Puppet's ``Regexp``, optionally matching one exact pattern source
    (``Regexp["^a.*"]`` or ``Regexp[re.compile("^a.*")]``)."""


class Pattern(metaclass=_TypeMeta):
    """Puppet's ``Pattern``: a ``str`` instance matching one of one or more
    regular expressions (``Pattern["^a", "^b"]``, each a ``str`` regex
    source or a compiled ``re.Pattern``)."""


class Enum(metaclass=_TypeMeta):
    """Puppet's ``Enum``: a ``str`` instance equal to one of the given
    values (``Enum["a", "b"]``)."""


class Collection(metaclass=_TypeMeta):
    """Puppet's ``Collection``: an Array or Hash, optionally size-bound
    (``Collection[1, 10]``)."""


class Array(metaclass=_TypeMeta):
    """Puppet's ``Array``, optionally of an element type
    (``Array[Integer]``) and/or size-bound (``Array[Integer, 1, 10]``,
    ``Array[1, 10]`` with no element type)."""


class Hash(metaclass=_TypeMeta):
    """Puppet's ``Hash``, optionally of key/value types
    (``Hash[String, Integer]``) and/or size-bound
    (``Hash[String, Integer, 1, 10]``)."""


class Tuple(metaclass=_TypeMeta):
    """Puppet's ``Tuple``: a fixed-shape array of per-position types
    (``Tuple[Integer, String]``), optionally size-bound as its own trailing
    int argument/pair (``Tuple[Integer, 1, 3]`` repeats the last type)."""


class Struct(metaclass=_TypeMeta):
    """Puppet's ``Struct``: a ``dict`` matching a fixed key/value-type
    shape, given as one mapping argument (``Struct[{"a": Integer}]``); a key
    may be a plain ``str`` (required) or ``Optional["k"]``/``NotUndef["k"]``
    (built from this same module, as a dict key) for an optional one."""


class Variant(metaclass=_TypeMeta):
    """Puppet's ``Variant``: a value matching any of the given types
    (``Variant[Integer, String]``)."""


class Data(metaclass=_TypeMeta):
    """Puppet's ``Data`` alias: ``Variant[ScalarData, Undef, Hash[String,
    Data], Array[Data]]``. Not parameterizable."""


class RichData(metaclass=_TypeMeta):
    """Puppet's ``RichData`` alias: :class:`Data` plus ``Sensitive``,
    ``Binary``, ``SemVerRange``, ``Type``, ``TypeSet``, ``URI`` and
    ``Object`` values. Not parameterizable."""


#: The public value wrapper (:class:`hyera.Sensitive`), re-exported so every public
#: Puppet type is reachable here. It answers ``isinstance`` and ``Sensitive(x)`` with no
#: metaclass; the type ``Sensitive[T]`` lives in :mod:`hyera._types.types`.
Sensitive = _priv.Sensitive
