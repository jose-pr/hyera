"""A YAML data file's own non-hash rule (``YAMLBackend._as_data_hash``) reads
``Backend.strict`` at call time (``yaml_data.rb:31``'s ``Puppet[:strict]``),
not once and for all: the same file, on the same ``Hiera`` instance, raises
under a ``strict="error"`` scope and warns-and-falls-through under
``strict="warning"``, in either order -- guards the ``(path, backend.strict)``
cache key in ``core.Hiera._load_file`` against caching the *adapted* result
under a bare path, which would freeze whichever strictness ran first.
"""

import logging

import pytest

from hyera import BackendError, Hiera


def _non_hash_tree(make_tree):
    return make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={"data/one.yaml": "- a\n- b\n"},
    )


def test_strict_error_then_warning(make_tree, caplog):
    root = _non_hash_tree(make_tree)
    h = Hiera(str(root / "hiera.yaml"))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(KeyError):
            h.scoped(strict="warning").lookup("k")
    assert any(
        "file does not contain a valid yaml hash" in r.getMessage()
        for r in caplog.records
    )

    with pytest.raises(BackendError, match="file does not contain a valid yaml hash"):
        h.scoped(strict="error").lookup("k")

    # A later warning-strict lookup still falls through cleanly -- the
    # error-strict attempt above must not have poisoned the cache for it.
    with pytest.raises(KeyError):
        h.scoped(strict="warning").lookup("k")


def test_strict_warning_then_error(make_tree):
    root = _non_hash_tree(make_tree)
    h = Hiera(str(root / "hiera.yaml"))

    with pytest.raises(KeyError):
        h.scoped(strict="warning").lookup("k")

    # A later error-strict lookup on the very same instance still raises --
    # the warning-strict attempt above must not have cached its way past the
    # non-hash rule for "error" too.
    with pytest.raises(BackendError, match="file does not contain a valid yaml hash"):
        h.scoped(strict="error").lookup("k")
