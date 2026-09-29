# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Merge strategies for accumulating values across the hierarchy.

Ports Puppet's ``pops/merge_strategy.rb`` and ``deep_merge_core``.
"""

from copy import deepcopy

from ._navigation import _MISSING
from .util import LookupDict

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
            raise ValueError("Unsupported merge type: {!r}".format(spec))
    else:
        strategy = spec
    if strategy in (None, "first"):
        return None
    if strategy not in _MERGE_STRATEGIES:
        raise ValueError("Unknown merge strategy: {!r}".format(strategy))
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
            self.value = LookupDict()
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
            out = LookupDict()
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
            out = LookupDict()
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
