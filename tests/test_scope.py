"""``hyera.Scope``: Puppet's top scope as a value object."""

import logging

import pytest

from hyera import InterpolationError, Scope


class _Weird:
    def __str__(self):
        return "weird"


def test_environment_defaults_to_production():
    assert Scope().environment == "production"


def test_environment_resolution_order():
    # Explicit argument wins over a variable, which wins over the default.
    explicit = Scope(environment="staging", variables={"environment": "fromvar"})
    assert explicit.environment == "staging"

    from_var = Scope(variables={"environment": "fromvar"})
    assert from_var.environment == "fromvar"

    default = Scope()
    assert default.environment == "production"


def test_facts_are_top_scope_variables_and_facts_hash():
    scope = Scope(facts={"os": {"family": "Debian"}, "kernel": "Linux"})
    assert scope.lookup("os") == {"family": "Debian"}
    assert scope.lookup("kernel") == "Linux"
    assert scope.lookup("facts") == {"os": {"family": "Debian"}, "kernel": "Linux"}


def test_variable_wins_over_fact_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        scope = Scope(variables={"role": "web"}, facts={"role": "db"})
    assert scope.lookup("role") == "web"
    assert scope.lookup("facts")["role"] == "db"
    assert any(
        "role" in r.getMessage() and "already set to" in r.getMessage()
        for r in caplog.records
    )


def test_environment_fact_ignored_with_warning(caplog):
    # Oracle ("collide"): %{environment} stays "production", %{facts.environment}
    # keeps the raw fact value "fromfacts".
    with caplog.at_level(logging.WARNING):
        scope = Scope(facts={"environment": "fromfacts"})
    assert scope.environment == "production"
    assert scope.lookup("environment") == "production"
    assert scope.lookup("facts")["environment"] == "fromfacts"
    assert any("environment" in r.getMessage() for r in caplog.records)


def test_server_facts_under_facts_and_variables(caplog):
    # Oracle ("collide"): a node-parameter serverversion wins over the
    # server_facts one; server_facts.environment is always the resolved
    # environment, forced last.
    with caplog.at_level(logging.WARNING):
        scope = Scope(
            variables={"serverversion": "1.0-fact"},
            server_facts={"serverversion": "8.10.0"},
        )
    assert scope.lookup("serverversion") == "1.0-fact"
    server_facts = scope.lookup("server_facts")
    assert server_facts["serverversion"] == "8.10.0"
    assert server_facts["environment"] == "production"
    assert any("serverversion" in r.getMessage() for r in caplog.records)


def test_trusted_local_default():
    scope = Scope()
    trusted = scope.lookup("trusted")
    assert trusted == {
        "authenticated": "local",
        "certname": None,
        "extensions": {},
        "hostname": None,
        "domain": None,
        "external": {},
    }
    assert list(trusted) == [
        "authenticated",
        "certname",
        "extensions",
        "hostname",
        "domain",
        "external",
    ]


def test_trusted_local_from_clientcert():
    dotted = Scope(variables={"clientcert": "web01.example.com"})
    trusted = dotted.lookup("trusted")
    assert trusted["certname"] == "web01.example.com"
    assert trusted["hostname"] == "web01"
    assert trusted["domain"] == "example.com"

    bare = Scope(variables={"clientcert": "web01"})
    trusted2 = bare.lookup("trusted")
    assert trusted2["hostname"] == "web01"
    assert trusted2["domain"] is None


def test_trusted_fact_resurrected_only_when_complete():
    complete = Scope(
        facts={
            "trusted": {
                "authenticated": "remote",
                "certname": "spoof.example.com",
                "extensions": {"pp_role": "x"},
            }
        }
    )
    trusted = complete.lookup("trusted")
    assert trusted == {
        "authenticated": "remote",
        "certname": "spoof.example.com",
        "extensions": {"pp_role": "x"},
    }
    assert complete.lookup("facts")["trusted"]["certname"] == "spoof.example.com"

    partial = Scope(facts={"trusted": {"certname": "fake.example.com"}})
    trusted2 = partial.lookup("trusted")
    assert trusted2["certname"] is None  # not resurrected: falls to the local hash
    assert partial.lookup("facts")["trusted"]["certname"] == "fake.example.com"


