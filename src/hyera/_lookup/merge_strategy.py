# Ported from Puppet 8 lib/puppet/pops/merge_strategy.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
# Ported from the deep_merge gem 1.2.2 lib/deep_merge/core.rb
# (https://github.com/danielsdeleo/deep_merge), MIT. Modified by jose-pr.
# See NOTICE.
"""Merge strategies for accumulating values across the hierarchy.

Ports Puppet's ``pops/merge_strategy.rb`` and the deep_merge gem's
``deep_merge_core.rb``.
"""

import collections.abc
import contextlib
import functools
import re
import typing as _ty

from .interpolation import _ruby_inspect, unshare
from .navigation import _MISSING
from ..exceptions import MergeError
from .._enums import _StrEnum, _plain

__all__ = ["Merge", "MergeSpec"]


class Merge(_StrEnum):
    """A merge strategy name: the ``merge=`` argument of
    :meth:`~hyera.Hiera.lookup`/:meth:`~hyera.Hiera.dig`/
    :meth:`~hyera.Hiera.get`/:meth:`~hyera.Hiera.explain`, and the
    ``"strategy"`` key of a :data:`MergeSpec` mapping.

    These are Puppet's own four public strategies
    (``MergeStrategy.strategy_keys()``); ``default`` (``first``'s hidden
    alias used when no strategy is given at all), ``unconstrained_deep``
    and ``reverse_deep`` are real strategies Puppet itself never exposes as
    a choice, so they stay out of this enum too.
    """

    FIRST = "first"
    """The first hierarchy level with a value wins; nothing is merged."""

    UNIQUE = "unique"
    """Flatten every found value into one list, keeping each item once."""

    HASH = "hash"
    """Shallow-merge every found ``Hash``; a higher-priority level's keys win."""

    DEEP = "deep"
    """Recursively merge every found ``Hash``/``Array`` (Puppet's
    ``deep_merge`` gem, with ``knockout_prefix``/``sort_merged_arrays``/
    ``merge_hash_arrays`` as extra ``MergeSpec`` mapping keys)."""


#: The type of every public ``merge=`` argument: a strategy name or member
#: (``Merge.DEEP``/``"deep"``/...), a ``{"strategy": ..., ...}`` mapping
#: with Puppet's deep-merge options, or ``None`` for the level's own default.
MergeSpec = _ty.Union[Merge, str, _ty.Mapping[str, _ty.Any], None]

#: The shared no-op context manager :meth:`MergeStrategy.lookup` uses when
#: called with no ``invocation`` at all.
_NULL_CTX = contextlib.nullcontext()

#: Registered strategy name -> MergeStrategy subclass, in definition order.
_STRATEGIES: dict = {}


def _rb_truthy(value):
    """Ruby truthiness: only ``nil`` (``None``) and ``false`` are falsy."""
    return value is not None and value is not False


def _eql_key(value):
    """A recursive, hashable key matching Ruby's ``eql?``.

    ``eql?`` never coerces across type: ``1``, ``1.0`` and ``true`` are three
    distinct values, ``0`` and ``false`` two. ``bool`` is tested before
    ``int`` since ``bool`` subclasses ``int`` in Python.
    """
    if value is None:
        return ("nil",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, list):
        return ("list", tuple(_eql_key(item) for item in value))
    if isinstance(value, dict):
        return (
            "dict",
            frozenset((_eql_key(k), _eql_key(v)) for k, v in value.items()),
        )
    return ("id", id(value))


