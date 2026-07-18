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


def test_standalone_alias_preserves_list_type(hiera_root):
    # A single stand-alone alias returns the referenced value's native type.
    h = make(hiera_root, environment="production")
    assert h.get("alias_list") == ["prod.pool.ntp.org"]


def test_numeric_value_stringified_in_interpolation(hiera_root):
    # An int resolved by %{hiera(...)} embedded in a string is stringified.
    h = make(hiera_root, environment="production")
    assert h.get("port_msg") == "listening on 5432"


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


def test_falsy_values_are_returned(tmp_path):
    # Regression: `%{hiera('x')}` where x is 0/""/False must resolve, not error.
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text(
        "zero: 0\n"
        "empty: ''\n"
        "flag: false\n"
        "ref_zero: \"value=%{hiera('zero')}\"\n",
        encoding="utf-8",
    )
    (tmp_path / "hiera.yaml").write_text(
        "defaults:\n  data_hash: yaml_data\n  data_dir: data\n"
        "hierarchy:\n  - name: c\n    path: common.yaml\n",
        encoding="utf-8",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.get("zero") == 0
    assert h.get("empty") == ""
    assert h.get("flag") is False
    assert h.get("ref_zero") == "value=0"


def test_deep_hash_merge(tmp_path):
    (tmp_path / "data" / "environments").mkdir(parents=True)
    (tmp_path / "data" / "common.yaml").write_text(
        "conf:\n  db:\n    host: localhost\n    port: 5432\n  cache:\n    ttl: 60\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "environments" / "prod.yaml").write_text(
        "conf:\n  db:\n    host: prod.db\n",
        encoding="utf-8",
    )
    (tmp_path / "hiera.yaml").write_text(
        "defaults:\n  data_hash: yaml_data\n  data_dir: data\n"
        "hierarchy:\n"
        "  - name: env\n    path: environments/%{environment}.yaml\n"
        "  - name: c\n    path: common.yaml\n",
        encoding="utf-8",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"), context={"environment": "prod"})
    merged = h.get("conf", merge=dict, merge_deep=True)
    # prod overrides db.host but keeps db.port and the whole cache subtree.
    assert merged == {
        "db": {"host": "prod.db", "port": 5432},
        "cache": {"ttl": 60},
    }


def test_dict_base_config(hiera_root):
    config = {
        "defaults": {"data_hash": "yaml_data", "data_dir": "data"},
        "hierarchy": [{"name": "Common", "path": "common.yaml"}],
    }
    h = Hiera(config, base_path=str(hiera_root))
    assert h.get("app::name") == "myapp"
