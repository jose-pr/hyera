"""What a function provider may return, and the errors it raises when it breaks the
contract.
"""

import datetime
import decimal
import re

import pytest

from hyera import BackendError, ConfigError, Hiera, HieraError, KeyNotFoundError
from hyera._lookup.function_provider import _EnvironmentContext
from hyera._lookup.provider_classes import _FunctionProvider
from hyera._lookup.navigation import _MISSING
from hyera.backends import HOCONBackend, JSONBackend, SopsBackend, YAMLBackend
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)

# --- contracts -----------------------------------------------------------


def test_provider_value_rich_data_validated(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "lookup_key": "test_lookup_key", "path": "a.yaml"}
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: {True: 1}
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        BackendError, match="has wrong type, expects Puppet::LookupValue"
    ):
        h.lookup("k")


def test_provider_value_rich_data_validated_no_location(make_tree, backends, script):
    # A location-less lookup_key entry: the "when using location '...'"
    # clause is omitted entirely from the message, not rendered empty.
    root = make_tree(
        {"hierarchy": [{"name": "s", "lookup_key": "test_lookup_key"}]},
    )
    script["lookup_key"] = lambda key, options, context: {True: 1}
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as exc:
        h.lookup("k")
    assert "when using location" not in str(exc.value)
    assert "has wrong type, expects Puppet::LookupValue" in str(exc.value)


@pytest.mark.parametrize(
    "value",
    [datetime.date(2026, 1, 2), decimal.Decimal("1.5"), b"bytes", {1, 2}],
    ids=["date", "decimal", "bytes", "set"],
)
@pytest.mark.parametrize("kind", ["lookup_key", "data_dig"])
def test_provider_value_of_an_unknown_python_type_is_a_backend_error(
    make_tree, backends, script, kind, value
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "path": "a.yaml", kind: "test_" + kind},
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script[kind] = lambda *args: value
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="function 'test_" + kind + "'.*got "):
        h.lookup("k")


@pytest.mark.parametrize(
    "value",
    [datetime.date(2026, 1, 2), decimal.Decimal("1.5"), b"bytes", {1, 2}],
    ids=["date", "decimal", "bytes", "set"],
)
def test_data_hash_value_of_an_unknown_python_type_names_function_and_key(
    make_tree, backends, script, value
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: {"k": value}
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(HieraError, match="key 'k'.*function 'test_data_hash'"):
        h.lookup("k")


def test_a_tuple_from_a_data_hash_hook_reads_as_a_list(make_tree, backends, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: {"k": ("a", ("b",))}
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == ["a", ["b"]]
    assert h.lookup("k.0") == "a"


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig"])
def test_an_exception_a_hook_raises_propagates_unchanged(
    make_tree, backends, script, kind
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml", kind: "test_" + kind}]},
        files={"data/a.yaml": "x"},
    )

    def hook(*args):
        raise ValueError("from the hook")

    script[kind] = hook
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ValueError, match="from the hook"):
        h.lookup("k")


@pytest.mark.parametrize("kind", ["lookup_key", "data_dig"])
def test_a_tuple_from_a_hook_reads_as_a_list(make_tree, backends, script, kind):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "path": "a.yaml", kind: "test_" + kind},
            ]
        },
        files={"data/a.yaml": "x"},
    )
    script[kind] = lambda *args: {"k": ("a", ("b", "c"))}
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == {"k": ["a", ["b", "c"]]}
    assert h.lookup("k", merge="deep") == {"k": ["a", ["b", "c"]]}


def test_a_tuple_from_a_lookup_key_hook_is_navigable_and_merges(
    make_tree, backends, script
):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml", "lookup_key": "test_lookup_key"},
                {"name": "b", "path": "b.yaml", "lookup_key": "test_lookup_key"},
            ]
        },
        files={"data/a.yaml": "x", "data/b.yaml": "x"},
    )
    script["lookup_key"] = lambda key, options, context: (
        ("a", "b") if options["path"].endswith("a.yaml") else ("c",)
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("tup") == ["a", "b"]
    assert h.lookup("tup.0") == "a"
    assert h.lookup("tup", merge="unique") == ["a", "b", "c"]


def test_data_hash_non_dict_return_is_backend_error(make_tree, backends, script):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: ["not", "a", "hash"]
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        BackendError,
        match=(
            "Value returned from data_hash function 'test_data_hash', when "
            "using location '.*a\\.yaml', has wrong type, expects a Hash "
            # A non-empty list infers as Tuple (Array is the empty-list case).
            "value, got Tuple"
        ),
    ):
        h.lookup("k")