def _ruby_or(a, b):
    """``Array#|`` -- union of ``a`` then ``b``, first occurrence per ``eql?`` wins."""
    seen: set = set()
    out: list = []
    for item in list(a) + list(b):
        key = _eql_key(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _ruby_uniq(a):
    """``Array#uniq`` -- equivalent to ``a | []``."""
    return _ruby_or(a, [])


def _ruby_flatten(a):
    """``Array#flatten`` -- recursive, returns a new list."""
    out: list = []
    for item in a:
        if isinstance(item, list):
            out.extend(_ruby_flatten(item))
        else:
            out.append(item)
    return out


def _is_data(value):
    """Puppet ``Data``: Undef/Scalar, or an Array/Hash of Data (String keys)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, list):
        return all(_is_data(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_data(v) for k, v in value.items())
    return False


def _puppet_type_name(value):
    """The generalized inferred Puppet type name used in mismatch messages."""
    if value is None:
        return "Undef"
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "String"
    if isinstance(value, list):
        return "Tuple" if value else "Array"
    if isinstance(value, dict):
        if value and all(isinstance(k, str) for k in value):
            return "Struct"
        return "Hash"
    return type(value).__name__


def _ruby_to_s(value):
    """Ruby ``to_s``: a String returns itself; everything else is ``inspect``."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return _ruby_inspect(value)


class MergeStrategy:
    """Base for the ported Puppet merge strategies (``pops/merge_strategy.rb``).

    Subclasses register themselves in ``_STRATEGIES`` by declaring their own
    ``KEY``; ``__init_subclass__`` also builds each one's no-options
    ``INSTANCE`` singleton.
    """

    KEY = None
    INSTANCE = None
    #: ``True`` when a reduce simply returns the first found variant, so a
    #: caller may run that loop itself and save a stack frame per level.
    first_found = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "KEY" in cls.__dict__:
            _STRATEGIES[cls.KEY] = cls
            cls.INSTANCE = cls({})

    @classmethod
    def strategy(cls, merge):
        """``MergeStrategy.strategy`` -- merge_strategy.rb:22-43."""
        if not _rb_truthy(merge):
            return DefaultMergeStrategy.INSTANCE
        if isinstance(merge, MergeStrategy):
            return merge
        if isinstance(merge, collections.abc.Mapping):
            name = _plain(merge.get("strategy"))
            if name is None:
                raise MergeError(
                    "The hash given as 'merge' must contain the name of a "
                    "strategy in string form for the key 'strategy'"
                )
            options = {} if len(merge) == 1 else dict(merge)
            if "strategy" in options:
                # A Merge member is never stored downstream -- normalize
                # even though merge_strategy.rb never reads this key back
                # out of options itself.
                options["strategy"] = name
        else:
            name = _plain(merge)
            options = {}
        found = _STRATEGIES.get(name) if isinstance(name, str) else None
        if found is None:
            raise MergeError("Unknown merge strategy: '{}'".format(_ruby_to_s(name)))
        if not options:
            return found.INSTANCE
        return found(options)

    @staticmethod
    def strategy_keys():
        """merge_strategy.rb:49-51 -- the CLI-facing ``--merge`` choices."""
        hidden = ("default", "unconstrained_deep", "reverse_deep")
        return tuple(key for key in _STRATEGIES if key not in hidden)

    def __init__(self, options):
        """merge_strategy.rb:83-86."""
        if options:
            problems = self._options_problems(options)
            if problems:
                raise MergeError(
                    "The merge options has wrong type, " + "; ".join(problems)
                )
        self.options = dict(options)

    @classmethod
    def _options_problems(cls, options):
        """merge_strategy.rb:186-188 -- the base accepts only ``strategy``."""
        return ["unrecognized key '{}'".format(k) for k in options if k != "strategy"]

    def lookup(self, variants, fn, invocation=None):
        """merge_strategy.rb:126-151.

        A value that actually enters a merge (two or more contributing
        variants) is passed through :func:`~hyera._lookup.interpolation.unshare`
        first -- :func:`~hyera._lookup.interpolation.interpolate` may return a
        value where two positions are the *same* object (a reused YAML
        anchor), and this strategy's own ``merge``/``convert_value`` mutate
        their higher-priority accumulator in place; unsharing first is what
        keeps that in-place mutation from corrupting an unrelated position
        that happens to share the same node. A lone (never-merged) value is
        returned exactly as found, sharing included -- nothing here ever
        mutates it.

        With ``invocation`` given: zero variants report nothing (a plain
        miss); exactly one runs with no ``merge`` node at all (Puppet's own
        ``merge_single`` never explains); two or more run under
        ``recording("merge", self)``, with ``report_result`` when a value
        was actually found (never on an all-missing reduce).
        """
        variants = list(variants)
        if not variants:
            return _MISSING
        if len(variants) == 1:
            result = fn(variants[0])
            if result is _MISSING:
                return _MISSING
            return self.merge_single(result)
        with (
            invocation.recording("merge", self) if invocation is not None else _NULL_CTX
        ):
            memo = _MISSING
            for variant in variants:
                value = fn(variant)
                if value is _MISSING:
                    continue
                value = unshare(value)
                if memo is _MISSING:
                    memo = self.convert_value(value)
                else:
                    memo = self._merge_owned(memo, value)
            if invocation is not None and memo is not _MISSING:
                invocation.report_result(memo)
            return memo

    def merge(self, e1, e2):
        """merge_strategy.rb:95-100."""
        self._assert("The first element of the merge", e1)
        self._assert("The second element of the merge", e2)
        return self.checked_merge(e1, e2)

    def _merge_owned(self, e1, e2):
        """:meth:`merge` for two values the caller owns outright, so a
        strategy that clones its lower-priority side may skip the clone."""
        return self.merge(e1, e2)

    def _assert(self, label, value):
        problem = self._value_problem(value)
        if problem is not None:
            raise MergeError("{} has wrong type, {}".format(label, problem))

    def convert_value(self, value):
        """merge_strategy.rb:156-165 -- identity in the base."""
        return value

    def merge_single(self, value):
        """merge_strategy.rb:156-165 -- identity in the base."""
        return value

    def checked_merge(self, e1, e2):
        raise NotImplementedError

    def _value_problem(self, value):
        raise NotImplementedError


class FirstFoundStrategy(MergeStrategy):
    """merge_strategy.rb:210-237 -- the first non-missing variant wins."""

    KEY = "first"
    first_found = True

    def lookup(self, variants, fn, invocation=None):
        """merge_strategy.rb:223-228 -- stop at the first found; never
        merge. ``invocation`` is accepted and ignored: a first-found
        reduce never records a merge node of its own (Puppet's own
        ``FirstFoundStrategy`` never calls ``with``)."""
        for variant in variants:
            value = fn(variant)
            if value is not _MISSING:
                return value
        return _MISSING

    def _value_problem(self, value):
        return None


class DefaultMergeStrategy(FirstFoundStrategy):
    """merge_strategy.rb:240-248 -- ``first``'s hidden alias, used when no
    merge strategy was given at all."""

    KEY = "default"


class HashMergeStrategy(MergeStrategy):
    """merge_strategy.rb:250-272 -- shallow hash merge, higher level wins."""

    KEY = "hash"

    def checked_merge(self, e1, e2):
        """merge_strategy.rb:264-266 -- ``e2.merge(e1)``: e2's key order,
        e1's (higher-priority) values win and its new keys append."""
        result = dict(e2)
        result.update(e1)
        return result

    def _value_problem(self, value):
        """merge_strategy.rb:270-272 -- ``Hash[String, Data]``."""
        if not isinstance(value, dict):
            return "expects a Hash value, got {}".format(_puppet_type_name(value))
        for key, val in value.items():
            if not isinstance(key, str):
                # Puppet's own message names the offending key/value types
                # (e.g. "Hash[Integer[1, 1], String]"); this subset reports
                # the simple "Hash" only.
                return "expects a Hash[String, Data] value, got Hash"
            if not _is_data(val):
                return "entry '{}' expects a Data value, got {}".format(
                    key, _puppet_type_name(val)
                )
        return None


class UniqueMergeStrategy(MergeStrategy):
    """merge_strategy.rb:280-314 -- flatten, then union across levels."""

    KEY = "unique"

    def checked_merge(self, e1, e2):
        """merge_strategy.rb:294-296."""
        return _ruby_or(self.convert_value(e1), self.convert_value(e2))

    def convert_value(self, value):
        """merge_strategy.rb:298-300 -- flatten a list; wrap a scalar."""
        if isinstance(value, list):
            return _ruby_flatten(value)
        return [value]

    def merge_single(self, value):
        """merge_strategy.rb:306-308 -- uniq applies only to one found array."""
        if isinstance(value, list):
            return _ruby_uniq(value)
        return value

    def _value_problem(self, value):
        """merge_strategy.rb:312-314 -- ``Variant[Scalar, Array[Data]]``."""
        if isinstance(value, (str, int, float, bool)):
            return None
        if isinstance(value, list):
            for item in value:
                if not _is_data(item):
                    # Puppet reports each offending element over several
                    # "variant N" lines; byte-exact prose is out of scope
                    # for this nested case.
                    return (
                        "expects a value of type Scalar or Array, "
                        "got Array[{}]".format(_puppet_type_name(item))
                    )
            return None
        return "expects a value of type Scalar or Array, got {}".format(
            _puppet_type_name(value)
        )


# ---------------------------------------------------------------------------
# deep_merge! port (rubygem-deep_merge 1.2.2, ``deep_merge_core.rb``).
# ---------------------------------------------------------------------------

#: Compiled knockout-prefix pattern, cached per prefix string.
_KO_PATTERN_CACHE: dict = {}


def _ruby_class_name(value):
    """The Ruby class name shown in a ``<=>`` comparison-failure message."""
    if value is None:
        return "NilClass"
    if isinstance(value, bool):
        return "TrueClass" if value else "FalseClass"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, str):
        return "String"
    if isinstance(value, list):
        return "Array"
    if isinstance(value, dict):
        return "Hash"
    return type(value).__name__


