# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Merge strategies for accumulating values across the hierarchy.

Ports Puppet's ``pops/merge_strategy.rb`` and ``deep_merge_core``.
"""

import json
from copy import deepcopy

from ._navigation import _MISSING
from .exceptions import MergeError

#: Strategy names that build a Merge; also mapped from the legacy type API.
_MERGE_STRATEGIES = {"first", "unique", "hash", "deep"}

#: Legacy type-based merge= values -> strategy name.
_TYPE_TO_STRATEGY = {list: "unique", set: "unique", dict: "hash"}


def make_merge(spec):
    """Build a :class:`Merge` from a caller ``merge=`` spec, or ``None``.

    Accepts a strategy name (``"first"``/``"unique"``/``"hash"``/``"deep"``),
    a legacy type (``list``/``set``/``dict``), or a hash
    ``{"strategy": "deep", "knockout_prefix": "--", ...}``. ``None``/``"first"``
    yields ``None`` (first-match-wins, no accumulation).
    """
    if spec is None:
        return None
    options: dict = {}
    if isinstance(spec, dict):
        options = dict(spec)
        strategy = options.pop("strategy", None) or options.pop("merge", None)
    elif isinstance(spec, type):
        strategy = _TYPE_TO_STRATEGY.get(spec)
        if strategy is None:
            raise MergeError("Unsupported merge type: {!r}".format(spec))
    else:
        strategy = spec
    if strategy in (None, "first"):
        return None
    if not isinstance(strategy, str) or strategy not in _MERGE_STRATEGIES:
        raise MergeError("Unknown merge strategy: {!r}".format(strategy))
    return Merge(strategy, **options)


class Merge:
    """Accumulates matches across the hierarchy per a merge strategy.

    Strategies: ``unique`` (flatten scalars+arrays, dedupe, first-seen order),
    ``hash`` (shallow, higher priority wins per key), ``deep`` (recursive, with
    ``knockout_prefix`` / ``sort_merged_arrays`` / ``merge_hash_arrays``).
    """

    def __init__(
        self,
        strategy,
        knockout_prefix=None,
        sort_merged_arrays=False,
        merge_hash_arrays=False,
        **_ignored,
    ):
        self.strategy = strategy
        self.knockout_prefix = knockout_prefix
        self.sort_merged_arrays = sort_merged_arrays
        self.merge_hash_arrays = merge_hash_arrays

        if strategy == "unique":
            self.value = []
        elif strategy == "hash":
            self.value = {}
        elif strategy == "deep":
            # Deep values may be dicts OR lists; let the first match set the
            # type rather than presuming a dict.
            self.value = _MISSING
        else:
            self.value = None

    def merge_value(self, value):
        if self.strategy == "unique":
            if isinstance(value, (list, tuple, set)):
                self.value += list(value)
            else:
                self.value.append(value)
        elif self.strategy == "hash":
            if isinstance(value, dict):
                for k, v in value.items():
                    if k not in self.value:  # higher priority (earlier) wins
                        self.value[k] = v
        elif self.strategy == "deep":
            self.value = self.deep_merge(self.value, value)
        else:  # pragma: no cover - first never accumulates
            self.value = value

    def finalize(self):
        """Return the accumulated value, applying post-merge normalization."""
        if self.strategy == "unique":
            seen: list = []
            for item in self.value:
                if item not in seen:
                    seen.append(item)
            if self.sort_merged_arrays:
                try:
                    seen.sort()
                except TypeError:
                    pass
            return seen
        if self.strategy == "deep":
            value = self._knockout(self.value)
            if self.sort_merged_arrays:
                value = self._sort_arrays(value)
            return value
        return self.value

    def _sort_arrays(self, obj):
        """Recursively sort every list in a merged structure, best-effort.

        ``sort_merged_arrays`` is a deep-merge option in Puppet, so it has to
        reach lists nested anywhere in the result, not just a top-level one.
        Heterogeneous lists have no total order in Python 3; those are left
        in merge order rather than failing the whole lookup.
        """
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                out[k] = self._sort_arrays(v)
            return out
        if isinstance(obj, list):
            items = [self._sort_arrays(item) for item in obj]
            try:
                items.sort()
            except TypeError:
                pass
            return items
        return obj

    def _knockout(self, obj):
        """Apply knockout_prefix removals to a merged structure."""
        prefix = self.knockout_prefix
        if not prefix:
            return obj
        if isinstance(obj, dict):
            removed = {
                k[len(prefix) :]
                for k in obj
                if isinstance(k, str) and k.startswith(prefix)
            }
            out = {}
            for k, v in obj.items():
                if isinstance(k, str) and k.startswith(prefix):
                    continue
                if k in removed:
                    continue
                out[k] = self._knockout(v)
            return out
        if isinstance(obj, list):
            drop = {
                item[len(prefix) :]
                for item in obj
                if isinstance(item, str) and item.startswith(prefix)
            }
            return [
                self._knockout(item)
                for item in obj
                if not (isinstance(item, str) and item.startswith(prefix))
                and item not in drop
            ]
        return obj

    def deep_merge(self, a, b):
        """Recursively merge ``b`` (lower priority) into ``a`` (higher).

        Hashes recurse; lists concatenate (deduped); a scalar already present
        in ``a`` wins — later levels never clobber an earlier level's value.
        With ``merge_hash_arrays``, equal-length lists of dicts are merged
        element-wise by index."""
        if a is _MISSING or a is None:
            return deepcopy(b)
        if isinstance(a, list) and isinstance(b, list):
            return self._merge_lists(a, b)
        if not isinstance(a, dict) or not isinstance(b, dict):
            # Mismatched or scalar types: higher-priority (a) wins.
            return a
        result = deepcopy(a)
        for k, v in b.items():
            if k in result and isinstance(result[k], dict):
                result[k] = self.deep_merge(result[k], v)
            elif k in result and isinstance(result[k], list):
                result[k] = self._merge_lists(result[k], v)
            elif k not in result:
                # Key only in the lower-priority side; take it. An existing
                # scalar in ``result`` (higher priority) is left untouched.
                result[k] = deepcopy(v)
        return result

    def _merge_lists(self, a, v):
        if not isinstance(v, list):
            out = list(a)
            out.append(v)
            return out
        if (
            self.merge_hash_arrays
            and len(a) == len(v)
            and all(isinstance(x, dict) for x in a)
            and all(isinstance(x, dict) for x in v)
        ):
            return [self.deep_merge(x, y) for x, y in zip(a, v)]
        extra = [item for item in v if item not in a]
        return a + deepcopy(extra)


# ---------------------------------------------------------------------------
# Puppet MergeStrategy port (``pops/merge_strategy.rb``) -- unwired.
#
# Runs beside the legacy ``Merge``/``make_merge`` above. A later phase wires
# this in per location/level/layer and removes the legacy code once every
# strategy (including ``deep``) is ported.
# ---------------------------------------------------------------------------

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


def _ruby_inspect(value):
    """Ruby ``inspect``, AIO/Ruby-3.2 ``Hash#=>`` rendering (matches production Puppet)."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_ruby_inspect(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(
            "{}=>{}".format(_ruby_inspect(k), _ruby_inspect(v))
            for k, v in value.items()
        )
        return "{" + pairs + "}"
    return repr(value)


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
        if isinstance(merge, dict):
            name = merge.get("strategy")
            if name is None:
                raise MergeError(
                    "The hash given as 'merge' must contain the name of a "
                    "strategy in string form for the key 'strategy'"
                )
            options = {} if len(merge) == 1 else dict(merge)
        else:
            name = merge
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

    def lookup(self, variants, fn):
        """merge_strategy.rb:126-151."""
        variants = list(variants)
        if not variants:
            return _MISSING
        if len(variants) == 1:
            result = fn(variants[0])
            if result is _MISSING:
                return _MISSING
            return self.merge_single(result)
        memo = _MISSING
        for variant in variants:
            value = fn(variant)
            if value is _MISSING:
                continue
            if memo is _MISSING:
                memo = self.convert_value(value)
            else:
                memo = self.merge(memo, value)
        return memo

    def merge(self, e1, e2):
        """merge_strategy.rb:95-100."""
        self._assert("The first element of the merge", e1)
        self._assert("The second element of the merge", e2)
        return self.checked_merge(e1, e2)

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

    def lookup(self, variants, fn):
        """merge_strategy.rb:223-228 -- stop at the first found; never merge."""
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
