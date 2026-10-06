"""A ``Hiera.scoped(...)`` view survives ``copy``, ``copy.deepcopy`` and
``pickle`` -- it is a plain :class:`~hyera.Hiera` instance (``_view`` builds
it with ``object.__new__`` plus a ``__dict__`` copy, no ``__getattr__``
proxy), so the usual object machinery is what these exercise, same as any
other ``Hiera``: ``Hiera.__getstate__``/``__setstate__`` (needed once the
scope-keyed caches held a ``threading.Lock``) drop and rebuild the caches,
never anything specific to a view.
"""

import copy
import pickle

import pytest

from hyera import Hiera


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
        assert clone.scope == s.scope
        assert clone.lookup("k") == s.lookup("k")


def test_getvar_returns_a_copy_of_the_scope_value(make_tree):
    from hyera import Scope

    root = make_tree(
        {"hierarchy": [{"name": "os", "path": "os/%{facts.os.family}.yaml"}]},
        files={
            "data/os/RedHat.yaml": 'k: redhat\nwho: "%{facts.os.family}"\n',
            "data/os/Debian.yaml": "k: debian\n",
        },
    )
    scope = Scope(facts={"os": {"family": "RedHat"}})
    h = Hiera(str(root / "hiera.yaml"), scope=scope)
    assert h.lookup("who") == "RedHat"

    os_fact = h.getvar("facts.os")
    os_fact["family"] = "Debian"

    assert h.getvar("facts.os.family") == "RedHat"
    assert h.lookup("who") == "RedHat"
    assert h.lookup("k") == "redhat"
    assert h.scope == Scope(facts={"os": {"family": "RedHat"}})


def test_scoped_missing_attribute_raises_attribute_error(make_tree):
    h = _make_hiera(make_tree, use_path=False)
    s = h.scoped(environment="production")

    assert getattr(s, "nope", "d") == "d"
    assert hasattr(s, "__nope__") is False
