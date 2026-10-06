# Ported from Psych lib/psych/scalar_scanner.rb (https://github.com/ruby/psych), MIT, and Puppet 8
# lib/puppet/pops/lookup/hiera_config.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Ruby values as Psych reads them: ``RubySymbol``, ``symkeys_to_string`` and
``ScalarScanner#tokenize``.

Ground truth: ``psych-5.3.1/lib/psych/scalar_scanner.rb`` (``tokenize``) and
``puppet/pops/lookup/hiera_config.rb`` for the ``symkeys_to_string`` rule.
"""

from __future__ import annotations

import re

from .._digits import parse_decimal_int
from ..exceptions import BackendError

__all__ = ["RubySymbol", "symkeys_to_string"]


class RubySymbol:
    """A Ruby ``:symbol`` value (``!ruby/sym``/``!ruby/symbol``, or a plain
    ``:name`` scalar). Not a ``str`` subclass -- no string-typed code path
    should ever accept one by accident.

    :param name: the symbol's name, without the leading ``:``.
    """

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name: str = name

    @property
    def name(self) -> str:
        """The symbol's name, without the leading ``:``."""
        return self._name

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RubySymbol):
            return NotImplemented
        return self._name == other._name

    def __ne__(self, other: object) -> bool:
        if not isinstance(other, RubySymbol):
            return NotImplemented
        return self._name != other._name

    def __hash__(self) -> int:
        return hash((RubySymbol, self._name))

    def __reduce__(self) -> "tuple[type, tuple[str]]":
        return (RubySymbol, (self._name,))

    def __repr__(self) -> str:
        return ":{}".format(self.name)


def symkeys_to_string(obj):
    """Recursively turn ``RubySymbol`` dict keys into their plain-string
    names (``hiera_config.rb``'s ``symkeys_to_string``, applied to a parsed
    hiera.yaml and, via v3/v4 config reading, to older configs too).
    Everything else -- including a ``RubySymbol`` *value* -- is unchanged.

    Memoized per call, keyed on node identity (a ``dict``/``list`` only,
    never a scalar): a YAML anchor reused elsewhere in the same document
    parses to one shared object (PyYAML's own behavior, matching Puppet),
    and rebuilding it independently at each occurrence -- as a naive
    recursive comprehension would -- would silently turn
    that one shared node into an equal but distinct copy per position,
    before interpolation ever gets a chance to preserve or reason about the
    sharing.
    """
    return _symkeys_to_string(obj, {})


def _symkeys_to_string(obj, memo):
    if isinstance(obj, dict):
        cached = memo.get(id(obj))
        if cached is not None:
            return cached[1]
        result = {
            (key.name if isinstance(key, RubySymbol) else key): _symkeys_to_string(
                value, memo
            )
            for key, value in obj.items()
        }
        memo[id(obj)] = (obj, result)
        return result
    if isinstance(obj, list):
        cached = memo.get(id(obj))
        if cached is not None:
            return cached[1]
        result = [_symkeys_to_string(item, memo) for item in obj]
        memo[id(obj)] = (obj, result)
        return result
    return obj


# ---------------------------------------------------------------------------
# Psych::ScalarScanner#tokenize
# ---------------------------------------------------------------------------

# Ruby's TIME/FLOAT/INTEGER_LEGACY regexes, translated 1:1 (`^`/`$` are line anchors -> re.M; `[[:alpha:]]`
# -> `[^\W\d_]`; the FLOAT source's `/x` whitespace is already absent). `strict_integer: false` (Puppet's
# `safe_load` default) selects INTEGER_LEGACY (comma-tolerant).
_TIME_RE = re.compile(
    r"^-?\d{4}-\d{1,2}-\d{1,2}(?:[Tt]|\s+)\d{1,2}:\d\d:\d\d(?:\.\d*)?"
    r"(?:\s*(?:Z|[-+]\d{1,2}:?(?:\d\d)?))?$",
    re.M,
)
_DATE_RE = re.compile(r"^\d{4}-(?:1[012]|0\d|\d)-(?:[12]\d|3[01]|0\d|\d)$", re.M)
_FLOAT_RE = re.compile(r"^(?:[-+]?([0-9][0-9_,]*)?\.[0-9]*([eE][-+][0-9]+)?)$", re.M)
_FLOAT_DOT_ONLY_RE = re.compile(r"\A[-+]?\.(?=\n?\Z)")
_INTEGER_LEGACY_RE = re.compile(
    r"^(?:[-+]?0b[_,]*[0-1][0-1_,]*"
    r"|[-+]?0[_,]*[0-7][0-7_,]*"
    r"|[-+]?(?:0|[1-9](?:[0-9]|,[0-9]|_[0-9])*)"
    r"|[-+]?0x[_,]*[0-9a-fA-F][0-9a-fA-F_,]*)$",
    re.M,
)
_STRING_LEAD_RE = re.compile(
    r"^[^\d.:-]?(?:[^\W\d_]|[_\s!@#$%^&*(){}<>|/\\~;=])+", re.M
)
_YTONF_LEAD_RE = re.compile(r"^[^ytonf~]", re.I | re.M)
_NULL_RE = re.compile(r"^null$", re.I | re.M)
_TRUE_RE = re.compile(r"^(?:yes|true|on)$", re.I | re.M)
_FALSE_RE = re.compile(r"^(?:no|false|off)$", re.I | re.M)
_PLUS_INF_RE = re.compile(r"^\+?\.inf$", re.I | re.M)
_MINUS_INF_RE = re.compile(r"^-\.inf$", re.I | re.M)
_NAN_RE = re.compile(r"^\.nan$", re.I | re.M)
_SYMBOL_RE = re.compile(r"^:.", re.M)
_SYMBOL_QUOTED_RE = re.compile(r"^:([\"'])(.*)\1", re.M)
_SEXAGESIMAL_INT_RE = re.compile(r"^[-+]?[0-9][0-9_]*(?::[0-5]?[0-9]){1,2}$", re.M)
_SEXAGESIMAL_FLOAT_RE = re.compile(
    r"^[-+]?[0-9][0-9_]*(?::[0-5]?[0-9]){1,2}\.[0-9_]*$", re.M
)


