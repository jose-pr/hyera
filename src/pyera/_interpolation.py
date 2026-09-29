# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Interpolation engine: resolving functions and variable references.

Ports Puppet's ``interpolation.rb``.
"""

import re
import string

from ._navigation import _ctx_lookup
from .exceptions import InterpolationError
from .util import LookupDict

function = re.compile(
    r"""%\{(scope|hiera|lookup|literal|alias)\(['"](?:::|)([^"']*)["']\)\}"""
)
# A bare ``%{var}`` reference. Excludes ``(`` so it does not also match a
# function-style ``%{hiera('x')}`` token (those are handled by ``function``);
# without this, an unresolved function leftover would be blanked here.
interpolate = re.compile(r"""%\{(?:::|)([^(}]*)\}""")
# A bare ``%{var}`` reference; the captured name becomes a ``{var}`` format
# field. The character class allows the identifier chars Puppet permits.
rformat = re.compile(r"""%\{(?:::|)([a-zA-Z0-9_.|-]+)\}""")


def _normalize_source(source: str) -> str:
    """Convert puppet ``%{var}`` references into ``str.format`` ``{var}`` fields."""
    return rformat.sub(r"{\g<1>}", source, count=0)


class _ContextFormatter(string.Formatter):
    """``str.format`` where a dotted field is *mapping*, not attribute, access.

    ``"{a.b}".format_map({"a": {"b": 1}})`` raises ``AttributeError`` because
    ``str.format`` reads ``.b`` as an attribute. Hierarchy sources are full of
    dotted references (``%{trusted.certname}``), and the contexts they resolve
    against are plain dicts — so the default behavior is never what hiera
    wants. Overriding ``get_field`` routes the whole dotted name through
    :func:`_ctx_lookup` instead of letting ``str.format`` split it.
    """

    def get_field(self, field_name, args, kwargs):
        from ._navigation import _MISSING

        value = _ctx_lookup(kwargs, field_name)
        if value is _MISSING:
            # KeyError is the signal callers already use to skip a level.
            raise KeyError(field_name)
        return value, field_name


_FORMATTER = _ContextFormatter()


def _format_source(source: str, context: dict) -> str:
    """Format a normalized source/template against a (possibly nested) context."""
    return _FORMATTER.vformat(source, (), context)


class Interpolation:
    """Mixin for interpolation: resolving functions and variable references.

    The host class must define ``get_key(key, paths, context, merge)``.
    """

    def can_resolve(self, s) -> bool:
        """True if any function call or interpolation is present in ``s``."""
        return isinstance(s, str) and bool(
            function.findall(s) or interpolate.findall(s)
        )

    def resolve_function(self, s, paths, context, merge):
        """Fully resolve hiera function calls (``%{hiera(...)}`` etc.) in ``s``."""
        calls = function.findall(s)
        # An alias replaces the whole value (no string interpolation).
        if len(calls) == 1 and calls[0][0] == "alias":
            if function.sub("", s) != "":
                raise InterpolationError(
                    "Alias cannot be used for string interpolation: `{}`".format(s)
                )
            try:
                return self.get_key(calls[0][1], paths, context, merge)
            except KeyError:
                raise InterpolationError(
                    "Alias lookup failed: key '{}' does not exist".format(calls[0][1])
                )

        for call, arg in calls:
            replace = None
            if call == "hiera" or call == "lookup":
                # Inline interpolation needs a single value; do not thread the
                # parent's array/hash merge into the referenced key.
                try:
                    replace = self.get_key(arg, paths, context, None)
                except KeyError:
                    replace = None
            elif call == "scope":
                # Dotted names resolve as nested lookups here too, so
                # %{scope('facts.os')} agrees with %{facts.os}.
                replace = _ctx_lookup(context, arg, None)
            elif call == "literal":
                replace = arg
            elif call == "alias":
                raise InterpolationError("Invalid alias function call: `{}`".format(s))
            else:  # pragma: no cover - guarded by the `function` regex
                raise InterpolationError(
                    "Unknown function call {!r} in: `{}`".format(call, s)
                )

            # Reject only a genuinely absent value; falsy results (0, "",
            # False) are legitimate and must interpolate as themselves.
            if replace is None:
                raise InterpolationError(
                    "Could not resolve value for function call: `{}`".format(s)
                )

            # A function call standing alone as the whole value keeps the
            # resolved value's native type (so `%{alias(...)}`-style single
            # calls to a list/dict pass through). When it is embedded in a
            # larger string, the resolved value is stringified.
            if function.sub("", s) == "" and len(calls) == 1:
                s = replace
            elif isinstance(replace, (str, int, float, bool)):
                text = str(replace)
                s = function.sub(lambda _m, r=text: r, s, 1)
            else:
                raise InterpolationError(
                    "Cannot interpolate non-scalar value {!r} into string: "
                    "`{}`".format(replace, s)
                )

        return s

    def resolve_interpolates(self, s, context):
        """Resolve context-based ``%{var}`` string interpolation."""
        for i in interpolate.findall(s):
            # Missing vars interpolate to empty string (matches ruby hiera).
            # Dotted names are nested lookups here too, so a reference means
            # the same thing in a value as it does in a hierarchy path.
            replacement = _ctx_lookup(context, i, "") or ""
            s = interpolate.sub(lambda _m, r=str(replacement): r, s, 1)
        return s

    def resolve(self, s, paths, context, merge):
        """Fully resolve ``s``: functions, interpolation, and nested structures.

        ``merge`` is only meaningful for a top-level ``%{alias(key)}`` (which
        may carry the caller's merge onto the aliased key). Nested structure
        elements resolve without it — accumulation happens once, in get_key.
        """
        if isinstance(s, dict):
            return self.resolve_dict(s, paths, context, None)
        elif isinstance(s, list):
            return list(self.resolve_list(s, paths, context, None))
        elif not self.can_resolve(s):
            return s

        base = self.resolve_function(s, paths, context, merge)
        if isinstance(base, str):
            base = self.resolve_interpolates(base, context)
        return base

    def resolve_dict(self, obj, paths, context, merge):
        new_obj = LookupDict()
        for k, v in obj.items():
            new_obj[k] = self.resolve(v, paths, context, merge)
        return new_obj

    def resolve_list(self, obj, paths, context, merge):
        for item in obj:
            yield self.resolve(item, paths, context, merge)
