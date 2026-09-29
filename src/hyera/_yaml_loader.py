"""Ports Psych ``safe_load`` + ``ScalarScanner`` as ``Puppet::Util::Yaml`` uses
them, on top of PyYAML (``CSafeLoader`` when libyaml is available, else the
pure-Python ``SafeLoader``).

Ground truth: ``psych-5.3.1/lib/psych/scalar_scanner.rb`` (``tokenize``),
``psych-5.3.1/lib/psych/visitors/to_ruby.rb`` (``deserialize``), and
``puppet/util/yaml.rb``/``puppet/pops/lookup/hiera_config.rb`` for the
one-document, ``nil``-is-``false`` and ``symkeys_to_string`` rules.
"""

import base64
import re

import yaml

from .exceptions import BackendError

__all__ = ["RubySymbol", "symkeys_to_string", "safe_load"]


class RubySymbol:
    """A Ruby ``:symbol`` value (``!ruby/sym``/``!ruby/symbol``, or a plain
    ``:name`` scalar). Not a ``str`` subclass -- no string-typed code path
    should ever accept one by accident."""

    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        return isinstance(other, RubySymbol) and self.name == other.name

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((RubySymbol, self.name))

    def __repr__(self):
        return ":{}".format(self.name)


def symkeys_to_string(obj):
    """Recursively turn ``RubySymbol`` dict keys into their plain-string
    names (``hiera_config.rb``'s ``symkeys_to_string``, applied to a parsed
    hiera.yaml and, via v3/v4 config reading, to older configs too).
    Everything else -- including a ``RubySymbol`` *value* -- is unchanged.
    """
    if isinstance(obj, dict):
        return {
            (key.name if isinstance(key, RubySymbol) else key): symkeys_to_string(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [symkeys_to_string(item) for item in obj]
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
    return int(sign + body, 10)


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
            total += int(part) * (60 ** abs(index - 2))
        # Only the *first* part carries the sign (Ruby: `n.to_i`, where the
        # sign lives in the first segment's own text).
        return total
    if _SEXAGESIMAL_FLOAT_RE.match(string):
        total = 0.0
        parts = string.split(":")
        for index, part in enumerate(parts):
            total += float(part) * (60 ** abs(index - 2))
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
            raise BackendError('invalid value for Integer(): "{}"'.format(string))
    return string


# ---------------------------------------------------------------------------
# PyYAML loader/resolver/constructor wiring
# ---------------------------------------------------------------------------

_PLAIN_TAG = "tag:hyera.internal,2026:plain"

#: Tags this loader treats as merge keys are left to PyYAML's own default
#: resolution/`flatten_mapping` -- only the literal plain scalar `<<` needs
#: to keep resolving to the real merge tag instead of `_PLAIN_TAG`.
_MERGE_SCALAR = "<<"


class _PsychResolverMixin:
    """Retags every implicit (untagged, plain-style) scalar as
    :data:`_PLAIN_TAG`, so none of PyYAML's own bool/int/float/null/
    timestamp implicit resolvers (Python-flavored) ever fire -- the
    constructor for :data:`_PLAIN_TAG` calls :func:`_tokenize` instead,
    which is Ruby/Psych-flavored. The one exception is the literal `<<`
    scalar, which must keep resolving to the real merge tag or PyYAML's
    own `flatten_mapping` stops recognizing it as a merge key.
    """

    def resolve(self, kind, value, implicit):
        if kind is yaml.nodes.ScalarNode and implicit[0] and value != _MERGE_SCALAR:
            return _PLAIN_TAG
        return super().resolve(kind, value, implicit)


def _construct_plain(loader, node):
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
    text = loader.construct_scalar(node)
    # Non-validating, like Ruby's `String#unpack('m')` -- lenient base64,
    # matching `!!binary '%%%'` -> "" in the oracle probes.
    try:
        raw = base64.b64decode(text, validate=False)
    except Exception:
        raw = b""
    return raw.decode("utf-8", "surrogateescape")


def _construct_float(loader, node):
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
    result = {}
    for child in node.value:
        # Each child is a one-pair MappingNode: `{key: value}`.
        pairs = loader.construct_mapping(child, deep=True)
        for k, v in pairs.items():
            result[k] = v
    return result


def _construct_set_disallowed(loader, node):
    raise _disallowed("Psych::Set")


def _construct_sequence(loader, node):
    return loader.construct_sequence(node, deep=True)


def _construct_mapping(loader, node):
    return loader.construct_mapping(node, deep=True)


def _construct_unknown(loader, tag_suffix, node):
    if isinstance(node, yaml.nodes.ScalarNode):
        return _tokenize(loader.construct_scalar(node))
    if isinstance(node, yaml.nodes.SequenceNode):
        return _construct_sequence(loader, node)
    return _construct_mapping(loader, node)


def _flatten_mapping_keeping_dupes_last(loader, node):
    """``construct_mapping`` with ``flatten_mapping`` applied first (so
    ``<<`` merges are honored) and the last duplicate key winning."""
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        value = loader.construct_object(value_node, deep=True)
        key = _freeze_key(key)
        result[key] = value
    return result


def _freeze_key(key):
    """An unhashable key (list/dict) is frozen into a hashable tuple,
    recursively, so it can be used as a Python dict key at all (Ruby has no
    such restriction: any value can be a Hash key)."""
    if isinstance(key, list):
        return tuple(_freeze_key(v) for v in key)
    if isinstance(key, dict):
        return tuple((k, _freeze_key(v)) for k, v in key.items())
    return key


def _make_loader_class(base):
    class _Loader(_PsychResolverMixin, base):
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
    ):
        _Loader.add_constructor(prefix, _construct_sequence)
    for prefix in ("!map", "tag:yaml.org,2002:map"):
        _Loader.add_constructor(prefix, _construct_mapping)
    # Registration order matters (PyYAML tries multi-constructor *prefixes*
    # in insertion order, first match wins): the specific "!ruby/string"
    # prefix must be added before the broad "!ruby/" one, or every
    # `!ruby/string[:X]` scalar would be swallowed by the disallowed-class
    # handler instead of returning its text.
    _Loader.add_multi_constructor("!ruby/string", _construct_str_multi)
    _Loader.add_multi_constructor("!ruby/", _construct_ruby_disallowed)
    _Loader.add_multi_constructor(None, _construct_unknown)
    _Loader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        _flatten_mapping_keeping_dupes_last,
    )
    return _Loader


_C_LOADER = (
    _make_loader_class(yaml.CSafeLoader)
    if getattr(yaml, "__with_libyaml__", False)
    else None
)
_PURE_LOADER = _make_loader_class(yaml.SafeLoader)
_LOADER = _C_LOADER or _PURE_LOADER


#: Matches a PyYAML error's own quoted token, e.g. the alias/tag/anchor name
#: in "found undefined alias 'NAME'" or "found duplicate anchor 'NAME'".
#: ``_yaml_problem`` never echoes decrypted data, but three ``problem``/
#: ``context`` texts (undefined alias, unknown tag, duplicate anchor) quote a
#: single scalar from the source verbatim -- redact it rather than trusting
#: PyYAML's own message templates to never do this.
_YAML_QUOTED_TOKEN_RE = re.compile(r"'[^']*'")


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
            text = _YAML_QUOTED_TOKEN_RE.sub("'<redacted>'", text)
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
        generator = yaml.load_all(text, _LOADER)
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
    raise BackendError(problem)
