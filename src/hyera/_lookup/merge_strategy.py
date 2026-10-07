# Ported from Puppet 8 lib/puppet/pops/merge_strategy.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Merge strategies for accumulating values across the hierarchy.

Ports Puppet's ``pops/merge_strategy.rb``; the deep strategies call the
deep_merge gem port in :mod:`hyera._lookup.deep_merge`.
"""

from __future__ import annotations

import collections.abc
import contextlib
import typing as _ty

from .deep_merge import _rb_truthy, _ruby_or, _ruby_to_s, deep_merge
from .interpolation import unshare
from .navigation import _MISSING
from ..exceptions import MergeError
from .._enums import _StrEnum, _plain

__all__ = ["Merge", "MergeLike"]


class Merge(_StrEnum):
    """A merge strategy name: the ``merge=`` argument of
    :meth:`~hyera.Hiera.lookup`/:meth:`~hyera.Hiera.dig`/
    :meth:`~hyera.Hiera.get`/:meth:`~hyera.Hiera.explain`, and the
    ``"strategy"`` key of a :data:`MergeLike` mapping.

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
    ``merge_hash_arrays`` as extra ``MergeLike`` mapping keys)."""


#: The type of every public ``merge=`` argument: a strategy name or member
#: (``Merge.DEEP``/``"deep"``/...), a ``{"strategy": ..., ...}`` mapping
#: with Puppet's deep-merge options, or ``None`` for the level's own default.
MergeLike = _ty.Union[Merge, str, _ty.Mapping[str, _ty.Any], None]

#: The shared no-op context manager :meth:`MergeStrategy.lookup` uses when
#: called with no ``invocation`` at all.
_NULL_CTX = contextlib.nullcontext()

#: Registered strategy name -> MergeStrategy subclass, in definition order.
_STRATEGIES: dict = {}


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
