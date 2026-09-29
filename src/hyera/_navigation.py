"""Navigation: dotted-key sub-navigation and context lookups.

Ports Puppet's ``sub_lookup.rb`` (``split_key``, ``sub_lookup``) and
``lookup_key.rb`` (``parse_lookup_key``).
"""

import functools
import re

from .exceptions import HieraLookupError, InterpolationError

#: Sentinel for "no such context reference" (``None`` is a legitimate value).
_MISSING = object()

#: A key needs sub-key parsing only if it contains a quote or a dot
#: (``sub_lookup.rb`` ``SPECIAL``).
_SPECIAL_RE = re.compile(r"""['".]""")
#: One key segment: a double- or single-quoted run (with its surrounding
#: whitespace, so that whitespace is consumed by the segment rather than
#: left as stray delimiter text), or a run of characters that is none of
#: quote/dot. ``re.ASCII`` matches Ruby's ASCII-only ``\s`` -- Python's
#: default ``\s`` also matches Unicode whitespace such as U+00A0 NBSP.
_SEGMENT_RE = re.compile(r"""(\s*"[^"]+"\s*|\s*'[^']+'\s*|[^'".]+)""", re.ASCII)
#: A segment that is (optionally colon-prefixed -- ``sub_lookup.rb:39``'s
#: ``(:?...)`` is a literal-colon-then-digits group, not a Ruby
#: non-capturing group; the leading colon is admitted here too, then
#: dropped by ``_ruby_to_i`` since Ruby's ``to_i`` does not parse it either)
#: signed digits, and nothing else.
_INT_SEGMENT_RE = re.compile(r"^(:?[+-]?[0-9]+)$", re.M)
#: Ruby ``String#to_i``: the leading signed-digit run, or 0 if there is none.
_TO_I_RE = re.compile(r"\s*([+-]?[0-9]+)")
#: Ruby ``String#strip``'s character set (ASCII whitespace plus NUL);
#: ``str.strip()`` with no arguments strips a wider Unicode set instead.
_RUBY_STRIP_CHARS = " \t\n\v\f\r\0"


def _ruby_to_i(segment: str) -> int:
    """Ruby's ``String#to_i``: the leading signed-digit run, else 0."""
    m = _TO_I_RE.match(segment)
    return int(m.group(1)) if m else 0


def _ruby_class(value: object) -> str:
    """The Ruby class name Puppet's error text would use for ``value``."""
    if isinstance(value, bool):  # bool before int: bool is an int subclass.
        return "TrueClass" if value else "FalseClass"
    if isinstance(value, list):
        return "Array"
    if isinstance(value, str):
        return "String"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, dict):
        return "Hash"
    return type(value).__name__


def split_key(
    key: str, make_error: "Callable[[str], Exception]"
) -> "List[Union[str, int]]":
    """Split a dotted lookup key into segments (``sub_lookup.rb:21-48``).

    A key with no quote or dot is returned untouched, as a single segment.
    Otherwise it is split on unquoted ``.`` characters; a segment quoted
    with matching single or double quotes keeps its literal content
    (including any embedded ``.``), and one made only of (optionally
    signed) digits becomes an ``int`` via Ruby's ``to_i``. Malformed input
    (an unbalanced quote, an empty segment, a stray leading/trailing dot)
    raises ``make_error("Syntax error")``.
    """
    if not _SPECIAL_RE.search(key):
        return [key]

    segments = _SEGMENT_RE.split(key)
    # Ruby's String#split drops trailing empty strings; re.split keeps them.
    while segments and segments[-1] == "":
        segments.pop()
    if not segments or segments.pop(0) != "":
        raise make_error("Syntax error")

    count = len(segments)
    if count == 0:
        raise make_error("Syntax error")
    segments = [s for s in segments if s != "."]
    if len(segments) * 2 != count + 1:
        raise make_error("Syntax error")

    result = []
    for segment in segments:
        segment = segment.strip(_RUBY_STRIP_CHARS)
        if segment.startswith(("'", '"')):
            segment = segment[1:-1]
        elif _INT_SEGMENT_RE.search(segment):
            segment = _ruby_to_i(segment)
        result.append(segment)
    return result


