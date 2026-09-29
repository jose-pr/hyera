"""End-to-end lookup, interpolation, merge, and glob behavior."""

import pytest

from hyera import Hiera, Scope


def make(hiera_root, **variables):
    return Hiera(str(hiera_root / "hiera.yaml"), scope=Scope(variables=variables))


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


def test_standalone_alias_preserves_list_type(hiera_root):
    # A single stand-alone alias returns the referenced value's native type.
    h = make(hiera_root, environment="production")
    assert h.get("alias_list") == ["prod.pool.ntp.org"]


def test_array_merge_includes_glob_level(hiera_root):
    h = make(hiera_root, environment="production")
    merged = h.get("classes", merge="unique")
    # production + web (glob) + common, in hierarchy order
    assert merged == ["prod", "web", "base"]


def test_hash_merge_shallow(hiera_root):
    h = make(hiera_root, environment="production")
    db = h.get("db", merge="hash")
    # production wins for host; port comes from common
    assert db == {"host": "db.prod.internal", "port": 5432}


def test_scoped_reuses_context(hiera_root):
    h = make(hiera_root)
    prod = h.scoped(environment="production")
    assert prod.get("ntp::servers") == ["prod.pool.ntp.org"]


def test_scoped_has_uses_bound_context(hiera_root):
    # ScopedHiera.has uses its own bound scope for path resolution too, not
    # just for interpolation.
    h = make(hiera_root)
    assert h.scoped(environment="production").has("lookup_greeting") is True


def test_scoped_does_not_leak_context(hiera_root):
    # Each scoped() call derives its own scope; the instance's own scope
    # (and a later scoped() call with no overrides) never sees it.
    h = make(hiera_root)
    h.scoped(environment="staging")
    assert h.scope.environment == "production"
    fresh = h.scoped()
    assert fresh.scope.environment == "production"


def test_falsy_values_are_returned(make_tree):
    # `%{hiera('x')}` resolves to a falsy value (0/""/False) rather than
    # treating it as missing.
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": (
                "zero: 0\n"
                "empty: ''\n"
                "flag: false\n"
                "ref_zero: \"value=%{hiera('zero')}\"\n"
            )
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.get("zero") == 0
    assert h.get("empty") == ""
    assert h.get("flag") is False
    assert h.get("ref_zero") == "value=0"


def test_deep_hash_merge(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "env", "path": "environments/%{environment}.yaml"},
                {"name": "c", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": (
                "conf:\n  db:\n    host: localhost\n    port: 5432\n  cache:\n    ttl: 60\n"
            ),
            "data/environments/prod.yaml": "conf:\n  db:\n    host: prod.db\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"environment": "prod"}))
    merged = h.get("conf", merge="deep")
    # prod overrides db.host but keeps db.port and the whole cache subtree.
    assert merged == {
        "db": {"host": "prod.db", "port": 5432},
        "cache": {"ttl": 60},
    }


def test_dict_base_config(hiera_root):
    config = {
        "version": 5,
        "defaults": {"data_hash": "yaml_data", "datadir": "data"},
        "hierarchy": [{"name": "Common", "path": "common.yaml"}],
    }
    h = Hiera(config, base_path=str(hiera_root))
    assert h.get("app::name") == "myapp"
