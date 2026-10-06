"""Helpers shared by the cache test modules."""

from hyera._lookup import locations


def _counting_resolver(monkeypatch):
    """Wrap ``locations.resolve_locations`` with a call counter: one call per
    hierarchy level per build, so ``calls[0] // n_levels`` is the number of
    distinct location builds a test's assertions care about.
    """
    calls = [0]
    original = locations.resolve_locations

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(locations, "resolve_locations", counting)
    return calls