def test_explicit_trusted_used_as_is():
    given = {"authenticated": "remote", "certname": "x", "extensions": {}}
    scope = Scope(trusted=given, variables={"clientcert": "ignored.example.com"})
    assert scope.lookup("trusted") == given


def test_facts_sanitized():
    scope = Scope(
        facts={
            "n": None,
            "items": [1, None, "a"],
            "obj": _Weird(),
            "flag": True,
            "count": 0,
        }
    )
    facts = scope.lookup("facts")
    assert facts["n"] == ""
    assert facts["items"] == [1, "", "a"]
    assert facts["obj"] == "weird"
    assert facts["flag"] is True
    assert facts["count"] == 0


def test_falsy_values_kept():
    scope = Scope(
        variables={"flag": False, "count": 0, "text": "", "lst": [], "obj": {}}
    )
    assert scope.lookup("flag") is False
    assert scope.lookup("count") == 0
    assert scope.lookup("text") == ""
    assert scope.lookup("lst") == []
    assert scope.lookup("obj") == {}
    for name in ("flag", "count", "text", "lst", "obj"):
        assert scope.exist(name) is True


def test_defined_nil_variable():
    scope = Scope(variables={"x": None})
    assert scope.lookup("x") is None
    assert scope.exist("x") is True
    assert scope.lookupvar("x") is None


def test_reserved_and_numeric_names_rejected():
    with pytest.raises(ValueError, match="reserved variable name: 'facts'"):
        Scope(variables={"facts": "x"})
    with pytest.raises(ValueError, match="reserved variable name: 'server_facts'"):
        Scope(variables={"server_facts": "x"})
    with pytest.raises(ValueError, match="reserved variable name: 'trusted'"):
        Scope(facts={"trusted": False})
    with pytest.raises(ValueError, match=r"numeric match result variable '\$1'"):
        Scope(variables={"1": "x"})
    for name in ("module_name", "title", "name"):
        with pytest.raises(ValueError, match="Cannot reassign variable"):
            Scope(variables={name: "x"})


def test_scope_constructor_type_validation():
    with pytest.raises(TypeError, match="environment must be a str, not int"):
        Scope(environment=5)
    with pytest.raises(ValueError, match="environment must not be empty"):
        Scope(environment="")
    with pytest.raises(TypeError, match="node_name must be a str, not int"):
        Scope(node_name=5)
    with pytest.raises(TypeError, match="variables must be a mapping, not str"):
        Scope(variables="notamapping")
    with pytest.raises(TypeError, match="variables keys must be strings, not int"):
        Scope(variables={1: "x"})
    with pytest.raises(TypeError, match="Unsupported data type: 'object'"):
        Scope(variables={"a": object()})
    with pytest.raises(TypeError, match="Unsupported data type: 'int'"):
        Scope(variables={"a": {1: "x"}})


def test_lookupvar_and_exist_reject_non_str_names():
    scope = Scope()
    with pytest.raises(TypeError, match="scope variable name must be a str, not int"):
        scope.lookupvar(5)
    with pytest.raises(TypeError, match="scope variable name must be a str, not int"):
        scope.exist(5)


def test_with_local_scope_rejects_none():
    with pytest.raises(TypeError, match="variables must be a mapping, not None"):
        Scope().with_local_scope(None)


def test_exist_finds_a_local_layer_variable():
    child = Scope().with_local_scope({"x": 1})
    assert child.exist("x") is True


def test_sanitize_fact_key_rejects_a_list():
    from hyera._scope import _sanitize_fact_key

    with pytest.raises(TypeError, match="a list cannot be used as a fact key"):
        _sanitize_fact_key(["a"])