def test_data_hash_path_based_type_label_fallback(make_tree, backends, script):
    # The path-based non-dict check falls back to the plain Python type
    # name for a value that is not any of Puppet's own JSON-ish shapes.
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash", "path": "a.yaml"}]},
        files={"data/a.yaml": "x"},
    )
    script["data_hash"] = lambda path, options: object()
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match="got object$"):
        h.lookup("k")


def test_data_hash_no_location_non_dict_return_is_backend_error(
    make_tree, backends, calls, script
):
    # A location-less data_hash entry calls the backend directly and
    # validates/caches the raw result itself (no path to name in the error).
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash"}]},
    )
    script["data_hash"] = lambda path, options: ["not", "a", "hash"]
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as exc:
        h.lookup("k")
    assert "when using location" not in str(exc.value)
    assert (
        "Value returned from data_hash function 'test_data_hash' has wrong "
        "type, expects a Hash value, got Tuple" in str(exc.value)
    )
    assert calls[0][1] is None


@pytest.mark.parametrize(
    "value, label",
    [
        (True, "Boolean"),
        (None, "Undef"),
        ("x", "String"),
        (5, "Integer"),
        (5.0, "Float"),
        ([], "Array"),
        (object(), "object"),
    ],
    ids=["bool", "none", "str", "int", "float", "empty-array", "other"],
)
def test_data_hash_no_location_type_label_table(
    make_tree, backends, script, value, label
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash"}]},
    )
    script["data_hash"] = lambda path, options: value
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError, match=re.escape("got " + label)):
        h.lookup("k")


def test_data_hash_no_location_called_once_and_cached(
    make_tree, backends, calls, script
):
    root = make_tree(
        {"hierarchy": [{"name": "s", "data_hash": "test_data_hash"}]},
    )
    script["data_hash"] = lambda path, options: {"k": "v"}
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert h.lookup("k") == "v"
    assert len(calls) == 1


def test_data_hash_uri_non_dict_return_is_backend_error_with_location(
    make_tree, backends, script
):
    # A uri location takes the *other* branch of _validate_data_hash's own
    # location-is-None check (unlike the plain location-less case above):
    # `label` is the uri itself, so the message names it.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "s", "data_hash": "test_data_hash", "uri": "x:custom"}
            ]
        },
    )
    script["data_hash"] = lambda path, options: ["not", "a", "hash"]
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(
        BackendError,
        match=re.escape(
            "Value returned from data_hash function 'test_data_hash', when "
            "using location 'x:custom', has wrong type, expects a Hash "
            "value, got Tuple"
        ),
    ):
        h.lookup("k")


def test_function_provider_key_lookup_is_abstract():
    # _FunctionProvider.key_lookup is never called directly in production
    # (every PROVIDER_CLASSES entry is a concrete subclass overriding it);
    # exercised by direct construction/call.
    provider = _FunctionProvider("n", None, {}, None, _EnvironmentContext(), "env")
    with pytest.raises(NotImplementedError):
        provider.key_lookup("root", [], None, None)


def test_data_hash_load_file_missing_is_not_found(make_tree, monkeypatch):
    # load_file returning _MISSING (a cached file vanishing under revalidation) makes
    # this location a miss, not an error.
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml"}]},
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    monkeypatch.setattr(
        h._store,
        "load_file",
        lambda path, backend, options, invocation=None: _MISSING,
    )
    with pytest.raises(KeyNotFoundError):
        h.lookup("k")


