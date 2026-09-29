"""Binding a ``Scope`` to ``Hiera`` and its scoped views, in place of context
dicts."""

import pytest

from hyera import Hiera, Scope


def test_hiera_uses_bound_scope(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "greeting: 'hi %{who}'\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"who": "bob"}))
    assert h.scope.lookup("who") == "bob"
    assert h.lookup("greeting") == "hi bob"


def test_scoped_view_binds_every_method(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "env", "path": "environments/%{environment}.yaml"},
                {"name": "c", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": "greeting: 'hi %{environment}'\n",
            "data/environments/staging.yaml": "only_staging: true\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    view = h.scoped(environment="staging")

    assert view.lookup("greeting") == "hi staging"
    assert "only_staging" in view
    assert any("staging" in str(p) for p in view.sources())
    assert view.format("env=%{environment}") == "env=staging"

    # The parent instance's own scope is unchanged.
    assert h.scope.environment == "production"
    assert h.lookup("greeting") == "hi production"
    assert "only_staging" not in h


def test_unknown_keywords_raise_type_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(TypeError):
        h.lookup("k", thorw=True)
    with pytest.raises(TypeError):
        h.lookup({"name": "k", "bogus": 1})
    with pytest.raises(TypeError):
        Hiera(str(root / "hiera.yaml"), environment="x")


def test_scope_argument_must_be_a_scope(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    with pytest.raises(TypeError, match="scope must be a hyera.Scope"):
        Hiera(str(root / "hiera.yaml"), scope={"environment": "production"})


def test_source_cache_distinguishes_true_and_1(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "v", "path": "v/%{x}.yaml"}]},
        files={
            "data/v/true.yaml": "picked: bool\n",
            "data/v/1.yaml": "picked: int\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    bool_view = h.scoped(variables={"x": True})
    int_view = h.scoped(variables={"x": 1})

    assert bool_view.lookup("picked") == "bool"
    assert int_view.lookup("picked") == "int"
    # Re-read in the opposite order to catch a cache keyed loosely enough
    # to conflate True and 1.
    assert int_view.lookup("picked") == "int"
    assert bool_view.lookup("picked") == "bool"
