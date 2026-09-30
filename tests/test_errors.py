"""The exception hierarchy and the lookup miss/error boundary."""

import copy
import pickle
import re

import pytest

import hyera
from hyera import (
    Backend,
    BackendError,
    ConfigError,
    Hiera,
    HieraError,
    HieraLookupError,
    InterpolationError,
    KeyNotFoundError,
    MergeError,
)


def test_hierarchy_and_exports():
    assert issubclass(ConfigError, HieraError)
    assert issubclass(BackendError, HieraError)
    assert issubclass(HieraLookupError, HieraError)
    assert issubclass(InterpolationError, HieraLookupError)
    assert issubclass(MergeError, HieraLookupError)
    assert issubclass(KeyNotFoundError, HieraLookupError)
    assert issubclass(KeyNotFoundError, KeyError)

    names = [
        "HieraError",
        "ConfigError",
        "BackendError",
        "HieraLookupError",
        "InterpolationError",
        "MergeError",
        "KeyNotFoundError",
    ]
    for name in names:
        assert name in hyera.__all__
        assert getattr(hyera, name) is getattr(hyera.exceptions, name)


def test_error_attributes_survive_pickle():
    e = BackendError("x", path="p")
    e2 = pickle.loads(pickle.dumps(e))
    assert e2.path == "p"

    knf = KeyNotFoundError("k")
    knf2 = pickle.loads(pickle.dumps(knf))
    assert knf2.name == "k"
    assert str(knf2) == str(knf)


def test_miss_raises_key_not_found_error(hiera_root):
    hiera = Hiera(str(hiera_root / "hiera.yaml"))
    with pytest.raises(KeyError) as excinfo:
        hiera.lookup("nope::key")
    assert isinstance(excinfo.value, HieraError)
    assert (
        str(excinfo.value)
        == "Function lookup() did not find a value for the name 'nope::key'"
    )
    assert "nope::key" not in hiera


def test_not_found_message_for_name_lists():
    assert str(KeyNotFoundError(["a", "b"])) == (
        "Function lookup() did not find a value for any of the names ['a', 'b']"
    )
    assert (
        str(KeyNotFoundError(["a"]))
        == "Function lookup() did not find a value for the name 'a'"
    )


def test_non_str_key_raises_type_error(hiera_root):
    hiera = Hiera(str(hiera_root / "hiera.yaml"))
    with pytest.raises(TypeError):
        hiera.lookup(5)
    with pytest.raises(TypeError):
        hiera.lookup(None)
    with pytest.raises(TypeError):
        5 in hiera


def test_bad_merge_strategy_raises_merge_error(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "one", "path": "one.yaml"},
                {"name": "two", "path": "two.yaml"},
            ],
        },
        files={
            "data/one.yaml": """\
                k: [a]
                lookup_options:
                  k:
                    merge: bogus
                """,
            "data/two.yaml": """\
                k: [b]
                """,
        },
    )
    hiera = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(MergeError):
        hiera.lookup("k")
    assert hiera.lookup("k", merge="first") == ["a"]


def test_internal_keyerror_is_not_chained(make_tree):
    bad_backend_root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml", "data_hash": "nope"}]},
    )
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(bad_backend_root / "hiera.yaml"))
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True

    # A missing inline `%{alias()}`/`%{hiera()}`/`%{lookup()}` now resolves
    # to "" (interpolation.rb:77-86 -- Puppet's own behavior), so it no
    # longer triggers InterpolationError; an unknown interpolation method
    # does, with no internal exception chained onto it.
    unknown_method_root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={
            "data/one.yaml": """\
                k: "%{bogus('x')}"
                """,
        },
    )
    hiera = Hiera(str(unknown_method_root / "hiera.yaml"))
    with pytest.raises(InterpolationError) as excinfo2:
        hiera.lookup("k")
    assert excinfo2.value.__cause__ is None
    assert excinfo2.value.__context__ is None


def test_missing_config_raises_config_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(missing))
    assert excinfo.value.path.endswith("nope.yaml")
    assert isinstance(excinfo.value.__cause__, FileNotFoundError)
    assert "No such file or directory" in str(excinfo.value)


def test_directory_config_raises_config_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={"data/one.yaml": "k: v\n"},
    )
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(root / "data"))
    assert "Is a directory" in str(excinfo.value)


def test_unparsable_config_raises_config_error(make_tree):
    root = make_tree(
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data\n"
        "hierarchy:\n"
        "  - {name: c, path: common.yaml}\n",
        raw=True,
    )
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(root / "hiera.yaml"))
    assert isinstance(excinfo.value.__cause__, BackendError)
    assert "\n" not in str(excinfo.value)
    assert "<byte string>" not in str(excinfo.value)
    assert re.search(
        r"\(.*hiera\.yaml\): .*while parsing a flow mapping at line 2 column 11$",
        str(excinfo.value),
    )