def _ruby_eq(a, b):
    """Ruby ``==``: numbers compare across int/float, but a Boolean never
    equals a number (``1 == true`` is false; ``1 == 1.0`` is true)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if a is None or b is None:
        return a is b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_ruby_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_ruby_eq(a[k], b[k]) for k in a)
    return False


def _ruby_dup(value):
    """Ruby ``dup`` -- shallow; a scalar (or anything not a dict/list) is
    returned as-is, since Ruby's own ``dup`` is a no-op for those here."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def _clear_or_nil(obj):
    """deep_merge_core.rb:238-245 -- ``clear_or_nil``."""
    if isinstance(obj, (list, dict)):
        obj.clear()
        return obj
    if isinstance(obj, str):
        return ""
    return None


def _ruby_index(lst, obj):
    """``Array#index`` -- the first index where an element is ``==`` obj,
    else ``None`` (index ``0`` is a hit: callers must test ``is not None``)."""
    for i, item in enumerate(lst):
        if _ruby_eq(item, obj):
            return i
    return None


def _ruby_delete(lst, obj):
    """``Array#delete`` -- removes every ``==``-equal element, in place."""
    write = 0
    length = len(lst)
    for read in range(length):
        item = lst[read]
        if not _ruby_eq(item, obj):
            lst[write] = item
            write += 1
    del lst[write:]
    return lst


