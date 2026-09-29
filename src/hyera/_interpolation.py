# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/pops/lookup/interpolation.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Interpolation engine: resolving functions and variable references.

Ports Puppet's ``interpolation.rb``.
"""

import re
import string

from ._navigation import _MISSING, _RUBY_STRIP_CHARS, _ruby_class, split_key, sub_lookup
from .exceptions import HieraLookupError, InterpolationError

_FUNCTION_RE = re.compile(
    r"""%\{(scope|hiera|lookup|literal|alias)\(['"](?:::|)([^"']*)["']\)\}"""
)
# A bare ``%{var}`` reference. Excludes ``(`` so it does not also match a
# function-style ``%{hiera('x')}`` token (those are handled by ``_FUNCTION_RE``);
# without this, an unresolved function leftover would be blanked here.
_INTERP_RE = re.compile(r"""%\{(?:::|)([^(}]*)\}""")
# A bare ``%{var}`` reference; the captured name becomes a ``{var}`` format
# field. Narrower than Puppet's own ``%{...}`` (``interpolation.rb:51-54``),
# which takes any text up to the closing ``}`` and strips it verbatim --
# this class is only what a var/datadir/mapped_paths reference actually
# needs to spell, not a claim about what Puppet itself permits there.
_FORMAT_RE = re.compile(r"""%\{(?:::|)([a-zA-Z0-9_.|-]+)\}""")
#: ``interpolation.rb``'s ``EMPTY_INTERPOLATIONS``: a bare ``%{...}`` whose
#: (stripped) content is exactly one of these tokens always resolves to the
#: empty string, without going through scope lookup at all -- notably, an
#: *empty* quoted name (``%{""}``/``%{''}``) is not sub-key syntax to parse
#: (``split_key`` requires 1+ characters inside a quoted segment and would
#: otherwise raise a spurious "Syntax error").
_EMPTY_INTERPOLATIONS = frozenset(["", "::", '""', "''", '"::"', "'::'"])


def _normalize_source(source: str) -> str:
    """Convert puppet ``%{var}`` references into ``str.format`` ``{var}`` fields."""
    return _FORMAT_RE.sub(r"{\g<1>}", source, count=0)


def _to_puppet_str(value) -> str:
    """A value's default string rendering for interpolation: ``None`` -> ``""``,
    ``True``/``False`` -> ``"true"``/``"false"``, anything else -> ``str(value)``.
    Floats and collections keep Python's own ``str()`` for now -- Ruby's
    exact ``to_s``/inspect forms are a later refinement."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


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


class _ContextFormatter(string.Formatter):
    """``str.format`` where a dotted field is *nested Scope lookup*, not
    attribute access.

    ``"{a.b}".format_map({"a": {"b": 1}})`` raises ``AttributeError`` because
    ``str.format`` reads ``.b`` as an attribute. Hierarchy sources are full of
    dotted references (``%{trusted.certname}``), so ``get_field`` routes the
    whole dotted name through :func:`_scope_ref` instead of letting
    ``str.format`` split it. The bound scope is threaded through as the
    ``kwargs`` position of :meth:`vformat`/:meth:`get_field` -- it is never
    an actual mapping, only ``get_field`` reads it.
    """

    def get_field(self, field_name, args, scope):
        value = _scope_ref(scope, field_name)
        if value is _MISSING:
            # KeyError is the signal callers already use to skip a level.
            raise KeyError(field_name)
        return value, field_name

    def format_field(self, value, spec):
        if spec:
            return super().format_field(value, spec)
        return _to_puppet_str(value)


_FORMATTER = _ContextFormatter()


def _format_source(source: str, scope) -> str:
    """Format a normalized source/template against a bound :class:`~hyera.Scope`."""
    return _FORMATTER.vformat(source, (), scope)


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