def _disallowed(name: str) -> "BackendError":
    return BackendError("Tried to load unspecified class: {}".format(name))


def _parse_int_legacy(text: str) -> int:
    """Port of ``ScalarScanner#parse_int``: ``Integer(string.delete(',_'))``.

    Ruby's ``Kernel#Integer`` infers the base from a ``0b``/``0x`` prefix,
    or a bare leading ``0`` (octal, *without* requiring a ``0o`` marker --
    unlike Python's own ``int(s, 0)``, which rejects a bare-zero-prefixed
    string as invalid). Matches the INTEGER_LEGACY regex's own four
    branches: the sign, if any, is checked once and reused for every base.
    """
    cleaned = text.replace(",", "").replace("_", "")
    sign, body = ("", cleaned)
    if cleaned[:1] in "+-":
        sign, body = cleaned[0], cleaned[1:]
    lower = body.lower()
    if lower.startswith("0b"):
        return int(sign + body, 2)
    if lower.startswith("0x"):
        return int(sign + body, 16)
    if body.startswith("0") and len(body) > 1:
        return int(sign + body, 8)
    return parse_decimal_int(sign + body)


#: Ruby ``String#to_i``/``#to_f`` prefixes: single underscores between digits
#: are separators, anything after the first non-digit is ignored.
_TO_I_PREFIX_RE = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)*")
_TO_F_PREFIX_RE = re.compile(r"[+-]?[0-9]+(?:_[0-9]+)*(?:\.[0-9]+(?:_[0-9]+)*)?")


def _ruby_to_i(text: str) -> int:
    m = _TO_I_PREFIX_RE.match(text)
    return parse_decimal_int(m.group().replace("_", "")) if m else 0


def _ruby_to_f(text: str) -> float:
    m = _TO_F_PREFIX_RE.match(text)
    return float(m.group().replace("_", "")) if m else 0.0


def _tokenize(string: str):
    """Port of ``Psych::ScalarScanner#tokenize`` (INTEGER_LEGACY, i.e.
    ``strict_integer: false``, and ``parse_symbols: true``, matching
    ``Psych.safe_load(permitted_classes: [Symbol])``)."""
    if string == "":
        return None

    if _STRING_LEAD_RE.match(string) or "\n" in string:
        if len(string) > 5:
            return string
        if _YTONF_LEAD_RE.match(string):
            return string
        if string == "~" or _NULL_RE.match(string):
            return None
        if _TRUE_RE.match(string):
            return True
        if _FALSE_RE.match(string):
            return False
        return string

    if _TIME_RE.match(string):
        raise _disallowed("Time")
    if _DATE_RE.match(string):
        raise _disallowed("Date")
    if _PLUS_INF_RE.match(string):
        return float("inf")
    if _MINUS_INF_RE.match(string):
        return float("-inf")
    if _NAN_RE.match(string):
        return float("nan")
    if _SYMBOL_RE.match(string):
        m = _SYMBOL_QUOTED_RE.match(string)
        name = m.group(2) if m else string[1:]
        if name.startswith(":"):
            name = name[1:]
        return RubySymbol(name)
    if _SEXAGESIMAL_INT_RE.match(string):
        total = 0
        parts = string.split(":")
        for index, part in enumerate(parts):
            total += _ruby_to_i(part) * (60 ** abs(index - 2))
        # Only the *first* part carries the sign (Ruby: `n.to_i`, where the
        # sign lives in the first segment's own text).
        return total
    if _SEXAGESIMAL_FLOAT_RE.match(string):
        total = 0.0
        parts = string.split(":")
        for index, part in enumerate(parts):
            total += _ruby_to_f(part) * (60 ** abs(index - 2))
        return total
    if _FLOAT_RE.match(string):
        if _FLOAT_DOT_ONLY_RE.match(string):
            return string
        cleaned = string.replace(",", "").replace("_", "")
        cleaned = re.sub(r"\.([Ee]|$)", r"\1", cleaned)
        try:
            return float(cleaned)
        except ValueError:
            raise BackendError('invalid value for Float(): "{}"'.format(string))
    if _INTEGER_LEGACY_RE.match(string):
        try:
            return _parse_int_legacy(string)
        except ValueError:
            # Each _INTEGER_LEGACY_RE alternative admits only the digits valid for the base it signals and _parse_int_legacy
            # picks that base from the same prefix, so after a match `int(sign + body, base)` cannot fail. Kept as a mirror
            # of Ruby's `rescue` around `Integer()`: it depends on every alternative staying aligned with its base.
            raise BackendError('invalid value for Integer(): "{}"'.format(string))
    return string
