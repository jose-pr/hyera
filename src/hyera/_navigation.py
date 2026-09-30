# Ported from Puppet 8 lib/puppet/pops/lookup/sub_lookup.rb, lookup_key.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Navigation: dotted-key sub-navigation.

Ports Puppet's ``sub_lookup.rb`` (``split_key``, ``sub_lookup``) and
``lookup_key.rb`` (``parse_lookup_key``). ``join_key`` is original code
(``split_key``'s display-only inverse, needed for a hyera-only tuple key
path's message text): no phiera/Puppet-source header.
"""

import contextlib
import functools
import re

from .exceptions import HieraLookupError


class _Unset:
    """The type of :data:`_MISSING`, hiera's "no default was given"
    sentinel (Puppet's own concept: a lookup with a real default of
    ``None`` still differs from one with no default at all).

    A plain ``object()`` would work as a sentinel too, but would render as
    ``<object object at 0x...>`` in ``help()``/``inspect.signature()`` and
    give a different object back from ``copy.deepcopy``/``pickle`` --
    :meth:`__repr__` and :meth:`__reduce__` fix both, with no other
    behavior change.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"

    def __reduce__(self) -> str:
        return "_MISSING"


#: Sentinel for "not found"/"undefined" (``None`` is a legitimate value).
#: The only instance of :class:`_Unset`; renders as ``<unset>`` and keeps
#: its identity through ``copy``/``pickle``.
_MISSING = _Unset()
#: The shared no-op context manager :func:`sub_lookup` uses when it was
#: called with no ``invocation`` at all (``dig``/``get``/``getvar``).
_NULL_CONTEXT = contextlib.nullcontext()


def _rec(invocation, kind, qualifier):
    return (
        invocation.recording(kind, qualifier)
        if invocation is not None
        else _NULL_CONTEXT
    )


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
    key: str, segments: "Sequence[Union[str, int]]", value: object, invocation=None
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

    ``invocation``, when given, records the walk (``recording("sub_lookup",
    segments)``, then one ``recording("segment", seg)`` per step with
    ``report_found``/``report_not_found``); every call site that already
    holds an ``Invocation`` passes it, except ``dig``/``get``/``getvar``,
    whose own navigation is not a hierarchy lookup dig at all.
    """
    with _rec(invocation, "sub_lookup", segments):
        for segment in segments:
            with _rec(invocation, "segment", segment):
                if value is None:
                    if invocation is not None:
                        invocation.report_not_found(segment)
                    return _MISSING
                seg_is_int = isinstance(segment, int) and not isinstance(segment, bool)
                if seg_is_int and isinstance(value, list):
                    if not (0 <= segment < len(value)):
                        if invocation is not None:
                            invocation.report_not_found(segment)
                        return _MISSING
                    value = value[segment]
                    if invocation is not None:
                        invocation.report_found(segment, value)
                    continue
                if not isinstance(value, dict):
                    raise HieraLookupError(
                        "Data Provider type mismatch: Got {} when a hash-like "
                        "object was expected to access value using '{}' from "
                        "key '{}'".format(_ruby_class(value), segment, key)
                    )
                seg_type = type(segment)
                found = _MISSING
                for k, v in value.items():
                    if type(k) is seg_type and k == segment:
                        found = v
                        break
                if found is _MISSING:
                    if invocation is not None:
                        invocation.report_not_found(segment)
                    return _MISSING
                value = found
                if invocation is not None:
                    invocation.report_found(segment, value)
        return value


def join_key(segments: "Sequence[Union[str, int]]") -> str:
    """Render ``[root, *segments]`` as Puppet's own dotted key text --
    the display-only inverse of :func:`split_key`, used wherever a name
    (including a ``hyera``-only tuple key path, which has no natural
    string spelling of its own) needs to appear in a message.

    An ``int`` segment is bare. A ``str`` segment is bare only if it would
    round-trip back through :func:`split_key` unchanged: quoted whenever
    it holds a ``.`` or a quote character (either would otherwise be
    misread as syntax), and additionally quoted whenever there is more
    than one segment and it looks like an integer (a bare digit-only
    segment there would parse back as an ``int``, not this ``str`` --
    Puppet's own quoted ``'0'`` segment rule). A quoted segment uses
    double quotes, unless it holds a ``"`` and no ``'``, in which case it
    uses single quotes; a segment holding both is still double-quoted,
    as-is (no escaping -- this exact text may not itself round-trip, but
    nothing in Puppet's own dotted-key grammar can spell it either).
    """
    multi = len(segments) > 1
    parts = []
    for segment in segments:
        if isinstance(segment, int) and not isinstance(segment, bool):
            parts.append(str(segment))
            continue
        needs_quote = bool(_SPECIAL_RE.search(segment)) or (
            multi and bool(_INT_SEGMENT_RE.search(segment))
        )
        if not needs_quote:
            parts.append(segment)
            continue
        has_dq = '"' in segment
        has_sq = "'" in segment
        quote = "'" if (has_dq and not has_sq) else '"'
        parts.append(quote + segment + quote)
    return ".".join(parts)


def key_to_a(root, segments) -> "Tuple[Union[str, int], ...]":
    """A ``data_dig`` function's full key argument (``lookup_key.rb:76-83``):
    the root plus every sub-navigation segment, in order."""
    return (root,) + tuple(segments)


def undig(segments, value) -> object:
    """Rebuild a nested structure from ``value`` so that digging ``segments``
    back out of it (:func:`sub_lookup`) returns ``value`` again
    (``lookup_key.rb:64-74``).

    A ``data_dig`` function already receives the full key
    (:func:`key_to_a`) and returns the value found at that exact path --
    the leaf, not a root-keyed hash. Wrapping it back up by ``segments``
    (an ``int`` segment builds a list just long enough to hold ``value`` at
    that index, anything else a single-key dict) lets the generic
    per-level/per-location merge treat a ``data_dig`` result exactly like a
    ``data_hash`` root value, with the caller's own :func:`sub_lookup` over
    the same ``segments`` recovering the original leaf after the merge.
    """
    for segment in reversed(segments):
        if isinstance(segment, int) and not isinstance(segment, bool):
            lst = [None] * (segment + 1)
            lst[segment] = value
            value = lst
        else:
            value = {segment: value}
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
