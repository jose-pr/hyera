"""Small helpers: dotted-path lookup dict and ruby-symbol-aware access."""

from functools import reduce


def sym_lookup(obj, key, default=None):
    """dict lookup with optional ruby-symbol-like (``:key``) keys."""
    for lookup_key in (key, ":{}".format(key)):
        if lookup_key in obj:
            return obj[lookup_key]
    return default


class LookupDict(dict):
    """A ``dict`` that supports dotted-path lookups (``a.b.0.c``).

    Note: intentionally not hashable. The original implementation defined
    ``__hash__`` over the frozenset of keys, which is unsafe for a mutable
    mapping (equal-by-value dicts with different keys collide, and the hash
    changes as the dict mutates). Nothing in hiera uses a LookupDict as a
    key, so it stays unhashable like a plain ``dict``.
    """

    def lookup(self, key):
        """Resolve a dotted path, indexing into nested dicts and lists.

        Raises ``KeyError`` (missing dict key) or ``IndexError`` (list index
        out of range), matching normal indexing semantics.
        """
        return reduce(
            lambda obj, seg: obj[int(seg) if isinstance(obj, list) else seg],
            key.split("."),
            self,
        )
