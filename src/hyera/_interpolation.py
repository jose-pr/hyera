# Ported from Puppet 8 lib/puppet/pops/lookup/interpolation.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Interpolation engine: resolving functions and variable references.

Ports Puppet's ``interpolation.rb``: a single left-to-right pass over each
``%{...}`` occurrence, never re-scanning inserted text -- only a method's
own result is interpolated again.
"""

import re
from decimal import Decimal

from ._navigation import _MISSING, _RUBY_STRIP_CHARS, _ruby_class, split_key, sub_lookup
from ._types import Sensitive
from .exceptions import HieraLookupError, InterpolationError

#: One ``%{...}`` occurrence (``interpolation.rb:51``'s
#: ``/%\{([^}]*)\}/``). Takes any text up to the first ``}``, stripped
#: verbatim -- unlike a plain ``str.format`` field, this admits an empty
#: expression, embedded quotes, and anything else the grammar below rejects
#: with its own error.
_EXPR_RE = re.compile(r"%\{([^}]*)\}")
#: A method call: a bare word, then a single- or double-quoted argument in
#: parentheses, with nothing else -- no whitespace around the parentheses or
#: the argument (``interpolation.rb:146``). ``re.ASCII`` matches Ruby's
#: ASCII-only ``\w``; ``re.MULTILINE`` (with ``.search``, not ``.match``)
#: matches Ruby's line-anchored ``^``/``$``.
_METHOD_RE = re.compile(
    r"""^(\w+)\((?:"([^"]+)"|'([^']+)')\)$""", re.ASCII | re.MULTILINE
)
#: ``interpolation.rb``'s ``EMPTY_INTERPOLATIONS``: a bare ``%{...}`` whose
#: (stripped) content is exactly one of these tokens always resolves to the
#: empty string, without going through scope lookup at all -- notably, an
#: *empty* quoted name (``%{""}``/``%{''}``) is not sub-key syntax to parse
#: (``split_key`` requires 1+ characters inside a quoted segment and would
#: otherwise raise a spurious "Syntax error").
_EMPTY_INTERPOLATIONS = frozenset(["", "::", '""', "''", '"::"', "'::'"])


#: Ruby ``String#inspect`` escapes for characters with a short mnemonic
#: (rather than a ``\\uXXXX`` fallback).
_RUBY_INSPECT_SIMPLE_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\t": "\\t",
    "\n": "\\n",
    "\r": "\\r",
    "\f": "\\f",
    "\v": "\\v",
    "\b": "\\b",
    "\a": "\\a",
    "\x1b": "\\e",
}