def test_scope_repr():
    scope = Scope(variables={"a": 1}, facts={"os": "x"})
    assert repr(scope) == (
        "Scope(environment='production', strict='warning', variables=9, facts=1)"
    )


def test_main_class_and_builtin_variables():
    scope = Scope()
    assert scope.lookup("name") == "main"
    assert scope.lookup("title") == "main"
    assert scope.lookup("module_name") == ""
    assert scope.lookup("caller_module_name") is None
    assert scope.exist("caller_module_name") is True
    assert scope.lookup("clientversion") is Scope.UNDEFINED
    assert scope.lookup("0") is None
    assert scope.exist("0") is False


def test_qualified_names():
    scope = Scope(facts={"os": {"family": "Debian"}})
    assert scope.lookup("foo::bar") is Scope.UNDEFINED
    assert scope.exist("foo::bar") is False
    assert scope.lookup("::os") == {"family": "Debian"}
    assert scope.lookup("::module_name") == ""


def test_lookupvar_strict_modes(caplog):
    off = Scope(strict="off")
    assert off.lookupvar("nope") is None

    warning = Scope(strict="warning")
    with caplog.at_level(logging.WARNING):
        assert warning.lookupvar("nope") is None
        assert warning.lookupvar("nope") is None  # second call: deduped
    matches = [
        r for r in caplog.records if "Undefined variable 'nope'" in r.getMessage()
    ]
    assert len(matches) == 1

    error = Scope(strict="error")
    with pytest.raises(InterpolationError, match="Undefined variable 'nope'"):
        error.lookupvar("nope")

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert error.lookupvar("nope", lenient=True) is None
    assert any(
        "Interpolation failed with 'nope'" in r.getMessage() for r in caplog.records
    )

    qualified_error = Scope(strict="error")
    with pytest.raises(InterpolationError, match=r"class foo could not be found"):
        qualified_error.lookupvar("foo::bar")


def test_with_local_scope():
    parent = Scope(variables={"x": 1})
    child = parent.with_local_scope({"x": 2, "y": 3})
    assert child.lookup("x") == 2
    assert child.lookup("y") == 3
    assert parent.lookup("x") == 1
    assert parent.exist("y") is False

    grandchild = child.with_local_scope({"x": 4})
    assert grandchild.lookup("x") == 4
    assert grandchild.lookup("y") == 3  # inherited from the outer layer


def test_derive_layers_facts_shallow():
    # verify-spec-context-facts/ours_layer.py's scenario: a per-call fact
    # override must not leave an unrelated fact stale, and must not touch
    # the parent.
    base = Scope(facts={"os": "linux", "kernel": "Linux"})
    derived = base.derive(facts={"os": "windows"})
    assert derived.lookup("os") == "windows"
    assert derived.lookup("kernel") == "Linux"
    assert derived.lookup("facts") == {"os": "windows", "kernel": "Linux"}
    assert base.lookup("os") == "linux"

    reenvironmented = base.derive(environment="staging")
    assert reenvironmented.environment == "staging"
    assert base.environment == "production"


def test_scope_value_semantics():
    a = Scope(variables={"x": 1})
    b = Scope(variables={"x": 1})
    c = Scope(variables={"x": True})
    d = Scope(variables={"x": 1.0})
    assert a == b
    assert hash(a) == hash(b)
    assert a != c
    assert a != d
    assert a != "not a scope"

    original = {"y": [1, 2]}
    scope = Scope(variables=original)
    original["y"].append(3)
    assert scope.lookup("y") == [1, 2]

    with pytest.raises(TypeError, match="Unsupported data type"):
        Scope(variables={"bad": object()})

    with pytest.raises(TypeError, match="split"):
        Scope(variables={"clientcert": 42})

    with pytest.raises(ValueError):
        Scope(strict="bogus")


def test_node_name_property():
    assert Scope().node_name is None
    assert Scope(node_name="web01.example.com").node_name == "web01.example.com"
