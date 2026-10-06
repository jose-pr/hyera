"""``uri``/``uris`` locations reaching function providers."""

import re

import pytest

from hyera import ConfigError, Hiera, Scope
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)

# --- uri/uris locations --------------------------------------------------


def test_uri_reaches_provider_as_option(make_tree, backends, calls, script):
    # No file named after this uri exists anywhere: the provider is still
    # called (proving the location is never fetched or checked for
    # existence -- `location.exist` is always True for a uri).
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "uri": "http://h.invalid/%{facts.os.family}",
                }
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"os": {"family": "RedHat"}}))
    assert h.lookup("k") == "v"
    assert calls[0][2] == {"uri": "http://h.invalid/RedHat"}


def test_lookup_key_receives_uri(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "uri": "mailto:a@b"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert calls[0][2]["uri"] == "mailto:a@b"


def test_uris_walked_in_order(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "s",
                    "lookup_key": "test_lookup_key",
                    "uris": ["mailto:a@b", "mailto:c@d"],
                }
            ]
        },
    )

    def fn(key, options, context):
        if options["uri"] == "mailto:a@b":
            context.not_found()
        return "found-second"

    script["lookup_key"] = fn
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "found-second"
    assert [c[2]["uri"] for c in calls] == ["mailto:a@b", "mailto:c@d"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("HTTP://X:8080/p?q=A B#f", "http://X:8080/p?q=A%20B#f"),
        ("http://x:80/a", "http://x/a"),
        ("https://x:443", "https://x"),
        ("http://x:/", "http://x/"),
        ("Foo+Bar://h/", "foo+bar://h/"),
    ],
)
def test_ruby_uri_to_s(raw, expected):
    from hyera._config.location_resolver import _ruby_uri

    assert _ruby_uri(raw) == expected


@pytest.mark.parametrize("raw", ["a b", "http://a%2", "http://u@h/p#f g"])
def test_bad_uri_raises_config_error(raw):
    from hyera._config.location_resolver import _ruby_uri

    with pytest.raises(
        ConfigError, match=re.escape('bad URI (is not URI?): "{}"'.format(raw))
    ):
        _ruby_uri(raw)


def test_uri_undefined_variable_is_lenient(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "uri": "%{nosuch}"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert calls[0][2]["uri"] == ""


def test_uri_method_syntax_rejected(make_tree, backends, calls, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "uri": "%{lookup('k')}"}
            ]
        },
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ConfigError, match="Interpolation using method syntax"):
        h.lookup("k")
    assert calls == []
