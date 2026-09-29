"""``ScopedHiera`` survives ``copy``, ``copy.deepcopy`` and ``pickle``.

``__getattr__`` used to recurse forever: copying/pickling rebuilds the
object without ``__init__``, and the resulting attribute lookups (looking
for dunder/state methods on the still-empty instance) reached
``__getattr__`` again, whose own body reads ``self.hiera`` -- itself an
attribute lookup on the same not-yet-initialized instance.
"""

import copy
import pickle

import pytest

from pyera import Hiera


def _make_hiera(tmp_path, use_path: bool):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text("k: v\n", encoding="utf-8")
    config = {
        "version": 5,
        "defaults": {"data_hash": "yaml_data", "data_dir": "data"},
        "hierarchy": [{"name": "c", "path": "common.yaml"}],
    }
    if not use_path:
        return Hiera(config, base_path=str(tmp_path))
    config_path = tmp_path / "hiera.yaml"
    import yaml

    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return Hiera(str(config_path))


@pytest.mark.parametrize("use_path", [False, True], ids=["dict", "path"])
def test_scoped_copies_and_pickles(use_path, tmp_path):
    h = _make_hiera(tmp_path, use_path)
    s = h.scoped(environment="production")

    for clone in (
        copy.copy(s),
        copy.deepcopy(s),
        pickle.loads(pickle.dumps(s)),
    ):
        assert clone.context == s.context
        assert clone.get("k") == s.get("k")


def test_scoped_missing_attribute_raises_attribute_error(tmp_path):
    h = _make_hiera(tmp_path, use_path=False)
    s = h.scoped(environment="production")

    assert getattr(s, "nope", "d") == "d"
    assert hasattr(s, "__nope__") is False
