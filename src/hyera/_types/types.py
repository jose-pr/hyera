# Ported from Puppet 8 lib/puppet/pops/types/types.rb, type_calculator.rb,
# type_formatter.rb, p_sensitive_type.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Puppet type system: the type model, Sensitive, convert_to.

Ports Puppet's ``pops/types`` (``types.rb``, ``type_calculator.rb``,
``type_formatter.rb``, ``p_sensitive_type.rb``). This module is a leaf
(imports only :mod:`hyera.exceptions`); :mod:`hyera._types.parser` builds
type instances from a Puppet type-expression string via ``parse_type``.

Each class here is Puppet's own type-expression name (``Any``, ``Integer``,
``Optional``, ...) rather than Ruby's ``P<Name>Type`` (``PAnyType``,
``PIntegerType``, ``POptionalType``, ...); the one exception is
:class:`SensitiveType`, kept out of Ruby's bare ``Sensitive`` name because
:class:`hyera.Sensitive` (this module's own value wrapper) already has it.
The classes stay private.
"""

import re
import typing as _ty


def _ruby_regex(source):
    """Compile a Ruby regex source into a Python ``re.Pattern``.

    Ruby's ``(?<name>``/``\\z`` differ from Python; ``\\Z`` in Ruby matches
    "end of string, or before a trailing newline" (Python's ``\\Z`` is a hard
    end-of-string anchor), and ``\\h``/``\\H`` (hex digit / non-hex digit)
    have no Python equivalent.
    """
    # A named-group open (Ruby ``(?<name>``) becomes Python's ``(?P<name>``,
    # but a lookbehind (Ruby/Python both spell it ``(?<=`` / ``(?<!``) must
    # be left alone -- rewriting it too turns a valid lookbehind into an
    # unterminated group name.
    pattern = re.sub(r"\(\?<(?![=!])", "(?P<", source)
    pattern = re.sub(r"(?<!\\)\\z", "\\\\Z", pattern)
    pattern = re.sub(r"(?<!\\)\\Z", "(?=\\\\n?\\\\Z)", pattern)
    pattern = pattern.replace("\\h", "[0-9a-fA-F]").replace("\\H", "[^0-9a-fA-F]")
    return re.compile(pattern, re.MULTILINE)


def _puppet_quote(value):
    """Puppet's single-quoted string literal rendering (``puppet_quote``)."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _num_str(value):
    """Render a range bound: ``None`` -> ``"default"``, else Puppet's number
    literal form (a float always shows at least one decimal digit)."""
    if value is None:
        return "default"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return repr(value)
        if value == int(value) and abs(value) < 1e16:
            return "%.1f" % value
        text = repr(value)
        if "e" in text and "." not in text.split("e")[0]:
            mantissa, exp = text.split("e")
            text = mantissa + ".0e" + exp
        return text
    return str(value)


def _literal_str(value):
    """Puppet's literal rendering of a value used as a bareword type
    argument (Enum values, a struct key's literal form, etc.)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _puppet_quote(value)
    if isinstance(value, float):
        return _num_str(value)
    return str(value)


def _render_size_args(from_, to_):
    """The trailing size-constraint arguments for a rendered type, e.g.
    ``Array[String, 1]`` or ``Array[String, 1, 3]``. Empty when unconstrained."""
    if from_ is None and to_ is None:
        return []
    if to_ is None:
        return [_num_str(from_)]
    return [_num_str(from_), _num_str(to_)]


class Any:
    """Base of the ported Puppet type model (``types.rb``)."""

    __slots__ = ()

    #: Puppet class name, without the leading ``P``/trailing ``Type``.
    TYPE_NAME = "Any"

    def instance(self, value):
        return True

    def assignable(self, other):
        return isinstance(other, Any)

    def normalize(self):
        return self

    def generalize(self):
        return self

    @property
    def name(self):
        return self.TYPE_NAME

    @property
    def simple_name(self):
        return self.TYPE_NAME

    def alias_expanded_str(self):
        return str(self)

    def __str__(self):
        return self.name

    def __repr__(self):
        return "<{}>".format(str(self))

    def _key(self):
        return ()

    def __eq__(self, other):
        return type(self) is type(other) and self._key() == other._key()

    def __hash__(self):
        return hash((type(self), self._key()))

    def __instancecheck__(self, value):
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


class Undef(Any):
    TYPE_NAME = "Undef"

    def instance(self, value):
        return value is None

    def assignable(self, other):
        return isinstance(other, Undef)


class NotUndef(Any):
    TYPE_NAME = "NotUndef"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if value is None:
            return False
        if self.contained is None:
            return True
        return _type_instance(self.contained, value)

    def assignable(self, other):
        if isinstance(other, Undef):
            return False
        if self.contained is None:
            return True
        return _type_assignable(self.contained, other)

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("NotUndef", self.contained, show_literal=True)


class Optional(Any):
    TYPE_NAME = "Optional"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if value is None:
            return True
        if self.contained is None:
            return True
        return _type_instance(self.contained, value)

    def assignable(self, other):
        if isinstance(other, Undef):
            return True
        if self.contained is None:
            return True
        return _type_assignable(self.contained, other)

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("Optional", self.contained, show_literal=True)


class Scalar(Any):
    TYPE_NAME = "Scalar"

    def instance(self, value):
        return isinstance(value, (bool, int, float, str)) or isinstance(
            value, re.Pattern
        )


class ScalarData(Scalar):
    TYPE_NAME = "ScalarData"

    def instance(self, value):
        if isinstance(value, (bool, int, float, str)):
            return True
        if isinstance(value, list):
            return all(ScalarData.instance(self, v) for v in value)
        if isinstance(value, dict):
            return all(
                isinstance(k, str) and ScalarData.instance(self, v)
                for k, v in value.items()
            )
        return False


class Numeric(Any):
    TYPE_NAME = "Numeric"

    def instance(self, value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)


class Integer(Any):
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

    def assignable(self, other):
        if not isinstance(other, Integer):
            return False
        if self.from_ is not None and (other.from_ is None or other.from_ < self.from_):
            return False
        if self.to is not None and (other.to is None or other.to > self.to):
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
    TYPE_NAME = "Float"

    def __init__(self, from_=None, to=None):
        self.from_ = from_
        self.to = to

    DEFAULT = None  # set below

    def instance(self, value):
        if not isinstance(value, float):
            return False
        if self.from_ is not None and value < self.from_:
            return False
        if self.to is not None and value > self.to:
            return False
        return True

    def assignable(self, other):
        if not isinstance(other, Float):
            return False
        if self.from_ is not None and (other.from_ is None or other.from_ < self.from_):
            return False
        if self.to is not None and (other.to is None or other.to > self.to):
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
    TYPE_NAME = "String"

    def __init__(self, size_from=None, size_to=None, literal=None):
        self.size_from = size_from
        self.size_to = size_to
        #: set only for the "value" flavor (``TypeFactory.string(literal)``),
        #: used by Optional/NotUndef to render the raw literal (see
        #: ``_render_container``); everywhere else it renders as bare
        #: ``String`` (``string_PStringType`` never shows ``.value``
        #: outside Puppet's debug formatter).
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

    def assignable(self, other):
        if not isinstance(other, String):
            return False
        if self.literal is not None:
            return other.literal == self.literal
        if self.size_from is None and self.size_to is None:
            return True
        if other.literal is not None:
            n = len(other.literal)
            lo, hi = n, n
        else:
            lo, hi = other.size_from, other.size_to
        if self.size_from is not None and (lo is None or lo < self.size_from):
            return False
        if self.size_to is not None and (hi is None or hi > self.size_to):
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

    def assignable(self, other):
        if not isinstance(other, Boolean):
            return False
        return self.value is None or other.value == self.value

    def generalize(self):
        return BOOLEAN

    def _key(self):
        return (self.value,)

    def __str__(self):
        if self.value is None:
            return "Boolean"
        return "Boolean[{}]".format("true" if self.value else "false")


class Regexp(Any):
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
    TYPE_NAME = "Pattern"

    def __init__(self, sources):
        self.sources = list(sources)
        self._compiled = [_ruby_regex(s) for s in self.sources]

    def instance(self, value):
        if not isinstance(value, str):
            return False
        return any(p.search(value) for p in self._compiled)

    def _key(self):
        return tuple(self.sources)

    def __str__(self):
        return "Pattern[{}]".format(", ".join("/{}/".format(s) for s in self.sources))


class Enum(Any):
    TYPE_NAME = "Enum"

    def __init__(self, values):
        self.values = list(values)

    def instance(self, value):
        if not isinstance(value, str):
            return False
        return any(value == v for v in self.values if isinstance(v, str))

    def _key(self):
        return tuple(self.values)

    def __str__(self):
        return "Enum[{}]".format(", ".join(_literal_str(v) for v in self.values))


class Collection(Any):
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
    TYPE_NAME = "Tuple"

    def __init__(self, types, size_from=None, size_to=None):
        self.types = list(types)
        self.size_from = size_from
        self.size_to = size_to

    def _bounds(self):
        n = len(self.types)
        if self.size_from is None and self.size_to is None:
            return n, n
        lo = self.size_from if self.size_from is not None else n
        hi = self.size_to if self.size_to is not None else lo
        return lo, hi

    def instance(self, value):
        if not isinstance(value, (list, tuple)):
            return False
        lo, hi = self._bounds()
        n = len(value)
        if n < lo or n > hi:
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
        parts = [str(t) for t in self.types]
        parts += _render_size_args(self.size_from, self.size_to)
        return "Tuple[{}]".format(", ".join(parts))


class StructElement:
    __slots__ = ("key", "optional", "value_type")

    def __init__(self, key, optional, value_type):
        self.key = key
        self.optional = optional
        self.value_type = value_type

    def render_key(self):
        if self.optional:
            return "Optional[{}]".format(_puppet_quote(self.key))
        return _puppet_quote(self.key)


class Struct(Any):
    TYPE_NAME = "Struct"

    def __init__(self, elements):
        self.elements = list(elements)

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
        parts = [
            "{} => {}".format(e.render_key(), str(e.value_type)) for e in self.elements
        ]
        return "Struct[{{{}}}]".format(", ".join(parts))


class Variant(Any):
    TYPE_NAME = "Variant"

    def __init__(self, types):
        self.types = list(types)

    def instance(self, value):
        return any(_type_instance(t, value) for t in self.types)

    def assignable(self, other):
        return any(_type_assignable(t, other) for t in self.types)

    def generalize(self):
        return Variant([generalize(t) for t in self.types])

    def _key(self):
        return tuple(_key_of(t) for t in self.types)

    def __str__(self):
        return "Variant[{}]".format(", ".join(str(t) for t in self.types))


class SensitiveType(Any):
    TYPE_NAME = "Sensitive"

    def __init__(self, contained=None):
        self.contained = contained

    def instance(self, value):
        if not isinstance(value, Sensitive):
            return False
        if self.contained is None:
            return True
        return _type_instance(self.contained, value.unwrap())

    def _key(self):
        return (_key_of(self.contained),)

    def __str__(self):
        return _render_container("Sensitive", self.contained)


class TypeReference(Any):
    """An unresolved type name (unknown to the static loader), Puppet's
    ``TypeReference``. Never an instance of anything."""

    TYPE_NAME = "TypeReference"

    def __init__(self, text):
        self.text = text

    def instance(self, value):
        return False

    def _key(self):
        return (self.text,)

    def __str__(self):
        return "TypeReference[{}]".format(_puppet_quote(self.text))


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
        return "Runtime[{}, {}]".format(self.runtime, _puppet_quote(self.runtime_name))


class TypeAlias(Any):
    """One of Puppet's five static-loader aliases (``Data``, ``RichDataKey``,
    ``RichData``, ``Puppet::LookupKey``, ``Puppet::LookupValue``). Resolved
    lazily (and memoized) against its own body text, so a self-referencing
    body (``Data`` -> ``...Array[Data]``) terminates: resolution reuses this
    same cached instance rather than re-parsing."""

    TYPE_NAME = "TypeAlias"

    def __init__(self, name, body_text):
        self.alias_name = name
        self._body_text = body_text
        self._resolved = None

    @property
    def resolved_type(self):
        if self._resolved is None:
            from .parser import parse_type as _parse

            self._resolved = _parse(self._body_text)
        return self._resolved

    def instance(self, value):
        return _type_instance(self.resolved_type, value)

    def assignable(self, other):
        if other is self:
            return True
        return _type_assignable(self.resolved_type, other)

    def normalize(self):
        return self.resolved_type.normalize()

    @property
    def name(self):
        return self.alias_name

    def _key(self):
        return (self.alias_name,)

    def __str__(self):
        return self.alias_name

    def alias_expanded_str(self, _guard=None):
        guard = _guard or set()
        if self.alias_name in guard:
            return self.alias_name
        guard = guard | {self.alias_name}
        return _alias_expand(self.resolved_type, guard)


def _alias_expand(t, guard):
    if isinstance(t, TypeAlias):
        return t.alias_expanded_str(guard)
    if isinstance(t, Variant):
        return "Variant[{}]".format(", ".join(_alias_expand(x, guard) for x in t.types))
    if isinstance(t, Array) and t.element_type is not None:
        parts = [_alias_expand(t.element_type, guard)]
        parts += _render_size_args(t.size_from, t.size_to)
        return "Array[{}]".format(", ".join(parts))
    if isinstance(t, Hash) and (t.key_type is not None or t.value_type is not None):
        parts = [
            _alias_expand(t.key_type, guard) if t.key_type is not None else "Any",
            _alias_expand(t.value_type, guard) if t.value_type is not None else "Any",
        ]
        parts += _render_size_args(t.size_from, t.size_to)
        return "Hash[{}]".format(", ".join(parts))
    return str(t)


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
COLLECTION = Collection()
REGEXP = Regexp()


def _render_container(name, contained, show_literal=False):
    """Optional/NotUndef/Sensitive's own formatter (``type_formatter.rb``
    ``string_POptionalType``/``string_PNotUndefType``/``string_PSensitiveType``):
    any contained type renders by its bare ``.name`` only, never with its
    own parameters (confirmed against the ``Sensitive[Integer]`` oracle
    golden -- these three wrapper types are the ones ``short_name`` also
    keeps one bare parameter level for). ``show_literal`` is Optional/
    NotUndef's own extra special case (``string_POptionalType``/
    ``string_PNotUndefType`` only, NOT Sensitive): a literal ``String``
    child prints its quoted literal value directly instead of recursing
    into the child's own (bare) renderer -- confirmed against the
    ``Optional['integer']`` oracle golden."""
    if contained is None or (isinstance(contained, Any) and type(contained) is Any):
        return name
    if show_literal and isinstance(contained, String) and contained.literal is not None:
        return "{}[{}]".format(name, _puppet_quote(contained.literal))
    if isinstance(contained, str):
        return "{}[{}]".format(name, _puppet_quote(contained))
    return "{}[{}]".format(name, contained.name)


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


def _type_assignable(t, other):
    if isinstance(t, str):
        return isinstance(other, String) and other.literal == t
    return t.assignable(other)


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
        return Tuple([infer_set(v) for v in value]) if value else Tuple([])
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


def generalize(t):
    if isinstance(t, Any):
        return t.generalize()
    return t


def _generalized_common(types):
    types = list(types)
    uniq = []
    for t in types:
        if not any(t == u for u in uniq):
            uniq.append(t)
    if len(uniq) == 1:
        return uniq[0]
    return Variant(uniq)


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
