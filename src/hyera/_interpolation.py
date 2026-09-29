# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/pops/lookup/interpolation.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Interpolation engine: resolving functions and variable references.

Ports Puppet's ``interpolation.rb``.
"""

import re
from decimal import Decimal

from ._navigation import _MISSING, _RUBY_STRIP_CHARS, _ruby_class, split_key, sub_lookup
from ._types import Sensitive
from .exceptions import HieraLookupError, InterpolationError

_FUNCTION_RE = re.compile(
    r"""%\{(scope|hiera|lookup|literal|alias)\(['"](?:::|)([^"']*)["']\)\}"""
)
# A bare ``%{var}`` reference. Excludes ``(`` so it does not also match a
# function-style ``%{hiera('x')}`` token (those are handled by ``_FUNCTION_RE``);
# without this, an unresolved function leftover would be blanked here.
_INTERP_RE = re.compile(r"""%\{(?:::|)([^(}]*)\}""")
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


class Interpolation:
    """Mixin for interpolation: resolving functions and variable references.

    The host class must define ``_get_key(key, paths, scope, merge)``.
    """

    def _can_resolve(self, s) -> bool:
        """True if any function call or interpolation is present in ``s``."""
        return isinstance(s, str) and bool(
            _FUNCTION_RE.findall(s) or _INTERP_RE.findall(s)
        )

    def _resolve_function(self, s, paths, scope, merge):
        """Fully resolve hiera function calls (``%{hiera(...)}`` etc.) in ``s``."""
        # Captured before the loop rebinds `s`: a %{...} syntax error names
        # the whole, original, unsubstituted value in its message, as
        # Puppet's own `interpolation.rb` does.
        subject = s
        calls = _FUNCTION_RE.findall(s)
        # An alias replaces the whole value (no string interpolation).
        if len(calls) == 1 and calls[0][0] == "alias":
            if _FUNCTION_RE.sub("", s) != "":
                raise InterpolationError(
                    "Alias cannot be used for string interpolation: `{}`".format(s)
                )
            try:
                return self._get_key(calls[0][1], paths, scope, merge)
            except KeyError:
                raise InterpolationError(
                    "Alias lookup failed: key '{}' does not exist".format(calls[0][1])
                ) from None

        for call, arg in calls:
            replace = None
            if call == "hiera" or call == "lookup":
                # Inline interpolation needs a single value; do not thread the
                # parent's array/hash merge into the referenced key.
                try:
                    replace = self._get_key(arg, paths, scope, None)
                except KeyError:
                    replace = None
                # Reject only a genuinely absent value; falsy results (0,
                # "", False) are legitimate and must interpolate as
                # themselves.
                if replace is None:
                    raise InterpolationError(
                        "Could not resolve value for function call: `{}`".format(s)
                    )
            elif call == "scope":
                # Dotted names resolve as nested lookups here too, so
                # %{scope('facts.os')} agrees with %{facts.os}. A
                # defined-nil value is legitimate (renders as "" below);
                # only a genuine miss raises.
                replace = _scope_ref(scope, arg, subject=subject)
                if replace is _MISSING:
                    raise InterpolationError(
                        "Could not resolve value for function call: `{}`".format(s)
                    )
            elif call == "literal":
                replace = arg
            elif call == "alias":
                raise InterpolationError("Invalid alias function call: `{}`".format(s))
            else:  # pragma: no cover - guarded by the `function` regex
                raise InterpolationError(
                    "Unknown function call {!r} in: `{}`".format(call, s)
                )

            # A function call standing alone as the whole value keeps the
            # resolved value's native type (so `%{alias(...)}`-style single
            # calls to a list/dict pass through). When it is embedded in a
            # larger string, the resolved value is stringified (``None``
            # and booleans through ``_to_puppet_str``, matching a plain
            # ``%{var}`` reference).
            if _FUNCTION_RE.sub("", s) == "" and len(calls) == 1:
                s = replace
            elif replace is None or isinstance(replace, (str, int, float, bool)):
                text = _to_puppet_str(replace)
                s = _FUNCTION_RE.sub(lambda _m, r=text: r, s, 1)
            else:
                raise InterpolationError(
                    "Cannot interpolate non-scalar value {!r} into string: "
                    "`{}`".format(replace, s)
                )

        return s

    def _resolve_interpolates(self, s, scope):
        """Resolve scope-based ``%{var}`` string interpolation."""
        # Captured before the loop rebinds `s`, same reasoning as
        # `_resolve_function`.
        subject = s
        for i in _INTERP_RE.findall(s):
            if i.strip(_RUBY_STRIP_CHARS) in _EMPTY_INTERPOLATIONS:
                replacement = ""
            else:
                # A genuine miss interpolates to empty string (matches ruby
                # hiera). Dotted names are nested lookups here too, so a
                # reference means the same thing in a value as in a path.
                value = _scope_ref(scope, i, subject=subject)
                replacement = "" if value is _MISSING else _to_puppet_str(value)
            s = _INTERP_RE.sub(lambda _m, r=str(replacement): r, s, 1)
        return s

    def _resolve(self, s, paths, scope, merge):
        """Fully resolve ``s``: functions, interpolation, and nested structures.

        ``merge`` is only meaningful for a top-level ``%{alias(key)}`` (which
        may carry the caller's merge onto the aliased key). Nested structure
        elements resolve without it — accumulation happens once, in ``_get_key``.
        """
        if isinstance(s, dict):
            return self._resolve_dict(s, paths, scope, None)
        elif isinstance(s, list):
            return list(self._resolve_list(s, paths, scope, None))
        elif not self._can_resolve(s):
            return s

        base = self._resolve_function(s, paths, scope, merge)
        if isinstance(base, str):
            base = self._resolve_interpolates(base, scope)
        return base

    def _resolve_dict(self, obj, paths, scope, merge):
        new_obj = {}
        for k, v in obj.items():
            new_obj[k] = self._resolve(v, paths, scope, merge)
        return new_obj

    def _resolve_list(self, obj, paths, scope, merge):
        for item in obj:
            yield self._resolve(item, paths, scope, merge)