def _ruby_delete_if(lst, pred):
    """``Array#delete_if`` -- in place, walking the **live** length with a
    read index and a write index, then truncating to the kept items.

    ``pred`` may itself mutate ``lst`` (the knockout callback below does,
    when source and dest alias through a shallow ``dup``): each read of
    ``lst[i]`` and of ``len(lst)`` happens fresh, so a mutation mid-walk can
    make the walk see fewer elements than it started with, and skip some --
    deliberately reproduced (Ruby's own ``ary_reject_bang``), not a bug.
    """
    i = 0
    j = 0
    while i < len(lst):
        v = lst[i]
        if pred(v):
            i += 1
        else:
            if i != j:
                lst[j] = lst[i]
            j += 1
            i += 1
    del lst[j:]
    return lst


def _ko_pattern(prefix):
    """``%r{^#{prefix}}`` -- the prefix is spliced UNESCAPED into the regex
    (Puppet lets a prefix double as a regex, e.g. ``.`` or ``x+``); ``^``
    with Ruby's default (always-multiline) semantics anchors every line."""
    cached = _KO_PATTERN_CACHE.get(prefix)
    if cached is not None:
        return cached
    try:
        compiled = re.compile("^" + prefix, re.MULTILINE)
    except re.error as exc:
        raise MergeError(
            "knockout_prefix '{}' is not a valid regular expression".format(prefix)
        ) from exc
    _KO_PATTERN_CACHE[prefix] = compiled
    return compiled


def _overwrite_unmergeables(source, dest, options):
    """deep_merge_core.rb:212-236 -- ``overwrite_unmergeables``.

    With ``preserve_unmergeables`` true (``overwrite_unmergeable`` false)
    and no knockout prefix, neither branch below fires and ``dest`` is
    returned unchanged -- the caller keeps its own (unmergeable) value.
    """
    overwrite_unmergeable = not _rb_truthy(options.get("preserve_unmergeables"))
    raw_prefix = options.get("knockout_prefix")
    knockout_prefix = raw_prefix if _rb_truthy(raw_prefix) else None
    if knockout_prefix is not None and overwrite_unmergeable:
        if isinstance(source, str):
            pattern = _ko_pattern(knockout_prefix)
            stripped = pattern.sub("", source)
            return stripped if stripped == source else ""
        if isinstance(source, list):
            pattern = _ko_pattern(knockout_prefix)
            _ruby_delete_if(
                source,
                lambda item: isinstance(item, str) and pattern.search(item) is not None,
            )
            return source
        return source
    if overwrite_unmergeable:
        return source
    return dest