def sub_lookup(
    key: str, segments: "Sequence[Union[str, int]]", value: object
) -> object:
    """Walk ``segments`` into ``value`` (``sub_lookup.rb:62-93``).

    Returns :data:`_MISSING` on a miss (a ``None`` value, an out-of-range or
    non-existent ``int`` segment on a list, or a segment absent from a
    dict). Raises :class:`~hyera.HieraLookupError` when a non-``int``
    segment, or an ``int`` segment against a non-list, meets a value that
    is not a ``dict`` (Puppet's ``Data Provider type mismatch``).

    Dict membership follows Ruby ``eql?``: an ``int`` segment matches only
    a key that is also an ``int`` (never a ``bool`` or ``float``), and a
    ``str`` segment matches only a ``str`` key -- never Python's looser
    ``==``, under which ``1 == True == 1.0``.
    """
    for segment in segments:
        if value is None:
            return _MISSING
        seg_is_int = isinstance(segment, int) and not isinstance(segment, bool)
        if seg_is_int and isinstance(value, list):
            if not (0 <= segment < len(value)):
                return _MISSING
            value = value[segment]
            continue
        if not isinstance(value, dict):
            raise HieraLookupError(
                "Data Provider type mismatch: Got {} when a hash-like object "
                "was expected to access value using '{}' from key '{}'".format(
                    _ruby_class(value), segment, key
                )
            )
        seg_type = type(segment)
        found = _MISSING
        for k, v in value.items():
            if type(k) is seg_type and k == segment:
                found = v
                break
        if found is _MISSING:
            return _MISSING
        value = found
    return value


@functools.lru_cache(maxsize=4096)
def parse_lookup_key(key: str) -> "Tuple[str, Tuple[Union[str, int], ...]]":
    """Split a lookup key into its root and sub-navigation segments
    (``lookup_key.rb:13-22``).

    Raises :class:`~hyera.HieraLookupError` on malformed syntax, or when the
    root segment is an ``int`` (Puppet crashes here with a Ruby
    ``NoMethodError`` -- ``0.x`` calls ``Integer#index``, which does not
    exist; this raises the same "Syntax error in key" text a genuine parse
    failure would).
    """
    segments = split_key(
        key, lambda problem: HieraLookupError("{} in key: '{}'".format(problem, key))
    )
    root, rest = segments[0], tuple(segments[1:])
    if not isinstance(root, str):
        raise HieraLookupError("Syntax error in key: '{}'".format(key))
    return root, rest


def _ctx_lookup(context, name, default=_MISSING, subject=None):
    """Resolve a ``%{...}`` context reference following Puppet's sub-key rules.

    A dotted name is nested key access only -- there is no flat-key
    precedence: a Puppet variable name cannot contain ``.``, so a context
    entry literally named ``"a.b"`` is as unreachable here as it is in
    Puppet. A malformed key, an ``int`` root (Puppet crashes on this with a
    Ruby ``NoMethodError``; this raises :class:`~hyera.InterpolationError`
    instead, matching ``parser/scope.rb``'s own message for a non-string
    variable name), or a type mismatch during the walk all raise -- only a
    root missing from ``context``, a ``None`` root value walked further, or
    an ordinary :func:`sub_lookup` miss return ``default``.

    ``subject`` is the text an error quotes as "in string: <subject>"; it
    defaults to ``%{<name>}`` (the plain interpolation form), and callers
    that already hold the original, unsubstituted source pass it through so
    the message names the whole value being resolved, not just this one
    reference.
    """
    if not isinstance(context, dict):
        return default
    if subject is None:
        subject = "%{" + name + "}"
    segments = split_key(
        name, lambda p: HieraLookupError("{} in string: {}".format(p, subject))
    )
    root, rest = segments[0], segments[1:]
    if not isinstance(root, str):
        raise InterpolationError(
            "Scope variable name {} is a {}, not a string".format(
                root, _ruby_class(root)
            )
        )
    if root not in context:
        return default
    value = context[root]
    if not rest:
        return value
    if value is None:
        return default
    result = sub_lookup(name, rest, value)
    return default if result is _MISSING else result