def test_data_hash_module_layer_prune_is_applied(tmp_path, make_tree, backends, script):
    # `_prune` is set only for a module-owned level; a location-less data_hash entry in
    # a module's hiera.yaml is the only way to reach the "no location, or a uri"
    # branch's prune call.
    base = make_tree(
        {"hierarchy": [{"name": "g", "path": "g.yaml"}]},
        files={"data/g.yaml": "g: 1\n"},
    )
    modules = tmp_path / "modules"
    mod_config = modules / "m" / "hiera.yaml"
    mod_config.parent.mkdir(parents=True)
    mod_config.write_text(
        "version: 5\nhierarchy:\n  - {name: c, data_hash: test_data_hash}\n",
        encoding="utf-8",
    )
    script["data_hash"] = lambda path, options: {"m::k": "v", "unqualified": "dropped"}
    h = Hiera(str(base / "hiera.yaml"), basemodulepath=[modules])
    assert h.lookup("m::k") == "v"
    with pytest.raises(KeyNotFoundError):
        h.lookup("unqualified")


@pytest.mark.parametrize(
    "entry, expected",
    [
        (
            {"lookup_key": "test_data_hash"},
            "'test_data_hash' expects 2 arguments, got 3",
        ),
        (
            {"data_hash": "test_lookup_key"},
            "'test_lookup_key' expects 3 arguments, got 2",
        ),
        (
            {"data_dig": "test_lookup_key"},
            "'test_lookup_key' parameter 'key' expects a String value, got Tuple",
        ),
        (
            {"lookup_key": "test_data_dig"},
            "'test_data_dig' parameter 'key_segments' expects an Array value, got String",
        ),
        (
            {"data_hash": "test_none"},
            "'test_none' implements none of data_hash, lookup_key or data_dig",
        ),
    ],
    ids=["dh-as-lk", "lk-as-dh", "lk-as-dd", "dd-as-lk", "none"],
)
def test_kind_mismatch_uses_puppet_text(make_tree, backends, entry, expected):
    # Lazy: the mismatch is only ever raised once the function is actually
    # invoked for a location that exists, not at construction.
    root = make_tree(
        {"hierarchy": [dict({"name": "s", "path": "a.yaml"}, **entry)]},
        files={"data/a.yaml": "x"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(ConfigError, match=re.escape(expected)):
        h.lookup("x")


def test_kind_mismatch_is_not_raised_for_a_missing_location(make_tree, backends):
    # A kind-mismatched level whose own location does not exist must not
    # refuse the whole Hiera instance -- Puppet degrades gracefully and
    # still resolves every key a correctly-configured level can answer.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "bad", "path": "missing.yaml", "lookup_key": "test_data_hash"},
                {"name": "good", "path": "a.yaml"},
            ]
        },
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"


@pytest.mark.parametrize(
    "cls",
    [YAMLBackend, JSONBackend, HOCONBackend, SopsBackend],
    ids=["yaml_data", "json_data", "hocon_data", "sops_data"],
)
def test_file_data_hash_takes_only_path(monkeypatch, cls):
    def boom(*a, **k):
        raise AssertionError("no file read or subprocess before the options check")

    monkeypatch.setattr("builtins.open", boom)
    monkeypatch.setattr("subprocess.run", boom, raising=False)

    backend = cls({}) if cls is not SopsBackend else cls({})
    with pytest.raises(ConfigError, match="one of 'path'"):
        backend.data_hash(None, {})
    with pytest.raises(ConfigError, match="one of 'path'"):
        backend.data_hash("x.yaml", {"foo": "bar"})


# --- eyaml: missing optional dependency -----------------------------------


def test_eyaml_missing_cryptography_hint(make_tree, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "cryptography", None)
    root = make_tree(
        {
            "hierarchy": [
                {
                    "name": "secrets",
                    "lookup_key": "eyaml_lookup_key",
                    "path": "secrets.eyaml",
                    "options": {"pkcs7_private_key": "keys/private_key.pkcs7.pem"},
                }
            ]
        },
        files={"data/secrets.eyaml": "k: v\n"},
    )
    with pytest.raises(BackendError, match=r"hyera\[eyaml\]"):
        Hiera(str(root / "hiera.yaml"))