def _ruby_join(value, sep):
    """``Array#join(sep)`` -- ``to_s`` per element, recursing into a nested
    list; a scalar (used when ``value`` is not itself a list) is just
    ``to_s``'d."""
    if isinstance(value, list):
        return sep.join(_ruby_join(item, sep) for item in value)
    return _ruby_to_s(value)


def _ruby_split(text, sep):
    """``String#split(sep)`` -- drops trailing empty fields; a single-space
    ``sep`` splits on whitespace runs and drops leading empties too
    (Ruby's documented special case); an empty string splits to ``[]``."""
    if text == "":
        return []
    if sep == " ":
        return text.split()
    parts = text.split(sep)
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def _ruby_cmp(a, b):
    """Ruby ``<=>`` -- ``-1``/``0``/``1``, or ``None`` when incomparable."""
    if isinstance(a, bool) or isinstance(b, bool):
        return 0 if _ruby_eq(a, b) else None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (a > b) - (a < b)
    if isinstance(a, str) and isinstance(b, str):
        return (a > b) - (a < b)
    if isinstance(a, list) and isinstance(b, list):
        for x, y in zip(a, b):
            c = _ruby_cmp(x, y)
            if c is None:
                return None
            if c != 0:
                return c
        return (len(a) > len(b)) - (len(a) < len(b))
    return 0 if _ruby_eq(a, b) else None


def _ruby_cmperr(x, y):
    """The ``ArgumentError`` text Ruby's ``Array#sort!`` raises for an
    incomparable pair (``x``, then the class or literal of ``y``)."""
    if (
        y is None
        or isinstance(y, (bool, float))
        or (isinstance(y, int) and -(2**62) <= y < 2**62)
    ):
        y_repr = _ruby_inspect(y)
    else:
        y_repr = _ruby_class_name(y)
    return "comparison of {} with {} failed".format(_ruby_class_name(x), y_repr)


def _ruby_sort(lst):
    """``Array#sort!`` via ``<=>``; the first incomparable pair raises,
    named in their ORIGINAL index order (not whatever order
    ``cmp_to_key`` happens to probe them in)."""

    def compare(a, b):
        cmp = _ruby_cmp(a[1], b[1])
        if cmp is None:
            if a[0] < b[0]:
                raise MergeError(_ruby_cmperr(a[1], b[1]))
            raise MergeError(_ruby_cmperr(b[1], a[1]))
        return cmp

    if all(type(item) is str for item in lst) or all(type(item) is int for item in lst):
        return sorted(lst)
    indexed = list(enumerate(lst))
    indexed.sort(key=functools.cmp_to_key(compare))
    return [item for _, item in indexed]


