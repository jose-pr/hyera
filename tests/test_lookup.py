"""End-to-end lookup, interpolation, merge, and glob behavior."""

import pytest

from hiera import Hiera


def make(hiera_root, **context):
    return Hiera(str(hiera_root / "hiera.yaml"), context=context)


def test_first_match_wins(hiera_root):
    h = make(hiera_root, environment="production")
    # production.yaml overrides common.yaml
    assert h.get("ntp::servers") == ["prod.pool.ntp.org"]


def test_falls_through_to_common(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("app::name") == "myapp"


def test_missing_key_default(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("nope::key", default="fallback") == "fallback"


def test_missing_key_throw(hiera_root):
    h = make(hiera_root, environment="production")
    with pytest.raises(KeyError):
        h.get("nope::key", throw=True)


def test_has(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.has("app::name") is True
    assert h.has("nope::key") is False


def test_scope_interpolation(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("greeting") == "hello production"


def test_hiera_function_interpolation(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("lookup_greeting") == "myapp in prod"


def test_alias_returns_referenced_value(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("alias_target") == "myapp"


def test_literal_percent(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("literal_pct") == "100% done"


def test_array_merge_includes_glob_level(hiera_root):
    h = make(hiera_root, environment="production")
    merged = h.get("classes", merge=list)
    # production + web (glob) + common, in hierarchy order
    assert merged == ["prod", "web", "base"]


def test_hash_merge_shallow(hiera_root):
    h = make(hiera_root, environment="production")
    db = h.get("db", merge=dict)
    # production wins for host; port comes from common
    assert db == {"host": "db.prod.internal", "port": 5432}


def test_dotted_lookup(hiera_root):
    h = make(hiera_root, environment="production")
    assert h.get("db.port") == 5432


def test_scoped_reuses_context(hiera_root):
    h = make(hiera_root)
    prod = h.scoped(environment="production")
    assert prod.get("ntp::servers") == ["prod.pool.ntp.org"]


def test_scoped_does_not_leak_context(hiera_root):
    # Regression: mutable-default-arg contamination between scoped() calls.
    h = make(hiera_root)
    h.scoped(environment="production")
    fresh = h.scoped()
    assert fresh.context == {}


def test_dict_base_config(hiera_root):
    config = {
        "defaults": {"data_hash": "yaml_data", "data_dir": "data"},
        "hierarchy": [{"name": "Common", "path": "common.yaml"}],
    }
    h = Hiera(config, base_path=str(hiera_root))
    assert h.get("app::name") == "myapp"
