"""``ScopedHiera`` survives ``copy``, ``copy.deepcopy`` and ``pickle``.

``__getattr__`` must guard against unbounded recursion: copying/pickling
rebuilds the object without ``__init__``, and the resulting attribute
lookups (looking for dunder/state methods on the still-empty instance)
would otherwise reach ``__getattr__`` again, whose own body reads
``self.hiera`` -- itself an attribute lookup on the same not-yet-initialized
instance.
"""

import copy
import pickle

import pytest

from pyera import Hiera


def _make_hiera(make_tree, use_path: bool):
    config = {
        "version": 5,
        "defaults": {"data_hash": "yaml_data", "datadir": "data"},
        "hierarchy": [{"name": "c", "path": "common.yaml"}],
    }
    root = make_tree(config, files={"data/common.yaml": "k: v\n"})
    if not use_path:
        return Hiera(config, base_path=str(root))
    return Hiera(str(root / "hiera.yaml"))


@pytest.mark.parametrize("use_path", [False, True], ids=["dict", "path"])
def test_scoped_copies_and_pickles(use_path, make_tree):
    h = _make_hiera(make_tree, use_path)
    s = h.scoped(environment="production")

    for clone in (
        copy.copy(s),
        copy.deepcopy(s),
        pickle.loads(pickle.dumps(s)),
    ):
        assert clone.context == s.context
        assert clone.get("k") == s.get("k")


def test_scoped_missing_attribute_raises_attribute_error(make_tree):
    h = _make_hiera(make_tree, use_path=False)
    s = h.scoped(environment="production")

    assert getattr(s, "nope", "d") == "d"
    assert hasattr(s, "__nope__") is False
