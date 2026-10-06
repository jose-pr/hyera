# Ported from the deep_merge gem 1.2.2 lib/deep_merge/core.rb
# (https://github.com/danielsdeleo/deep_merge), MIT. Modified by jose-pr.
# See NOTICE.
"""The deep_merge gem's ``deep_merge!``, ported over Python values.

Ports ``deep_merge_core.rb``, with the Ruby value helpers (``eql?`` keys,
truthiness, ``Array#|``, ``to_s``) it and the merge strategies share.
"""

from __future__ import annotations

import functools
import re

from ..exceptions import MergeError
from .interpolation import _ruby_inspect


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


def _rb_truthy(value):
    """Ruby truthiness: only ``nil`` (``None``) and ``false`` are falsy."""
    return value is not None and value is not False


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


def _ruby_to_s(value):
    """Ruby ``to_s``: a String returns itself; everything else is ``inspect``."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return _ruby_inspect(value)


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
                    # :117-130 -- dest lacks this key (or it is falsy): merge src_value with its own shallow
                    # dup, so a nested container can end up aliased between source and dest one level down.
                    src_dup = _ruby_dup(src_value)
                    if isinstance(src_dup, list) and keep_array_duplicates:
                        # :127-129 -- the merge is additive (concat, not a
                        # bounded union) below, so start empty instead of
                        # merging src_value with itself.
                        src_dup = []
                    dest[src_key] = deep_merge(src_value, src_dup, options)
            elif isinstance(dest, list) and extend_existing_arrays:
                # :132-133 -- pushes the whole (Hash) ``source`` once per source key: dest stays an Array
                # and never reaches a fixed point, as in Ruby; reproduced, not optimized to one push.
                dest.append(source)
            else:
                # :134-138 -- dest is not a Hash (or Array to extend): source overwrites it, and each later
                # key merges its own value with itself, which dedupes arrays and applies the knockout prefix.
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
                # :159-175 -- for each prefixed source item, strip the prefix, remove both the stripped and
                # original forms from dest, then drop the item from source. Mutating one live array that
                # source and dest alias is what produces the "shares nested objects" quirk.
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