def deep_merge(source, dest, options):
    """``deep_merge!`` -- deep_merge_core.rb:78-209.

    ``source`` (higher priority) is merged onto ``dest`` (lower); ``dest`` is
    mutated in place when it is a dict or list. Reproduces Ruby's aliasing
    deliberately: a nested list can be the SAME object as both ``source``
    and ``dest`` in a recursive call (via ``_ruby_dup``'s shallow copy), and
    the knockout-prefix step below relies on that when it happens.

    Puppet's ``deep`` strategy only ever passes ``knockout_prefix``,
    ``sort_merged_arrays``, ``merge_hash_arrays``, ``merge_debug`` (read,
    prints nothing) and a hardcoded false ``preserve_unmergeables``.
    ``unconstrained_deep``/``reverse_deep`` may also pass
    ``preserve_unmergeables``, ``overwrite_arrays``, ``unpack_arrays``,
    ``extend_existing_arrays``, ``keep_array_duplicates`` and
    ``merge_nil_values`` -- read here unconditionally (Puppet's own
    ``deep_merge!`` does too; nothing guards them by strategy, only by
    which options happen to be present).
    """
    overwrite_unmergeable = not _rb_truthy(options.get("preserve_unmergeables"))
    raw_prefix = options.get("knockout_prefix")
    knockout_prefix = raw_prefix if _rb_truthy(raw_prefix) else None
    if knockout_prefix == "":
        # deep_merge_core.rb:83.
        raise MergeError("knockout_prefix cannot be an empty string in deep_merge!")
    if knockout_prefix is not None and not overwrite_unmergeable:
        # deep_merge_core.rb:84.
        raise MergeError(
            "overwrite_unmergeable must be true if knockout_prefix is "
            "specified in deep_merge!"
        )
    array_split_char = options.get("unpack_arrays")
    if not _rb_truthy(array_split_char):
        array_split_char = None
    overwrite_arrays = _rb_truthy(options.get("overwrite_arrays"))
    sort_merged_arrays = _rb_truthy(options.get("sort_merged_arrays"))
    merge_hash_arrays = _rb_truthy(options.get("merge_hash_arrays"))
    extend_existing_arrays = _rb_truthy(options.get("extend_existing_arrays"))
    keep_array_duplicates = _rb_truthy(options.get("keep_array_duplicates"))
    merge_nil_values = _rb_truthy(options.get("merge_nil_values"))

    # deep_merge_core.rb:102.
    if source is None and not merge_nil_values:
        return dest
    # deep_merge_core.rb:104-106.
    if not _rb_truthy(dest) and overwrite_unmergeable:
        return source

    if isinstance(source, dict):
        # deep_merge_core.rb:109-140.
        for src_key, src_value in list(source.items()):
            if isinstance(dest, dict):
                dest_value = dest.get(src_key)
                if _rb_truthy(dest_value):
                    # :114-116.
                    dest[src_key] = deep_merge(src_value, dest_value, options)
                else:
                    # :117-130 -- dest doesn't have this key (or it's
                    # falsy): merge src_value with its own shallow dup. A
                    # nested container can end up aliased between source
                    # and dest one level down because of this.
                    src_dup = _ruby_dup(src_value)
                    if isinstance(src_dup, list) and keep_array_duplicates:
                        # :127-129 -- the merge is additive (concat, not a
                        # bounded union) below, so start empty instead of
                        # merging src_value with itself.
                        src_dup = []
                    dest[src_key] = deep_merge(src_value, src_dup, options)
            elif isinstance(dest, list) and extend_existing_arrays:
                # :132-133 -- pushes the whole (Hash) ``source``, once per
                # source key (Ruby re-evaluates this every iteration; dest
                # stays an Array so it never reaches a fixed point the way
                # the other two branches do -- faithfully reproduced, not
                # a bug to "optimize" into a single push).
                dest.append(source)
            else:
                # :134-138 -- dest isn't a Hash (or Array to extend): the
                # whole value is overwritten by source. Every later key
                # sees the new dest (the source Hash itself) and merges
                # its own value with itself, which dedupes arrays and
                # applies the knockout prefix there.
                dest = _overwrite_unmergeables(source, dest, options)
        return dest

    if isinstance(source, list):
        # deep_merge_core.rb:141-198.
        if overwrite_arrays:
            # :143-145.
            return source
        if array_split_char is not None:
            # :148-154 -- join then split source (and dest, if it is also
            # an Array) on the same separator before anything else.
            source = _ruby_split(_ruby_join(source, array_split_char), array_split_char)
            if isinstance(dest, list):
                dest = _ruby_split(_ruby_join(dest, array_split_char), array_split_char)
        if (
            knockout_prefix is not None
            and _ruby_index(source, knockout_prefix) is not None
        ):
            # :156-158 -- a naked prefix element truncates dest outright.
            dest = _clear_or_nil(dest)
            _ruby_delete(source, knockout_prefix)
        if isinstance(dest, list):
            if knockout_prefix is not None:
                # :159-175 -- for each prefixed source item, strip the
                # prefix and remove BOTH the stripped and the original
                # (still-prefixed) form from dest; the prefixed item itself
                # is then dropped from source. Iterating and mutating
                # through the SAME live array (when source and dest alias)
                # is what produces the "shares nested objects" quirk.
                pattern = _ko_pattern(knockout_prefix)

                def _knockout(ko_item, _dest=dest, _pattern=pattern):
                    item = (
                        _pattern.sub("", ko_item)
                        if isinstance(ko_item, str)
                        else ko_item
                    )
                    if not _ruby_eq(item, ko_item):
                        _ruby_delete(_dest, item)
                        _ruby_delete(_dest, ko_item)
                        return True
                    return False

                _ruby_delete_if(source, _knockout)
            source_all_hashes = all(isinstance(i, dict) for i in source)
            dest_all_hashes = all(isinstance(i, dict) for i in dest)
            if merge_hash_arrays and source_all_hashes and dest_all_hashes:
                # :177-187.
                merged = []
                for i in range(len(dest)):
                    s = source[i] if i < len(source) else {}
                    merged.append(deep_merge(s, dest[i], options))
                if len(source) > len(dest):
                    merged.extend(source[len(dest) :])
                dest = merged
            elif keep_array_duplicates:
                # :188-189 -- ``concat``: every element of source, in
                # order, duplicates included.
                dest = list(dest) + list(source)
            else:
                # :190-191 -- ``dest | source``: dest's elements come
                # first, source's new ones are appended, both deduped.
                dest = _ruby_or(dest, source)
            if sort_merged_arrays:
                # :193.
                dest = _ruby_sort(dest)
            return dest
        # :194-197.
        return _overwrite_unmergeables(source, dest, options)

    # :199-206 -- any other (scalar, or nil with merge_nil_values) source.
    if isinstance(dest, list) and extend_existing_arrays:
        dest.append(source)
        return dest
    return _overwrite_unmergeables(source, dest, options)


