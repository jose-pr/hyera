# Derived from phiera/util.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
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

    Note: intentionally not hashable, like a plain ``dict``. Hashing a mutable
    mapping by its keys is unsafe (equal-by-value dicts with different keys
    would collide, and the hash would change as the dict mutates). Nothing in
    hiera uses a LookupDict as a key.
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