def _to_puppet_str(value) -> str:
    """A value's default string rendering for interpolation: Ruby ``to_s``.

    ``None`` -> ``""``; ``True``/``False`` -> ``"true"``/``"false"``; a
    ``str`` is itself; an ``int`` is its decimal digits; a ``float`` follows
    Ruby's ``Float#to_s`` (:func:`_float_to_s`); a ``list``/``dict`` renders
    as Ruby ``inspect`` (:func:`_ruby_inspect`); a :class:`~hyera.Sensitive`
    redacts. Anything else falls back to Python's own ``str()``.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _float_to_s(value)
    if isinstance(value, (list, dict)):
        return _ruby_inspect(value)
    if isinstance(value, Sensitive):
        return "Sensitive [value redacted]"
    return str(value)


def _ruby_inspect(value) -> str:
    """Ruby ``Object#inspect`` for a value already produced by our backends:
    ``str``, ``None``, ``bool``/``int``/``float``, ``list``, ``dict`` or
    :class:`~hyera.Sensitive` -- the shapes Ruby's ``to_json``/render path
    can actually hold. Used for a bare ``%{var}``/function-call result that
    is itself a list or hash, and recursively for their elements/keys/values.
    """
    if isinstance(value, str):
        return _ruby_inspect_str(value)
    if value is None:
        return "nil"
    if isinstance(value, (bool, int, float)):
        return _to_puppet_str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_ruby_inspect(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                "{}=>{}".format(_ruby_inspect(k), _ruby_inspect(v))
                for k, v in value.items()
            )
            + "}"
        )
    if isinstance(value, Sensitive):
        return "#<Sensitive [value redacted]>"
    return str(value)


def _ruby_inspect_str(s: str) -> str:
    """Ruby ``String#inspect``: a double-quoted, escaped form.

    ``\\`` ``"`` and the named control escapes (``\\t\\n\\r\\f\\v\\b\\a``,
    ``\\x1b`` as ``\\e``) render as their short mnemonic; ``#`` immediately
    before ``{``/``$``/``@`` is escaped (``\\#``) since Ruby would otherwise
    read it as interpolation syntax; the rest of C0, DEL, C1 and U+2028/9
    become ``\\uXXXX``; everything else -- including non-ASCII text outside
    those ranges -- is left raw.
    """
    out = ['"']
    n = len(s)
    i = 0
    while i < n:
        ch = s[i]
        escape = _RUBY_INSPECT_SIMPLE_ESCAPES.get(ch)
        if escape is not None:
            out.append(escape)
            i += 1
            continue
        if ch == "#" and i + 1 < n and s[i + 1] in "{$@":
            out.append("\\#")
            i += 1
            continue
        cp = ord(ch)
        if cp <= 0x1F or 0x7F <= cp <= 0x9F or ch in (" ", " "):
            out.append("\\u{:04X}".format(cp))
            i += 1
            continue
        out.append(ch)
        i += 1
    out.append('"')
    return "".join(out)


def _float_to_s(f: float) -> str:
    """Ruby ``Float#to_s``: fixed notation for a scientific exponent of
    -4..14 (and 15 when the shortest round-trip digits run past the point),
    ``<d>.<digits>e±NN`` otherwise; ``NaN``/``Infinity``/``-Infinity`` for
    the non-finite cases. Measured against Ruby 4.0.7 over 8,291 floats
    spanning exponents -30..39 and 1-17 significant digits (see the parent
    plan's Known Facts) -- Python's own ``repr()`` already agrees with Ruby
    in the fixed-notation range, so this only has to pick which range
    applies and reformat the scientific case.
    """
    if f != f:
        return "NaN"
    if f == float("inf"):
        return "Infinity"
    if f == float("-inf"):
        return "-Infinity"
    sign, digits, exp = Decimal(repr(f)).as_tuple()
    m = "".join(map(str, digits)).rstrip("0") or "0"
    e = len(digits) + exp - 1
    if -4 <= e and (e <= 14 or e + 1 < len(m)):
        return repr(f)
    return "{}{}.{}e{}{:02d}".format(
        "-" if sign else "", m[0], m[1:] or "0", "+" if e >= 0 else "-", abs(e)
    )


def _scope_ref(scope, ref: str, subject: str = None):
    """Resolve a dotted ``%{...}``/``scope()`` reference against a bound
    :class:`~hyera.Scope` (``interpolation.rb:87-121``, without ``strict``
    -- that side effect belongs to whichever caller wires it in).

    Returns :data:`_MISSING` on an ordinary miss (an unbound root, or a
    :func:`~hyera._navigation.sub_lookup` miss navigating further). A
    defined-nil root with no further segments returns ``None`` itself, same
    as any other bound value. Raises :class:`~hyera.HieraLookupError` on a
    malformed ``ref``, :class:`~hyera.InterpolationError` for a non-``str``
    root name (Puppet crashes on this; ``parser/scope.rb``'s own message
    for that case), or :class:`~hyera.HieraLookupError` for a navigation
    type mismatch -- ``subject`` is what an error quotes as "in string:
    <subject>", defaulting to ``%{<ref>}`` (the plain interpolation form).

    Used only by the ``str.format``-style location/datadir/format helpers in
    :mod:`hyera._location_resolver`, which have no strict routing of their
    own; the engine below (:func:`interpolate`) resolves a bare ``%{var}``
    through :meth:`~hyera.Scope.lookupvar` instead, via :func:`_scope_lookup`.
    """
    if subject is None:
        subject = "%{" + ref + "}"
    segments = split_key(
        ref, lambda p: HieraLookupError("{} in string: {}".format(p, subject))
    )
    root, rest = segments[0], segments[1:]
    if not isinstance(root, str):
        raise InterpolationError(
            "Scope variable name {} is a {}, not a string".format(
                root, _ruby_class(root)
            )
        )
    value = scope.lookup(root)
    if value is _MISSING:
        return _MISSING
    if not rest:
        return value
    result = sub_lookup(ref, rest, value)
    return _MISSING if result is _MISSING else result


def interpolate(value, invocation, allow_methods=True):
    """Fully resolve every ``%{...}`` in ``value`` (``interpolation.rb:19-32``).

    A ``str`` with no ``"%{"`` is returned unchanged; any other ``str`` is
    scanned once, left to right (:func:`_interpolate_string`). A ``list``
    interpolates each element into a new list; a ``dict`` interpolates each
    key and value into a new dict (``out[interpolate(k)] = interpolate(v)``,
    so a later duplicate key overwrites, as Ruby ``Hash#[]=`` does) -- an
    interpolated key that is not hashable raises :class:`~hyera.
    InterpolationError`. Anything else (``None``, ``bool``, ``int``,
    ``float``, already-native structures with no string inside) passes
    through unchanged.

    ``invocation`` is a :class:`~hyera._invocation.Invocation`.
    ``allow_methods=False`` (used for hierarchy locations) still allows a
    plain ``%{var}``/``%{scope('var')}`` reference; only an explicit method
    call (``%{lookup(...)}``, ``%{hiera(...)}``, ``%{alias(...)}``,
    ``%{literal(...)}``) raises.
    """
    if isinstance(value, str):
        if "%{" not in value:
            return value
        return _interpolate_string(value, invocation, allow_methods)
    if isinstance(value, list):
        return [interpolate(v, invocation, allow_methods) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            new_key = interpolate(k, invocation, allow_methods)
            try:
                hash(new_key)
            except TypeError:
                raise InterpolationError(
                    "Interpolated hash key {!r} is not hashable".format(new_key)
                ) from None
            out[new_key] = interpolate(v, invocation, allow_methods)
        return out
    return value


def _interpolate_string(subject, inv, allow_methods):
    """One left-to-right pass over every ``%{...}`` in ``subject``
    (``interpolation.rb:48-73``). Inserted text is never re-scanned by this
    pass; only a resolved method result is interpolated again, through a
    fresh call to :func:`interpolate`.
    """
    out = []
    pos = 0
    for m in _EXPR_RE.finditer(subject):
        out.append(subject[pos : m.start()])
        pos = m.end()
        expr = m.group(1).strip(_RUBY_STRIP_CHARS)
        if expr in _EMPTY_INTERPOLATIONS:
            out.append("")
            continue
        method, key = _get_method_and_data(expr, allow_methods)
        if method == "alias" and m.group(0) != subject:
            raise InterpolationError(
                "'alias' interpolation is only permitted if the expression "
                "is equal to the entire string"
            )
        resolver = _METHODS.get(method)
        if resolver is None:
            raise InterpolationError("Unknown interpolation method '{}'".format(method))
        value = resolver(key, inv, subject)
        if method == "alias":
            # The whole result, returned immediately: an alias replaces the
            # entire value (already asserted equal to `subject` above), with
            # no re-interpolation and no stringification.
            return value
        value = interpolate(value, inv, allow_methods)
        out.append(_to_puppet_str(value))
    out.append(subject[pos:])
    return "".join(out)


def _get_method_and_data(expr, allow_methods):
    """Split a stripped ``%{...}`` expression into its method name and raw
    argument (``interpolation.rb:146-157``).

    A method-call shape (``_METHOD_RE``) needs ``allow_methods``, whichever
    method it names; anything else is always a plain scope reference
    (``"scope", expr``), argument passed through verbatim (a leading
    ``::`` and any embedded ``.`` stay exactly as written).
    """
    m = _METHOD_RE.search(expr)
    if m:
        if not allow_methods:
            raise InterpolationError(
                "Interpolation using method syntax is not allowed in this context"
            )
        return m.group(1), m.group(2) if m.group(2) is not None else m.group(3)
    return "scope", expr


def _global_lookup(key, inv, subject):
    """``%{lookup(...)}``/``%{hiera(...)}``/``%{alias(...)}``
    (``interpolation.rb:77-86``): a sub-lookup through the invocation's host
    callable. A miss becomes ``""`` here (the *caller*, ``_interpolate_string``,
    returns an alias's result raw before this ever stringifies it)."""
    value = inv.lookup(key)
    return "" if value is _MISSING else value


def _scope_lookup(key, inv, subject):
    """``%{scope(...)}``/a plain ``%{var}`` reference
    (``interpolation.rb:87-121``): resolve ``key``'s root against
    ``inv.scope``, then any dotted sub-navigation via
    :func:`~hyera._navigation.sub_lookup`.

    A root present in ``inv.override_values`` wins outright. A root present
    in ``inv.default_values`` is looked up leniently (Puppet's
    ``catch(:undefined_variable)`` form: an undefined root reads as ``None``
    here, with no strict side effect); any other root goes through
    :meth:`~hyera.Scope.lookupvar`, which *does* apply ``inv.scope.strict``.
    Either way, an undefined root (``None`` and genuinely unbound, per
    :meth:`~hyera.Scope.exist`) then falls back to ``inv.default_values``
    when the root is there, else stays ``None``.
    """
    segments = split_key(
        key, lambda p: HieraLookupError("{} in string: {}".format(p, subject))
    )
    root, rest = segments[0], segments[1:]
    if not isinstance(root, str):
        raise InterpolationError(
            "Scope variable name {} is a {}, not a string".format(
                root, _ruby_class(root)
            )
        )
    scope = inv.scope
    if root in inv.override_values:
        value = inv.override_values[root]
    elif root in inv.default_values:
        looked = scope.lookup(root)
        value = None if looked is _MISSING else looked
    else:
        value = scope.lookupvar(root, lenient=inv.lenient)
    if value is None and not scope.exist(root):
        value = inv.default_values.get(root)
    if value is not None and rest:
        result = sub_lookup(key, rest, value)
        value = None if result is _MISSING else result
    return value


#: Each interpolation method's resolver, keyed by name
#: (``interpolation.rb:132-145``). ``lookup``/``hiera``/``alias`` share one
#: implementation (a sub-lookup through the invocation); only
#: ``_interpolate_string`` treats ``alias`` differently (whole-result,
#: never stringified/re-scanned).
_METHODS = {
    "lookup": _global_lookup,
    "hiera": _global_lookup,
    "alias": _global_lookup,
    "scope": _scope_lookup,
    "literal": lambda key, inv, subject: key,
}
