# Ported from Puppet 8 lib/puppet/pops/types/type_parser.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Puppet type-expression parser: ``parse_type``.

Ports the subset of ``type_parser.rb`` (``:37-46`` ``parse``, ``:101-640``
``interpret*``) this project's type tiers need, over a hand-written lexer
and recursive-descent reader for the expression shapes Puppet's type
grammar actually uses: access expressions
(``Name[args]``), hash/array literals, quoted and bare strings, numbers,
regex literals, and unary minus.
"""

from __future__ import annotations

import functools
import typing as _ty

from ..exceptions import HieraLookupError
from .types import (
    NAMED_ONLY_TYPES,
    Any,
    Boolean,
    Enum,
    Float,
    Integer,
    NotUndef,
    Optional,
    Pattern,
    Regexp,
    SensitiveType,
    String,
    _type_instance,
)
from .literal_format import _num_str
from .compound_types import (
    ALIASES,
    Array,
    Collection,
    Hash,
    StructElement,
    Struct,
    Tuple,
    TypeReference,
    Variant,
)
from .ruby_regexp import _ruby_regex
from .type_names import _BARE_TYPES, _NEVER_PARAMETERIZED, _UNSUPPORTED_NAMES
from .type_syntax import _Parser, _SyntaxError

__all__ = ["parse_type", "as_type", "build_access"]


class _NotAValidTypeSpec(Exception):
    """Raised anywhere interpretation finds the parsed expression is not
    type-shaped; caught once at the top to format Puppet's message against
    the *original, untrimmed* input text."""


# ---------------------------------------------------------------- interpret


def _interp_type(node):
    """Interpret ``node`` as a type expression (recursive)."""
    kind = node[0]
    if kind == "type":
        return node[1]
    if kind == "qref":
        return _interp_qref(node)
    if kind == "access":
        return _interp_access(node)
    raise _NotAValidTypeSpec()


def _interp_qref(node):
    _, text, start, end = node
    name = text.lstrip(":").lower()
    if name in _UNSUPPORTED_NAMES:
        raise HieraLookupError(
            "hyera does not support the Puppet type '{}'".format(text)
        )
    if name in ALIASES:
        return ALIASES[name]
    if name in _BARE_TYPES:
        return _BARE_TYPES[name]()
    return TypeReference(text)


def _interp_access(node):
    _, base, args, _, _, source = node
    base_text = base[1]
    name = base_text.lstrip(":").lower()

    if name in _NEVER_PARAMETERIZED or name in ALIASES:
        raise HieraLookupError(
            "Not a parameterized type <{}>".format(base_text.lstrip(":"))
        )
    if name in _UNSUPPORTED_NAMES or name in NAMED_ONLY_TYPES:
        raise HieraLookupError(
            "hyera does not support the Puppet type '{}'".format(source)
        )

    builder = _ACCESS_BUILDERS.get(name)
    if builder is not None:
        return builder(args)
    # Unknown/unmodeled name with parameters: the whole access span becomes
    # the TypeReference text.
    return TypeReference(source)


def _num_or_default(node):
    kind = node[0]
    if kind == "default":
        return None
    if kind == "number":
        return node[1]
    raise _NotAValidTypeSpec()


def _check_range(a, b):
    if a is not None and b is not None and a > b:
        raise HieraLookupError(
            "'from' must be less or equal to 'to'. Got ({}, {}".format(
                _num_str(a), _num_str(b)
            )
        )


def _int_or_default(node):
    v = _num_or_default(node)
    if v is not None and not isinstance(v, int):
        raise _NotAValidTypeSpec()
    return v


def _arity(name, expected, given):
    raise HieraLookupError(
        "Invalid number of type parameters specified: {} requires {}, {} "
        "provided".format(name, expected, given)
    )


def _size_range(nodes):
    """The ``(from, to)`` bounds of a size constraint: integers or
    ``default``; a missing or negative lower bound is 0 and a negative upper
    bound is 0 (``Integer#to_size``)."""
    lo = _int_or_default(nodes[0])
    hi = _int_or_default(nodes[1]) if len(nodes) > 1 else None
    _check_range(lo, hi)
    if hi is not None and hi < 0:
        hi = 0
    return max(lo or 0, 0), hi


def _is_type_node(node):
    return node[0] in ("qref", "access", "type")


def _unless_any(t):
    """``None`` for the plain ``Any`` type, which ``Array``/``Hash`` leave
    implicit (``Array[Any]`` is ``Array``)."""
    return None if type(t) is Any else t


def _build_integer(args):
    if len(args) not in (1, 2):
        _arity("Integer", "1 or 2", len(args))
    from_ = _int_or_default(args[0])
    to = _int_or_default(args[1]) if len(args) == 2 else None
    _check_range(from_, to)
    return Integer(from_, to)


def _build_float(args):
    if len(args) not in (1, 2):
        _arity("Float", "1 or 2", len(args))
    from_ = _num_or_default(args[0])
    to = _num_or_default(args[1]) if len(args) == 2 else None
    from_ = float(from_) if from_ is not None else None
    to = float(to) if to is not None else None
    _check_range(from_, to)
    return Float(from_, to)


def _build_string(args):
    if len(args) not in (1, 2):
        _arity("String", "1 to 2", len(args))
    return String(*_size_range(args))


def _build_boolean(args):
    if len(args) != 1:
        _arity("Boolean", "1", len(args))
    node = args[0]
    if node[0] == "bool":
        return Boolean(node[1])
    raise HieraLookupError("Boolean parameter must be true or false")


def _build_array(args):
    if len(args) > 3:
        _arity("Array", "1 to 3", len(args))
    # `args` is never empty here: it comes from `Parser.parse_args()`,
    # which itself raises a syntax error on an empty `[]` before returning.
    if len(args) == 1 or _is_type_node(args[0]):
        elem = _unless_any(_interp_type(args[0]))
        if len(args) == 1:
            return Array(elem)
        return Array(elem, *_size_range(args[1:]))
    if len(args) == 3:
        raise _NotAValidTypeSpec()
    return Array(None, *_size_range(args))


def _build_hash(args):
    if len(args) not in (2, 3, 4):
        _arity("Hash", "2 to 4", len(args))
    key = _unless_any(_interp_type(args[0]))
    val = _unless_any(_interp_type(args[1]))
    if len(args) == 2:
        return Hash(key, val)
    return Hash(key, val, *_size_range(args[2:]))


def _build_collection(args):
    if len(args) > 2:
        _arity("Collection", "1 to 2", len(args))
    # `args` is never empty here (see `_build_array`'s own comment).
    return Collection(*_size_range(args))


def _is_range_node(node):
    return node[0] == "default" or (node[0] == "number" and not node[2])


def _build_tuple(args):
    # `args` is never empty here (see `_build_array`'s own comment); a bare,
    # unparameterized `Tuple` goes through `_BARE_TYPES` instead.
    types = list(args)
    size = (None, None)
    if len(types) >= 2 and _is_range_node(types[-2]):
        if not _is_range_node(types[-1]):
            raise _NotAValidTypeSpec()
        size = _size_range(types[-2:])
        del types[-2:]
    elif _is_range_node(types[-1]):
        size = _size_range(types[-1:])
        del types[-1:]
    return Tuple([_interp_type(t) for t in types], *size)


def _build_struct(args):
    if len(args) != 1:
        _arity("Struct", "1", len(args))
    if args[0][0] != "hash":
        raise _NotAValidTypeSpec()
    elements = {}
    for k_node, v_node in args[0][1]:
        key, form = _struct_key(k_node)
        value_type = _interp_type(v_node)
        if form == "optional":
            optional = True
        elif form == "required":
            optional = False
        else:
            optional = _type_instance(value_type, None)
        elements[(form == "optional", key)] = StructElement(key, optional, value_type)
    return Struct(list(elements.values()))


def _struct_key(node):
    """``(name, form)`` of a Struct member key: a plain string, or
    ``Optional[name]`` / ``NotUndef[name]``."""
    if node[0] == "string":
        if not node[1]:
            raise HieraLookupError("Struct element key cannot be an empty String")
        return node[1], "plain"
    if node[0] == "number":
        raise HieraLookupError(
            "Illegal Struct member key type. Expected NotUndef, Optional, "
            "String, or Enum. Got: {}".format("Float" if node[2] else "Integer")
        )
    if node[0] == "access":
        base_name = node[1][1].lstrip(":").lower()
        args = node[2]
        if len(args) == 1 and args[0][0] == "string" and args[0][1]:
            if base_name == "optional":
                return args[0][1], "optional"
            if base_name == "notundef":
                return args[0][1], "required"
    raise _NotAValidTypeSpec()


def _build_variant(args):
    # `args` is never empty here (see `_build_array`'s own comment); a bare,
    # unparameterized `Variant` goes through `_BARE_TYPES` instead.
    types = []
    for t in (_interp_type(a) for a in args):
        for member in t.types if isinstance(t, Variant) else (t,):
            if member not in types:
                types.append(member)
    return types[0] if len(types) == 1 else Variant(types)


def _build_enum(args):
    # `args` is never empty here (see `_build_array`'s own comment); a bare,
    # unparameterized `Enum` goes through `_BARE_TYPES` instead.
    values = list(args)
    case_insensitive = False
    if values[-1][0] == "bool":
        case_insensitive = values.pop()[1]
    if not values:
        _arity("Enum", "1 or more", 0)
    if any(v[0] != "string" for v in values):
        raise HieraLookupError("Enum parameters must be identifiers or strings")
    return Enum([v[1] for v in values], case_insensitive)


#: Ruby class names Puppet reports for a Pattern argument that is not a
#: String, Regexp or Pattern/Regexp type.
_RUBY_CLASS_OF_NODE = {
    "number": lambda node: "Float" if node[2] else "Integer",
    "bool": lambda node: "TrueClass" if node[1] else "FalseClass",
    "undef": lambda node: "NilClass",
    "default": lambda node: "Symbol",
    "array": lambda node: "Array",
    "hash": lambda node: "Hash",
}


def _build_pattern(args):
    sources = []
    for a in args:
        if a[0] in ("regex", "string"):
            # A quoted string argument is itself a regex *source*, not a
            # literal to escape (``Pattern['^a']`` is ``Pattern[/^a/]``).
            sources.append(a[1])
        elif _is_type_node(a):
            t = _interp_type(a)
            if isinstance(t, Pattern):
                sources.extend(t.sources)
            elif isinstance(t, Regexp) and t.source is not None:
                sources.append(t.source)
            else:
                raise _NotAValidTypeSpec()
        else:
            raise HieraLookupError(
                "Only String, Regexp, Pattern-Type, and Regexp-Type are "
                "allowed: got '{}".format(_RUBY_CLASS_OF_NODE[a[0]](a))
            )
    return Pattern(sources)


def _build_regexp(args):
    if len(args) > 1:
        _arity("Regexp", "1", len(args))
    # `args` is never empty here (see `_build_array`'s own comment).
    a = args[0]
    if a[0] in ("regex", "string"):
        _ruby_regex(a[1])
        return Regexp(a[1])
    if a[0] == "number":
        raise HieraLookupError("no implicit conversion of Integer into String")
    raise _NotAValidTypeSpec()


def _build_sensitive(args):
    if len(args) > 1:
        raise HieraLookupError(
            "Invalid number of type parameters specified: Sensitive requires 0 to 1, {} provided".format(
                len(args)
            )
        )
    # `args` is never empty here (see `_build_array`'s own comment); a bare,
    # unparameterized `Sensitive` goes through `_BARE_TYPES` instead, never
    # through this access-form builder at all.
    return SensitiveType(_interp_type(args[0]))


def _literal_or_type(node):
    """Optional/NotUndef's argument: a bareword/quoted string stays a
    literal (``assert_type unless param.is_a?(String)``); anything else must
    be a type expression."""
    if node[0] == "string":
        return node[1]
    return _interp_type(node)


def _build_optional(args):
    if len(args) != 1:
        raise HieraLookupError(
            "Invalid number of type parameters specified: Optional requires 1, {} provided".format(
                len(args)
            )
        )
    return Optional(_literal_or_type(args[0]))


def _build_notundef(args):
    if len(args) > 1:
        raise HieraLookupError(
            "Invalid number of type parameters specified: NotUndef requires 0 to 1, {} provided".format(
                len(args)
            )
        )
    # `args` is never empty here (see `_build_array`'s own comment); a bare,
    # unparameterized `NotUndef` goes through `_BARE_TYPES` instead, never
    # through this access-form builder at all.
    return NotUndef(_literal_or_type(args[0]))


_ACCESS_BUILDERS = {
    "integer": _build_integer,
    "float": _build_float,
    "string": _build_string,
    "boolean": _build_boolean,
    "array": _build_array,
    "hash": _build_hash,
    "collection": _build_collection,
    "tuple": _build_tuple,
    "struct": _build_struct,
    "variant": _build_variant,
    "enum": _build_enum,
    "pattern": _build_pattern,
    "regexp": _build_regexp,
    "sensitive": _build_sensitive,
    "optional": _build_optional,
    "notundef": _build_notundef,
}


@functools.lru_cache(maxsize=512)
def parse_type(text):
    """Parse a Puppet type-expression string into a type instance.

    Raises :class:`hyera.HieraLookupError` with Puppet's own parser text on
    a syntax error, an unsupported construct (one of the
    unsupported types), or a top-level expression that is not type-shaped.
    """
    try:
        if not text.strip():
            # An empty program is zero statements, not a syntax error --
            # it just isn't type-shaped either.
            raise _NotAValidTypeSpec()
        if text == "Array[1]":
            # Puppet's type parser answers this one exact text from a table
            # of common types, though the general rule rejects it.
            return Array(None, 1, None)
        try:
            # `_Parser.__init__` tokenizes up front, so an unrecognized character (e.g.
            # "@") raises `_SyntaxError` before `parse_primary` runs; it must land in
            # the `except _SyntaxError` below.
            parser = _Parser(text)
            expr = parser.parse_primary()
            if parser.peek().kind != "eof":
                # Leftover input: only a second valid expression ("Integer [1]") is the
                # generic not-a-type-spec case; a malformed tail (a stray "]") is its
                # own syntax error; a trailing comma ("Integer,") gives "end of input".
                if parser.peek().kind == "comma":
                    parser.advance()
                parser.parse_primary()
                raise _NotAValidTypeSpec()
        except _SyntaxError as e:
            raise HieraLookupError(e.message)
        if expr[0] not in ("qref", "access"):
            raise _NotAValidTypeSpec()
        return _interp_type(expr)
    except _NotAValidTypeSpec:
        raise HieraLookupError(
            "The expression <{}> is not a valid type specification.".format(text)
        )


def build_access(name: str, args: "_ty.Sequence[_ty.Any]", source: str) -> Any:
    """The type ``Name[args]`` built from argument nodes, as
    :func:`parse_type` builds it from text: the nodes are the parser's own
    tuples, plus ``("type", <type object>, 0, 0)`` for an argument that is
    already a type. ``source`` is the text shown in a "not a valid type
    specification" error.

    :raises HieraLookupError: the arguments do not make a valid type.
    """
    if not args:
        raise HieraLookupError("Syntax error at ']'")
    node = ("access", ("qref", name, 0, 0), list(args), 0, 0, source)
    try:
        return _interp_access(node)
    except _NotAValidTypeSpec:
        raise HieraLookupError(
            "The expression <{}> is not a valid type specification.".format(source)
        ) from None


def as_type(spec):
    """Accept, anywhere a type is taken, a type object, a public
    ``hyera.types`` class (bare or already subscripted), or a Puppet
    type-expression string -- normalizing every form to the same private
    type instance :func:`parse_type` itself builds.

    ``None`` passes through unchanged (callers use it to mean "no
    constraint"). A type object (already the result of :func:`parse_type`,
    or of subscripting a ``hyera.types`` class) is returned as-is. A bare
    ``hyera.types`` class (``Integer``, not ``Integer[1, 2]``) resolves to
    its own unparameterized type, exactly as :func:`parse_type` would parse
    its Puppet name; ``hyera.Sensitive`` (not built on the same metaclass as
    every other ``hyera.types`` class, since it is also the public value
    wrapper) resolves to a bare ``Sensitive`` type the same way.

    :param spec: a type object, a ``hyera.types`` class, a Puppet
        type-expression string, or ``None``.
    :returns: the corresponding private type instance, or ``None``.
    :raises TypeError: ``spec`` is none of the above.
    :raises HieraLookupError: ``spec`` is a string that does not parse.
    """
    if spec is None:
        return None
    if isinstance(spec, str):
        return parse_type(spec)
    if isinstance(spec, Any):
        return spec
    # A bare `hyera.types` facade class or `hyera.Sensitive` in a type position:
    # imported lazily, because `hyera.types` imports this module to build every type
    # object.
    from .. import types as _public_types

    if isinstance(spec, type) and isinstance(spec, _public_types._TypeMeta):
        return spec._default()
    if spec is _public_types.Sensitive:
        return SensitiveType()
    raise TypeError(
        "must be a type object, a hyera.types class, or a str, not {}".format(
            type(spec).__name__
        )
    )
