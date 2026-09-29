"""End-to-end lookup, interpolation, merge, and glob behavior."""

import pytest

from pyera import Hiera


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


def test_get_kwargs_context_reaches_source_resolution(hiera_root):
    # Per-call **kwargs are documented context overrides, and they reach
    # source resolution: `environment=` as a kwarg selects the
    # environments/%{environment}.yaml level instead of falling through to
    # common.yaml.
    h = make(hiera_root)  # no instance context at all
    assert h.get("ntp::servers", environment="production") == ["prod.pool.ntp.org"]


def test_get_context_arg_and_kwargs_agree(hiera_root):
    # The positional context= path already worked; kwargs must match it.
    h = make(hiera_root)
    assert h.get("ntp::servers", context={"environment": "production"}) == h.get(
        "ntp::servers", environment="production"
    )


def test_has_kwargs_context_reaches_source_resolution(hiera_root):
    # has() funnels context through kwargs exactly like get(), reaching
    # source resolution the same way.
    h = make(hiera_root)
    assert h.has("lookup_greeting", environment="production") is True
    # Absent the environment, that key exists only in the production level.
    assert h.has("lookup_greeting") is False


def test_scoped_has_uses_bound_context(hiera_root):
    # ScopedHiera.has uses its own bound scope for path resolution too, not
    # just for interpolation.
    h = make(hiera_root)
    assert h.scoped(environment="production").has("lookup_greeting") is True


def test_scoped_has_per_call_override_wins(hiera_root):
    # A per-call override always wins over the bound context -- the same
    # precedence as .get() -- so the two agree.
    # Bind an environment with no data file, then override it per call with
    # the real one. Only correct precedence (per-call over bound) consults
    # the production level.
    staging = make(hiera_root).scoped(environment="staging")
    assert staging.has("lookup_greeting", environment="production") is True
    assert staging.get("lookup_greeting", environment="production") == "myapp in prod"
    # Without the override, the bound scope stands and the key is absent.
    assert staging.has("lookup_greeting") is False
    # .has and .get agree on the overridden value, not just on existence.
    assert staging.get("ntp::servers", environment="production") == [
        "prod.pool.ntp.org"
    ]


def test_scoped_does_not_leak_context(hiera_root):
    # Each scoped() call gets its own independent context dict.
    h = make(hiera_root)
    h.scoped(environment="production")
    fresh = h.scoped()
    assert fresh.context == {}


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


def test_legacy_merge_deep_flag_extension(make_tree):
    # Non-Puppet extension; this test goes with the feature: `merge=dict,
    # merge_deep=True` is our own legacy convenience spelling, not part of
    # Puppet's lookup() call forms.
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
    h = Hiera(str(root / "hiera.yaml"), context={"environment": "prod"})
    merged = h.get("conf", merge=dict, merge_deep=True)
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