def test_non_mapping_config_falls_back_to_v3_default(make_tree, caplog):
    # A hiera.yaml that parses but is not a YAML hash falls back to Puppet's
    # Hiera version 3 default config (`hiera_config.rb:139-144`), which is
    # itself schema-valid -- construction succeeds (Puppet's own AIO
    # default codedir has no matching data file at rest here, so the key
    # is simply not found, not a `ConfigError`). The fallback's own
    # warning is what this test actually pins.
    root = make_tree("- a\n- b\n", raw=True)
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(KeyNotFoundError):
        h.lookup("k")
    assert any(
        "does not contain a valid YAML hash" in r.message for r in caplog.records
    )
    assert any(
        "does not contain a valid YAML hash" in r.message for r in caplog.records
    )


@pytest.mark.parametrize(
    "hiera_yaml",
    [
        "version: 5\nhierarchy: [common.yaml]\n",
        "version: 5\nhierarchy: {a: 1}\n",
        (
            "version: 5\nhierarchy:\n"
            "  - {name: one, data_hash: yaml_data, mapped_paths: [a, b]}\n"
        ),
        (
            "version: 5\ndefaults: {datadir: 5}\nhierarchy:\n"
            "  - {name: one, path: one.yaml, data_hash: yaml_data}\n"
        ),
    ],
    ids=[
        "hierarchy-list-of-strings",
        "hierarchy-dict",
        "mapped-paths-2-tuple",
        "int-datadir",
    ],
)
def test_config_shape_raises_config_error(make_tree, hiera_yaml):
    root = make_tree(hiera_yaml)
    with pytest.raises(ConfigError) as excinfo:
        Hiera(str(root / "hiera.yaml"))
    assert str(excinfo.value).startswith("The Lookup Configuration at '")


def test_data_parse_error_raises_backend_error(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "c", "path": "common.yaml"},
                {"name": "o", "path": "other.yaml"},
            ],
        },
        files={
            "data/common.yaml": "good: yes\n",
            "data/other.yaml": "k: [unclosed\nz: 2\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as excinfo:
        h.lookup("anything")
    assert not isinstance(excinfo.value, ConfigError)
    assert excinfo.value.path.endswith("other.yaml")
    assert str(excinfo.value).startswith("Unable to parse (")
    assert len(str(excinfo.value).splitlines()) == 1
    assert re.search(
        r"^Unable to parse \(.*other\.yaml\): .*while parsing a flow sequence "
        r"at line 1 column 4$",
        str(excinfo.value),
    )


def test_json_parse_error_names_file(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "j", "path": "first.json", "data_hash": "json_data"}]},
        files={"data/first.json": ""},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as excinfo:
        h.lookup("anything")
    assert re.search(
        r"^Unable to parse \(.*first\.json\): Expecting value at line 1 column 1$",
        str(excinfo.value),
    )


def test_yaml_control_character_names_position_not_line_column(make_tree):
    # A raw control character is a yaml.reader.ReaderError, not a
    # yaml.MarkedYAMLError -- _yaml_problem's own "at position N" shape,
    # distinct from every other yaml parse error in this file (all "at
    # line L column C").
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": b"k: \x01value\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as excinfo:
        h.lookup("k")
    assert re.search(
        r"^Unable to parse \(.*c\.yaml\): unacceptable character #x0001: "
        r"control characters are not allowed at position 3$",
        str(excinfo.value),
    )


def test_backend_exception_wrapped_with_path(make_tree, monkeypatch):
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))

    class BrokenBackend(Backend):
        NAMES = {"function": ("broken_data",)}

        def loads(self, text):
            raise ValueError("boom")

    root = make_tree(
        {
            "hierarchy": [
                {"name": "one", "path": "one.yaml", "data_hash": "broken_data"}
            ],
        },
        files={"data/one.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), backends=[BrokenBackend])
    with pytest.raises(BackendError) as excinfo:
        h.lookup("anything")
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert excinfo.value.path is not None


def test_data_file_errors_surface_on_lookup(make_tree):
    # Puppet reads data only inside a lookup (`hiera_config.rb:127` builds
    # the config without touching data; `data_hash_function_provider.rb`
    # reads a location only when a lookup reaches it). `Hiera(...)` must
    # not read `common.yaml` at all, so a malformed data file cannot fail
    # construction -- it fails the first lookup that reaches it, every
    # time, not only once.
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "a: [\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))

    with pytest.raises(BackendError) as excinfo:
        h.lookup("k")
    assert str(excinfo.value.path).endswith("common.yaml")

    with pytest.raises(BackendError):
        h.lookup("k")
