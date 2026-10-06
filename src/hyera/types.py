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
:mod:`hyera._types.types` are); that question now means "is this value of
that Puppet type" instead. Internal dispatch still uses the private classes
directly, unaffected by anything in this module.
"""

import re as _re
import typing as _ty

from ._types import types as _priv
from ._types.parser import parse_type as _parse_type

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
    "TypeSpec",
]

#: Anywhere a Puppet type is taken (``value_type`` on ``lookup``/``dig``/
#: ``get``/``explain``/``__call__``, and a nested type argument in a
#: subscript): a type object (what subscripting one of this module's
#: classes returns, or :func:`hyera._types.parser.parse_type` itself), a
#: bare class from this module (``Integer``, not ``Integer[1, 2]``), or a
#: Puppet type-expression string (``"Integer[1, 2]"``) -- see
#: :func:`hyera._types.parser.as_type`, which normalizes all three.
TypeSpec = _ty.Union[str, type, _priv.Any]


# --------------------------------------------------------------- rendering
#
# `__getitem__` renders its arguments into the Puppet source text for the
# equivalent type expression and hands it to `parse_type` -- the same
# constructors, grammar and error text `parse_type` itself uses, so
# `Integer[1, 2] == parse_type("Integer[1, 2]")` by construction. The
# `str`/`bool`/`int`/`float`/`None` cases mirror `_types.types`'s own
# `_puppet_quote`/`_num_str`/`_literal_str`; kept local to avoid reaching
# into that module's private renderers for the one extra case (dispatching
# on a nested type argument) they do not need to handle.


def _lit_str(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _lit_bool(b: bool) -> str:
    return "true" if b else "false"


def _lit_num(value: _ty.Any) -> str:
    """A value-position literal: ``None`` -> Puppet's ``default``, else a
    bool/int/float/str literal rendered the way Puppet's own type-expression
    grammar spells it."""
    if value is None:
        return "default"
    if isinstance(value, bool):
        return _lit_bool(value)
    if isinstance(value, str):
        return _lit_str(value)
    if isinstance(value, float):
        return _priv._num_str(value)
    if isinstance(value, int):
        return str(value)
    raise TypeError("unexpected literal argument: {!r}".format(value))


def _is_type_like(value: _ty.Any) -> bool:
    return isinstance(value, (str, _priv.Any, _TypeMeta))


def _type_text(value: _ty.Any) -> str:
    """A nested *type-position* argument (``Array``'s element type,
    ``Hash``'s key/value types, ``Tuple``'s elements, ``Variant``'s
    branches, ``Sensitive``'s contained type): a type object or a
    ``hyera.types`` class renders as its own Puppet text; a ``str`` is
    spliced in verbatim as type-expression source (``Array["Integer"]`` ==
    ``Array[Integer]``), never quoted -- unlike a *value*-position string
    (:func:`_lit_num`), which always is."""
    if isinstance(value, str):
        return value
    if isinstance(value, (_priv.Any, _TypeMeta)):
        return str(value)
    raise TypeError(
        "expected a type, a hyera.types class, or a type-expression str, "
        "not {}".format(type(value).__name__)
    )


def _regex_text(value: _ty.Any) -> str:
    if isinstance(value, _re.Pattern):
        source = value.pattern
    elif isinstance(value, str):
        source = value
    else:
        raise TypeError(
            "expected a str or re.Pattern, not {}".format(type(value).__name__)
        )
    # A literal "/" has no escape that round-trips through `parse_type`
    # (its own lexer stores a regex's raw, unescaped source) -- not
    # expressible here, same as it is not expressible as `value_type` text
    # without going through `parse_type` directly.
    return "/{}/".format(source)


def _args_text_optional_like(args: tuple) -> str:
    """``NotUndef``/``Optional``: each argument is a literal (a ``str``, as
    Puppet's own grammar treats a bareword/quoted string here) or a nested
    type -- never both at once in real Puppet, but every argument is
    rendered so a wrong count still reaches :func:`parse_type`'s own arity
    error."""
    parts = []
    for a in args:
        parts.append(_lit_str(a) if isinstance(a, str) else _type_text(a))
    return ", ".join(parts)


def _args_text_literal(args: tuple) -> str:
    return ", ".join(_lit_num(a) for a in args)


def _args_text_types(args: tuple) -> str:
    return ", ".join(_type_text(a) for a in args)


def _args_text_regex(args: tuple) -> str:
    return ", ".join(_regex_text(a) for a in args)


def _args_text_array(args: tuple) -> str:
    if args and _is_type_like(args[0]):
        parts = [_type_text(args[0])] + [_lit_num(a) for a in args[1:]]
    else:
        parts = [_lit_num(a) for a in args]
    return ", ".join(parts)


def _args_text_hash(args: tuple) -> str:
    parts = [_type_text(a) if i < 2 else _lit_num(a) for i, a in enumerate(args)]
    return ", ".join(parts)


def _args_text_tuple(args: tuple) -> str:
    items = list(args)
    sizes: list = []
    while items and len(sizes) < 2 and type(items[-1]) is int:
        sizes.insert(0, items.pop())
    parts = [_type_text(a) for a in items] + [_lit_num(a) for a in sizes]
    return ", ".join(parts)


def _struct_key_text(key: _ty.Any) -> str:
    if isinstance(key, str):
        return _lit_str(key)
    if isinstance(key, _priv.Optional) and isinstance(key.contained, str):
        return "Optional[{}]".format(_lit_str(key.contained))
    if isinstance(key, _priv.NotUndef) and isinstance(key.contained, str):
        return "NotUndef[{}]".format(_lit_str(key.contained))
    raise TypeError(
        "a Struct key must be a str, Optional[str] or NotUndef[str], "
        "not {!r}".format(key)
    )


def _args_text_struct(args: tuple) -> str:
    if len(args) == 1 and isinstance(args[0], dict):
        pairs = [
            "{} => {}".format(_struct_key_text(k), _type_text(v))
            for k, v in args[0].items()
        ]
        return "{{{}}}".format(", ".join(pairs))
    # Not a single dict: let `parse_type` raise its own "not a valid type
    # specification" error for the malformed expression.
    return ", ".join(_type_text(a) if _is_type_like(a) else _lit_num(a) for a in args)


def _args_text_mixed(args: tuple) -> str:
    """Names with no parameter grammar of their own (``Any``, ``Undef``,
    ``Scalar``, ``Numeric``, the ``Data``/``RichData`` aliases, ...):
    renders whatever was given so :func:`parse_type` raises its own "not a
    parameterized type" error."""
    return ", ".join(_type_text(a) if _is_type_like(a) else _lit_num(a) for a in args)


#: Puppet name (lowercased) -> the renderer for that name's subscript
#: arguments, mirroring `hyera._types.parser._ACCESS_BUILDERS`'s own keys.
_ARG_TEXT_BUILDERS: _ty.Dict[str, _ty.Callable[[tuple], str]] = {
    "notundef": _args_text_optional_like,
    "optional": _args_text_optional_like,
    "integer": _args_text_literal,
    "float": _args_text_literal,
    "string": _args_text_literal,
    "boolean": _args_text_literal,
    "collection": _args_text_literal,
    "enum": _args_text_literal,
    "pattern": _args_text_regex,
    "regexp": _args_text_regex,
    "array": _args_text_array,
    "hash": _args_text_hash,
    "tuple": _args_text_tuple,
    "struct": _args_text_struct,
    "variant": _args_text_types,
}


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
        # Every class in this module is named exactly after the Puppet type
        # it represents, so the Python class name doubles as `_puppet_name`
        # (the one exception, `hyera.Sensitive`, does not use this
        # metaclass at all -- see the module docstring).
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)
        cls._puppet_name = name
        return cls

    def _default(cls) -> _priv.Any:
        """This class's own unparameterized type object."""
        return _parse_type(cls._puppet_name)

    def __getitem__(cls, item: _ty.Any) -> _priv.Any:
        """``ClassName[item]``: builds the parameterized type object --
        see the module docstring's "Subscripted" paragraph."""
        args = item if isinstance(item, tuple) else (item,)
        builder = _ARG_TEXT_BUILDERS.get(cls._puppet_name.lower(), _args_text_mixed)
        text = "{}[{}]".format(cls._puppet_name, builder(args))
        return _parse_type(text)

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
        sites narrow it with an ordinary annotation or ``cast``."""
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


#: The existing public value wrapper (:class:`hyera.Sensitive`) -- the same
#: object, not a copy -- re-exported here so every public Puppet type is
#: reachable from this module. It already answers ``isinstance`` and
#: ``Sensitive(x)`` correctly without a metaclass (it is a real class whose
#: real instances are wrapped values); ``Sensitive[T]`` (the *type*
#: ``Sensitive[T]``, as opposed to the value ``Sensitive(x)``) is defined
#: directly on it in :mod:`hyera._types.types`.
Sensitive = _priv.Sensitive
