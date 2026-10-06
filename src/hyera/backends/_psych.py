# Ported from Psych lib/psych/scalar_scanner.rb, lib/psych/visitors/to_ruby.rb
# (https://github.com/ruby/psych), MIT. Modified by jose-pr. See NOTICE.
# Ported from Puppet 8 lib/puppet/util/yaml.rb, lib/puppet/pops/lookup/hiera_config.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Ports Psych ``safe_load`` + ``ScalarScanner`` as ``Puppet::Util::Yaml`` uses
them, on top of PyYAML (``CSafeLoader`` when libyaml is available, else the
pure-Python ``SafeLoader``).

Ground truth: ``psych-5.3.1/lib/psych/scalar_scanner.rb`` (``tokenize``),
``psych-5.3.1/lib/psych/visitors/to_ruby.rb`` (``deserialize``), and
``puppet/util/yaml.rb``/``puppet/pops/lookup/hiera_config.rb`` for the
one-document, ``nil``-is-``false`` and ``symkeys_to_string`` rules.
"""

from __future__ import annotations

import base64
import re

import yaml

from .._digits import parse_decimal_int
from ..exceptions import BackendError

__all__ = ["RubySymbol", "symkeys_to_string", "safe_load"]


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
    and rebuilding it independently at each occurrence -- the naive
    recursive-comprehension form this used to be -- would silently turn
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

# Ruby's TIME/FLOAT/INTEGER_LEGACY regexes, translated 1:1 (Ruby `^`/`$` are
# line anchors -> re.M; `[[:alpha:]]` -> `[^\W\d_]`; Ruby's `/x` extended
# mode just ignores whitespace/comments in the FLOAT source, already absent
# here). `scalar_scanner.rb` uses `strict_integer: false` (Puppet's
# `Psych::YAMLTree`/`safe_load` default), so INTEGER_LEGACY (comma-tolerant).
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
            # Each of _INTEGER_LEGACY_RE's four alternatives only ever
            # admits the digit characters valid for the base it signals
            # (0-1 for "0b", 0-7 for a bare leading "0", 0-9a-fA-F for
            # "0x", 0-9 otherwise), and _parse_int_legacy picks that same
            # base from that same prefix -- so once the regex has matched,
            # stripping the "," / "_" separators it also allows can never
            # leave `int(sign + body, base)` an invalid literal. Kept as a
            # direct mirror of Ruby's own `rescue` around `Integer()`
            # (matching the sibling Float conversion just above, which
            # *is* reachable) rather than assumed dead: unlike that
            # invariant, this one depends on every one of the four
            # alternatives staying exactly aligned with its base's digit
            # set, which a future edit here could silently break.
            raise BackendError('invalid value for Integer(): "{}"'.format(string))
    return string


# ---------------------------------------------------------------------------
# PyYAML loader/resolver/constructor wiring
# ---------------------------------------------------------------------------

_PLAIN_TAG = "tag:hyera.internal,2026:plain"

_MERGE_TAG = "tag:yaml.org,2002:merge"
_MERGE_SCALAR = "<<"


class _PsychResolverMixin:
    """Retags every implicit (untagged, plain-style) scalar as
    :data:`_PLAIN_TAG`, so none of PyYAML's own bool/int/float/null/
    timestamp implicit resolvers (Python-flavored) ever fire -- the
    constructor for :data:`_PLAIN_TAG` calls :func:`_tokenize` instead,
    which is Ruby/Psych-flavored. An untagged ``<<`` scalar, plain or
    quoted, resolves to the merge tag; only an explicit ``!!str`` keeps it
    from merging (``_revive_hash``).
    """

    def resolve(self, kind, value, implicit):
        if kind is yaml.nodes.ScalarNode:
            if value == _MERGE_SCALAR:
                return _MERGE_TAG
            if implicit[0]:
                return _PLAIN_TAG
        return super().resolve(kind, value, implicit)


def _construct_plain(loader, node):
    if not isinstance(node, yaml.nodes.ScalarNode):
        return _construct_unknown(loader, None, node)
    return _tokenize(loader.construct_scalar(node))


def _construct_str(loader, node):
    if not isinstance(node, yaml.nodes.ScalarNode):
        # `!str`/`!ruby/string` on a non-scalar node is an obscure Ruby
        # ivars-on-a-String encoding with no fixture coverage; fall back to
        # ordinary construction by node kind rather than crashing.
        return _construct_unknown(loader, None, node)
    return loader.construct_scalar(node)


def _construct_str_multi(loader, tag_suffix, node):
    # Multi-constructor form for the "!ruby/string" prefix (covers the bare
    # tag and every "!ruby/string:X" subclass suffix alike).
    return _construct_str(loader, node)


def _construct_binary(loader, node):
    if not isinstance(node, yaml.nodes.ScalarNode):
        return _construct_unknown(loader, None, node)
    text = loader.construct_scalar(node)
    # Non-validating, like Ruby's `String#unpack('m')` -- lenient base64,
    # matching `!!binary '%%%'` -> "" in the oracle probes.
    try:
        raw = base64.b64decode(text, validate=False)
    except Exception:
        raw = b""
    return raw.decode("utf-8", "surrogateescape")


def _construct_float(loader, node):
    if not isinstance(node, yaml.nodes.ScalarNode):
        return _construct_unknown(loader, None, node)
    text = loader.construct_scalar(node)
    value = _tokenize(text)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise BackendError('invalid value for Float(): "{}"'.format(text))


def _construct_ruby_symbol(loader, node):
    return RubySymbol(loader.construct_scalar(node))


def _construct_ruby_disallowed(loader, tag_suffix, node):
    # tag_suffix is node.tag with the matched "!ruby/" prefix stripped, e.g.
    # "object:Foo" -> "Foo" (text after ":"), "regexp" -> "Regexp" (kind,
    # capitalized), "object" alone -> "Object".
    if ":" in tag_suffix:
        name = tag_suffix.split(":", 1)[1] or "Object"
    elif tag_suffix == "object" or tag_suffix == "":
        name = "Object"
    else:
        name = tag_suffix[:1].upper() + tag_suffix[1:]
    raise _disallowed(name)


def _construct_omap(loader, node):
    """``visit_Psych_Nodes_Sequence``'s ``!!omap`` branch: each entry maps its
    first child to its last child."""
    if isinstance(node, yaml.nodes.ScalarNode):
        return _construct_unknown(loader, None, node)
    if not isinstance(node, yaml.nodes.SequenceNode):
        raise _disallowed("Psych::Omap")
    result = _Mapping()
    for child in node.value:
        if isinstance(child, yaml.nodes.MappingNode) and child.value:
            first, last = child.value[0][0], child.value[-1][1]
        elif isinstance(child, yaml.nodes.SequenceNode) and child.value:
            first, last = child.value[0], child.value[-1]
        else:
            raise BackendError("invalid entry in !!omap")
        key = _freeze_key(loader.construct_object(first, deep=True))
        result.store(key, loader.construct_object(last, deep=True))
    return result.items


def _construct_set_disallowed(loader, node):
    if not isinstance(node, yaml.nodes.MappingNode):
        return _construct_by_kind(loader, node)
    raise _disallowed("Psych::Set")


def _construct_by_kind(loader, node):
    """Build ``node`` by its kind, whatever its tag says."""
    if isinstance(node, yaml.nodes.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.nodes.MappingNode):
        return _revive_hash(loader, node)
    return _tokenize(loader.construct_scalar(node))


def _construct_unknown(loader, tag_suffix, node):
    return _construct_by_kind(loader, node)


_STR_TAG = "tag:yaml.org,2002:str"


def _ruby_eql(a, b) -> bool:
    """Ruby ``eql?`` for two keys that already compare ``==`` in Python:
    ``1``, ``1.0`` and ``True`` are one key to Python and three to Ruby."""
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(_ruby_eql(x, y) for x, y in zip(a, b))
    return True


def _describe_key(key) -> str:
    if isinstance(key, (bool, int, float)):
        return repr(key)
    return "a composite key"


class _Mapping:
    """A dict under construction. Python merges keys that Ruby keeps apart
    (``1``/``1.0``/``True``); that is an error here, never a silent merge."""

    __slots__ = ("items", "_keys")

    def __init__(self):
        self.items = {}
        self._keys = {}

    def store(self, key, value):
        prior = self._keys.setdefault(key, key)
        if prior is not key and not _ruby_eql(prior, key):
            raise BackendError(
                "mapping keys {} and {} are indistinguishable to hyera".format(
                    _describe_key(prior), _describe_key(key)
                )
            )
        self.items[key] = value

    def update(self, other):
        for key, value in other.items():
            self.store(key, value)


def _merge_sources(key_node, value_node, value):
    """The mappings a ``<<`` entry merges, or ``None`` when Psych keeps the
    ``<<`` key literally (``to_ruby.rb`` ``revive_hash``)."""
    # An alias resolves to the anchored node, defined before the key.
    is_alias = value_node.start_mark.index < key_node.start_mark.index
    if isinstance(value_node, yaml.nodes.SequenceNode) and not is_alias:
        if isinstance(value, list) and all(isinstance(m, dict) for m in value):
            return list(reversed(value))
        return None
    return [value] if isinstance(value, dict) else None


def _revive_hash(loader, node):
    """Port of ``revive_hash``: entries in document order, a ``<<`` key
    merging into the entries so far (the merged value wins) unless it is
    tagged ``!!str``; the last duplicate key wins."""
    result = _Mapping()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        value = loader.construct_object(value_node, deep=True)
        if isinstance(key, str) and key == "<<" and key_node.tag != _STR_TAG:
            sources = _merge_sources(key_node, value_node, value)
            if sources is not None:
                merged = _Mapping()
                for source in sources:
                    merged.update(source)
                result.update(merged.items)
                continue
        result.store(_freeze_key(key), value)
    return result.items


def _freeze_key(key):
    """An unhashable key (list/dict) is frozen into a hashable tuple,
    recursively, so it can be used as a Python dict key at all (Ruby has no
    such restriction: any value can be a Hash key)."""
    if isinstance(key, list):
        return tuple(_freeze_key(v) for v in key)
    if isinstance(key, dict):
        return tuple((k, _freeze_key(v)) for k, v in key.items())
    return key


def _make_loader_class(base, *mixins):
    class _Loader(*mixins, _PsychResolverMixin, base):
        pass

    _Loader.add_constructor(_PLAIN_TAG, _construct_plain)
    # PyYAML's SafeLoader already registers Python-flavored constructors
    # for these core tags (e.g. `construct_yaml_int`); overriding them here
    # (not just relying on the `None` catch-all, which only ever sees an
    # *unregistered* tag) makes an *explicit* `!!int`/`!!bool`/etc tag route
    # through the same Ruby-flavored `_tokenize` as an implicit plain
    # scalar does, matching `deserialize`'s tokenize bucket.
    for tag in (
        "tag:yaml.org,2002:null",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:timestamp",
        "tag:yaml.org,2002:value",
        "tag:yaml.org,2002:merge",
    ):
        _Loader.add_constructor(tag, _construct_plain)
    for prefix in ("!str", "tag:yaml.org,2002:str"):
        _Loader.add_constructor(prefix, _construct_str)
    for prefix in ("!binary", "tag:yaml.org,2002:binary"):
        _Loader.add_constructor(prefix, _construct_binary)
    for prefix in ("!float", "tag:yaml.org,2002:float"):
        _Loader.add_constructor(prefix, _construct_float)
    _Loader.add_constructor("!ruby/sym", _construct_ruby_symbol)
    _Loader.add_constructor("!ruby/symbol", _construct_ruby_symbol)
    for prefix in ("!omap", "tag:yaml.org,2002:omap"):
        _Loader.add_constructor(prefix, _construct_omap)
    for prefix in ("!set", "tag:yaml.org,2002:set"):
        _Loader.add_constructor(prefix, _construct_set_disallowed)
    for prefix in (
        "!seq",
        "tag:yaml.org,2002:seq",
        "!pairs",
        "tag:yaml.org,2002:pairs",
        "!map",
        "tag:yaml.org,2002:map",
    ):
        _Loader.add_constructor(prefix, _construct_by_kind)
    # Registration order matters (PyYAML tries multi-constructor *prefixes*
    # in insertion order, first match wins): the specific "!ruby/string"
    # prefix must be added before the broad "!ruby/" one, or every
    # `!ruby/string[:X]` scalar would be swallowed by the disallowed-class
    # handler instead of returning its text.
    _Loader.add_multi_constructor("!ruby/string", _construct_str_multi)
    _Loader.add_multi_constructor("!ruby/", _construct_ruby_disallowed)
    _Loader.add_multi_constructor(None, _construct_unknown)
    return _Loader


class _RedefinableAnchors:
    """Composer mixin: a repeated anchor name rebinds to the latest definition
    (``to_ruby.rb`` keeps one table and overwrites), where PyYAML's pure-Python
    composer raises. libyaml's composer cannot be hooked; :func:`safe_load`
    routes a document with a repeated anchor to the pure-Python loader."""

    def compose_node(self, parent, index):
        if not self.check_event(yaml.events.AliasEvent):
            anchor = self.peek_event().anchor
            if anchor is not None:
                self.anchors.pop(anchor, None)
        return super().compose_node(parent, index)


_C_LOADER = (
    _make_loader_class(yaml.CSafeLoader)
    if getattr(yaml, "__with_libyaml__", False)
    else None
)
_PURE_LOADER = _make_loader_class(yaml.SafeLoader, _RedefinableAnchors)
_LOADER = _C_LOADER or _PURE_LOADER


#: A quoted token in a PyYAML problem text. Alias, tag-handle and
#: escape-character names quote source text verbatim, so they are redacted;
#: the parser's own structural tokens (``','``, ``']'``, ``'<block end>'``)
#: are part of the message and kept.
_YAML_QUOTED_TOKEN_RE = re.compile(r"'([^']*)'")
_YAML_FIXED_TOKEN_RE = re.compile(r"[,\]\[{}:?-]|<[a-z ]+>")


def _redact_token(match) -> str:
    if _YAML_FIXED_TOKEN_RE.fullmatch(match.group(1)):
        return match.group(0)
    return "'<redacted>'"


def _yaml_problem(exc) -> str:
    """Summarize a YAML parse error with no plaintext, in Psych's shape:
    ``<problem> <context> at line L column C``. Never the decrypted data, a
    source snippet (``mark.get_snippet()``), ``str(exc)`` itself, or a
    quoted token embedded in the reason text.

    ``problem``/``context`` are PyYAML's own fixed phrases (tokens and tags
    at most, per Ruby Psych's ``[problem, context].compact.join(' ')``) --
    joining both, when present, matches Puppet's own message text. Position
    is the context mark when present, else the problem mark, both 1-based.
    """
    if isinstance(exc, yaml.MarkedYAMLError):
        parts = [p for p in (exc.problem, exc.context) if p]
        text = " ".join(parts)
        mark = exc.context_mark or exc.problem_mark
        if text:
            text = _YAML_QUOTED_TOKEN_RE.sub(_redact_token, text)
        if mark is not None:
            return "{} at line {} column {}".format(
                text, mark.line + 1, mark.column + 1
            ).strip()
        if text:
            return text
    elif isinstance(exc, yaml.reader.ReaderError):
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        return "{} at position {}".format(first_line, exc.position)
    return type(exc).__name__


#: Deepest collection nesting :func:`safe_load` reads. libyaml's composer
#: recurses in C without a bound and ends the process on Python 3.9; Psych
#: itself gives up near 10000 levels.
_MAX_NESTING = 500
_TOO_DEEP = "nested too deeply (more than {} levels)".format(_MAX_NESTING)

_DEPTH_STEP = {
    yaml.events.SequenceStartEvent: 1,
    yaml.events.MappingStartEvent: 1,
    yaml.events.SequenceEndEvent: -1,
    yaml.events.MappingEndEvent: -1,
}
_ANCHORED = frozenset(
    (
        yaml.events.ScalarEvent,
        yaml.events.SequenceStartEvent,
        yaml.events.MappingStartEvent,
    )
)
_DOCUMENT_END = yaml.events.DocumentEndEvent


#: A run of line breaks, blanks and block indicators. A block collection
#: starts after nothing but such a run on its line (indentation plus compact
#: ``- ``, ``? ``, ``: `` entries), so its column is at most the longest run.
_BLOCK_PREFIX_RE = re.compile("[ \t\r\n\x85  \\-?:]+")
_ANCHOR_NAME_RE = re.compile("&[0-9A-Za-z_-]+")


def _nesting_bound(text: str) -> int:
    """An upper bound on the collection nesting of any document in ``text``,
    from string operations alone.

    Flow collections: each needs a ``[`` or ``{``. Block collections: a
    child is at a deeper column, or at the same column only as the indentless
    sequence of a mapping, so at most two per column, and no column exceeds
    the longest block-prefix run. Quotes, comments, block scalars and later
    documents can only add to the count.
    """
    longest = max(map(len, _BLOCK_PREFIX_RE.findall(text)), default=0)
    return text.count("[") + text.count("{") + 2 * (longest + 1)


def _within_nesting_limit(text: str) -> bool:
    """``_nesting_bound(text) <= _MAX_NESTING``, without building the runs."""
    room = (_MAX_NESTING - text.count("[") - text.count("{")) // 2
    if room < 1:
        return False
    # The bound holds when no run is `room` characters long.
    return re.search("[ \t\r\n\x85  \\-?:]{%d}" % room, text) is None


def _anchors_may_repeat(text: str) -> bool:
    """Whether some anchor name might be defined twice (an anchor starts with
    ``&``; the name prefix is compared, so a repeat is never missed)."""
    if text.count("&") < 2:
        return False
    names = _ANCHOR_NAME_RE.findall(text)
    return len(set(names)) != len(names)


def _scan_structure(text: str):
    """Walk the first document's parse events, which the parser produces
    without recursion, before anything recursive sees it.

    :returns: the loader class to compose with: the pure-Python one when an
        anchor name is defined twice.
    :raises BackendError: nesting exceeds :data:`_MAX_NESTING`.
    """
    anchors_repeat = _anchors_may_repeat(text)
    if not anchors_repeat and _within_nesting_limit(text):
        return _LOADER
    depth = 0
    anchors = set()
    redefined = False
    track_anchors = anchors_repeat
    scanner = _LOADER(text)
    get_event = scanner.get_event
    try:
        while True:
            event = get_event()
            kind = type(event)
            step = _DEPTH_STEP.get(kind)
            if step is not None:
                depth += step
                if depth > _MAX_NESTING:
                    raise BackendError(_TOO_DEEP)
            elif kind is _DOCUMENT_END or event is None:
                break
            if track_anchors and kind in _ANCHORED:
                anchor = event.anchor
                if anchor is not None:
                    redefined = redefined or anchor in anchors
                    anchors.add(anchor)
    finally:
        scanner.dispose()
    return _PURE_LOADER if redefined else _LOADER


def safe_load(text: str):
    """Parse ``text`` the way ``Puppet::Util::Yaml.safe_load`` does:

    - a leading U+FEFF (BOM) is replaced with a single space (reproduces
      every probed Psych BOM outcome on both the C and pure loaders without
      touching the scanner itself);
    - only the first YAML document is read (``next(yaml.load_all(...))``);
    - ``None`` (an empty/comment-only/``~`` document) becomes ``False``
      (``util/yaml.rb:28-41``);
    - a parse error, a disallowed class, or an invalid ``!!float`` becomes a
      path-free, one-line :class:`BackendError` (raised outside the
      ``except`` block, so no ``__cause__``/``__context__`` holds PyYAML's
      own exception or a source snippet). Callers add the ``(<path>)``
      prefix once.
    """
    if text.startswith("\ufeff"):
        text = " " + text[1:]
    problem = None
    try:
        generator = yaml.load_all(text, _scan_structure(text))
        try:
            result = next(generator)
        except StopIteration:
            result = None
        finally:
            generator.close()
        return False if result is None else result
    except BackendError as e:
        problem = str(e)
    except yaml.YAMLError as e:
        problem = _yaml_problem(e)
    except RecursionError:
        problem = _TOO_DEEP
    except (ValueError, TypeError, AttributeError, ArithmeticError, LookupError) as e:
        problem = "unsupported YAML construct ({})".format(type(e).__name__)
    raise BackendError(problem)