class DeepMergeStrategy(MergeStrategy):
    """merge_strategy.rb:350-412 -- recursive merge via ``deep_merge!``."""

    KEY = "deep"

    def checked_merge(self, e1, e2, *, clone=True):
        """merge_strategy.rb:372-377 -- ``deep_merge!(e1, deep_clone(e2))``.

        ``preserve_unmergeables`` defaults false -- ``deep`` itself can
        never set it (``_options_problems`` below rejects the key), so this
        only ever matters for a subclass (``unconstrained_deep``/
        ``reverse_deep``) whose own options may set it explicitly.
        """
        merge_options = {k: v for k, v in self.options.items() if k != "strategy"}
        merge_options.setdefault("preserve_unmergeables", False)
        return deep_merge(e1, unshare(e2) if clone else e2, merge_options)

    def _merge_owned(self, e1, e2):
        return self.checked_merge(e1, e2, clone=False)

    def _value_problem(self, value):
        """merge_strategy.rb:410-412 -- ``Any``: never a problem."""
        return None

    @classmethod
    def _options_problems(cls, options):
        """merge_strategy.rb:399-407."""
        problems = []
        bool_keys = ("merge_debug", "merge_hash_arrays", "sort_merged_arrays")
        for key, value in options.items():
            if key == "strategy":
                continue
            if key == "knockout_prefix":
                if value is not None and not isinstance(value, str):
                    problems.append(
                        "entry 'knockout_prefix' expects a value of type "
                        "Undef or String, got {}".format(_puppet_type_name(value))
                    )
            elif key in bool_keys:
                if value is not None and not isinstance(value, bool):
                    problems.append(
                        "entry '{}' expects a value of type Undef or Boolean, "
                        "got {}".format(key, _puppet_type_name(value))
                    )
            else:
                problems.append("unrecognized key '{}'".format(key))
        return problems


class UnconstrainedDeepMergeStrategy(DeepMergeStrategy):
    """merge_strategy.rb:419-430 -- ``deep``, but accepting any of
    deep_merge's options (``preserve_unmergeables``, ``overwrite_arrays``,
    ``unpack_arrays``, ``extend_existing_arrays``, ``keep_array_duplicates``,
    ``merge_nil_values``, on top of ``deep``'s own four) under any
    non-empty string key -- unrecognized keys pass through to
    :func:`deep_merge`, which simply ignores whatever it doesn't read.
    With no options at all, ``INSTANCE`` behaves exactly like ``deep``'s
    own (inherited ``checked_merge``/``_value_problem``)."""

    KEY = "unconstrained_deep"

    @classmethod
    def _options_problems(cls, options):
        """merge_strategy.rb:426-428 -- ``Hash[String[1], Any]``: one
        problem for the whole options hash on the first bad key, not one
        per key (Puppet reports the hash type mismatch once)."""
        for key in options:
            if key == "strategy":
                continue
            if not isinstance(key, str) or key == "":
                return [
                    "expects a Hash[String[1], Any] value, got Hash",
                ]
        return []


class ReverseDeepMergeStrategy(UnconstrainedDeepMergeStrategy):
    """merge_strategy.rb:434-446 -- ``unconstrained_deep`` with the two
    sides swapped: the lower-priority value is merged as deep_merge's
    ``source`` (so it wins ties/collisions), the higher-priority one is
    what gets cloned into ``dest``."""

    KEY = "reverse_deep"

    def checked_merge(self, e1, e2, *, clone=True):
        """merge_strategy.rb:442-444."""
        return super().checked_merge(e2, e1, clone=clone)
