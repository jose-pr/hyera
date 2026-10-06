"""The self-registering ``Backend`` registry itself: namespaces,
registration errors, lookup, and the base class's default behavior."""

import copy
import re
import sys

import pytest

from hyera.backends import Backend, HOCONBackend, NamePattern, default_backends


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Every test gets its own copy of the process-global registry, so a
    throwaway class defined in a test can never collide with another test
    or with the real built-ins."""
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))


def test_namespaces_are_kept_apart():
    class Probe(Backend):
        NAMES = {"function": ("probe_data",), "format": ("probe",)}

        def loads(self, text):
            return {}

    assert Backend.find("probe_data", kind="function") is Probe
    assert Backend.find("probe_data", kind="format") is None
    assert Backend.find("probe", kind="format") is Probe
    assert Backend.find("probe", kind="function") is None


def test_duplicate_exact_name_raises():
    class First(Backend):
        NAMES = {"function": ("dup_data",)}

        def loads(self, text):
            return {}

    with pytest.raises(ValueError, match="dup_data"):

        class Second(Backend):
            NAMES = {"function": ("dup_data",)}

            def loads(self, text):
                return {}


def test_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown"):

        class Bad(Backend):
            NAMES = {"nosuchkind": ("x",)}


def test_render_without_dumps_raises_type_error():
    with pytest.raises(TypeError, match="dumps"):

        class BadRender(Backend):
            NAMES = {"render": ("badrender",)}


def test_format_without_loads_raises_type_error():
    with pytest.raises(TypeError, match="loads"):

        class BadFormat(Backend):
            NAMES = {"format": ("badformat",)}


def test_name_pattern_captures_reach_init_and_exact_beats_pattern():
    class PatternBackend(Backend):
        NAMES = {
            "function": (
                "exact_data",
                NamePattern("probe_<x|y>", re.compile(r"probe_(?P<letter>x|y)")),
            )
        }

        def __init__(self, conf=None, *, strict=None, letter=None):
            super().__init__(conf, strict=strict)
            self.letter = letter

        def loads(self, text):
            return {}

    exact = Backend.new("exact_data", {})
    assert exact.letter is None
    assert exact.name == "exact_data"

    matched = Backend.new("probe_x", {})
    assert matched.letter == "x"
    assert matched.name == "probe_x"

    # An exact name beats a pattern that would also match it.
    assert Backend.find("exact_data") is PatternBackend
    assert Backend.find("probe_z") is None
    assert "probe_<x|y>" in Backend.names("function")


def test_default_name_skips_a_leading_pattern():
    # Every built-in backend lists an exact name before any pattern, so _default_name's
    # "skip a pattern, keep looking" step needs a class (a third-party one may order
    # NAMES either way) that puts the pattern first.
    class PatternFirst(Backend):
        NAMES = {
            "function": (
                NamePattern("pf_<x|y>", re.compile(r"pf_(?P<letter>x|y)")),
                "pf_exact",
            )
        }

        def loads(self, text):
            return {}

    assert PatternFirst().name == "pf_exact"


def test_names_lists_exact_then_pattern_display():
    class A(Backend):
        NAMES = {"function": ("a_data",)}

        def loads(self, text):
            return {}

    class B(Backend):
        NAMES = {
            "function": (
                NamePattern("b_<n>", re.compile(r"b_(?P<n>\d+)")),
                "b_data",
            )
        }

        def loads(self, text):
            return {}

    names = Backend.names("function")
    # Exact names (in registration order) all precede every pattern display.
    assert names.index("a_data") < names.index("b_data") < names.index("b_<n>")


def test_for_path_longest_suffix_match_case_sensitive():
    class Foo(Backend):
        NAMES = {"format": ("foo",)}
        EXTENSIONS = (".probeext",)

        def loads(self, text):
            return {}

    class Bar(Backend):
        NAMES = {"format": ("bar",)}
        EXTENSIONS = (".special.probeext",)

        def loads(self, text):
            return {}

    assert Backend.for_path("data.probeext") is Foo
    assert Backend.for_path("data.special.probeext") is Bar
    assert Backend.for_path("data.PROBEEXT") is None
    assert Backend.for_path("data.nosuchext") is None


def test_implements_is_derived_from_overrides():
    class OnlyLoads(Backend):
        NAMES = {"format": ("onlyloads",)}

        def loads(self, text):
            return {}

    assert OnlyLoads.implements("loads") is True
    assert OnlyLoads.implements("load") is True
    assert OnlyLoads.implements("data_hash") is True
    assert OnlyLoads.implements("dumps") is False
    assert OnlyLoads.implements("dump") is False
    assert OnlyLoads.implements("lookup_key") is False
    assert OnlyLoads.implements("data_dig") is False
    with pytest.raises(ValueError):
        OnlyLoads.implements("nosuchop")


def test_base_hooks_raise_not_implemented():
    class Empty(Backend):
        pass

    backend = Empty()
    with pytest.raises(NotImplementedError):
        backend.loads("x")
    with pytest.raises(NotImplementedError):
        backend.dumps({})
    with pytest.raises(NotImplementedError):
        backend.lookup_key("k", {}, {})
    with pytest.raises(NotImplementedError):
        backend.data_dig(["k"], {}, {})


def test_strict_validation():
    Backend(strict="error")
    Backend(strict="warning")
    Backend(strict="off")
    with pytest.raises(ValueError):
        Backend(strict="bogus")


def test_strict_property_falls_back_to_call_time_default():
    assert Backend().strict == "warning"
    assert Backend(strict="error").strict == "error"


def test_get_raises_for_unknown_name_listing_known():
    with pytest.raises(Exception, match="yaml_data"):
        Backend.get("nonexistent_data")


def test_get_returns_the_available_class():
    # get()'s own success path (check_available() then return), distinct
    # from the unknown-name failure case above -- not called from anywhere
    # else in this codebase, but part of the registry's own public API.
    from hyera.backends import YAMLBackend

    assert Backend.get("yaml_data") is YAMLBackend


def test_new_raises_for_unknown_name_listing_known():
    # new()'s own "unknown name" raise, a separate code path from get()'s.
    with pytest.raises(Exception, match="yaml_data"):
        Backend.new("nonexistent_data")


def test_duplicate_pattern_raises():
    class First(Backend):
        NAMES = {"function": (NamePattern("dup_<n>", re.compile(r"dup_(?P<n>\d+)")),)}

        def loads(self, text):
            return {}

    with pytest.raises(ValueError, match="dup_<n>"):

        class Second(Backend):
            NAMES = {
                "function": (NamePattern("dup_<n>", re.compile(r"dup_(?P<n>\d+)")),)
            }

            def loads(self, text):
                return {}


def test_default_backends_dedups_a_class_registered_under_two_patterns():
    # default_backends()'s pattern loop skips a class already seen, normally possible
    # only via the exact-name loop; a backend with two distinct patterns for one class
    # exercises that dedup.
    class TwoPatterns(Backend):
        NAMES = {
            "function": (
                NamePattern("tp_a_<n>", re.compile(r"tp_a_(?P<n>\d+)")),
                NamePattern("tp_b_<n>", re.compile(r"tp_b_(?P<n>\d+)")),
            )
        }

        def loads(self, text):
            return {}

    assert default_backends().count(TwoPatterns) == 1


def test_hocon_check_available_names_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyhocon", None)
    with pytest.raises(Exception) as excinfo:
        HOCONBackend.check_available()
    assert "pyhocon" in str(excinfo.value)
    assert "hyera[hocon]" in str(excinfo.value)


def test_exact_name_answered_by_a_registered_pattern_raises():
    assert Backend.find("sops_yaml") is not None

    with pytest.raises(ValueError, match="sops_yaml.*SopsBackend.*Hijack"):

        class Hijack(Backend):
            NAMES = {"function": ("sops_yaml",)}

    assert Backend.find("sops_yaml").__name__ == "SopsBackend"


def test_pattern_matching_a_registered_exact_name_raises():
    with pytest.raises(ValueError, match="yaml_data.*YAMLBackend.*Greedy"):

        class Greedy(Backend):
            NAMES = {
                "function": (NamePattern("any_<x>", re.compile(r"(?P<x>[a-z]+)_data")),)
            }

    assert Backend.find("yaml_data").__name__ == "YAMLBackend"


def test_one_class_cannot_register_a_name_and_a_pattern_that_overlap():
    with pytest.raises(ValueError, match="dup_1.*Both"):

        class Both(Backend):
            NAMES = {
                "function": (
                    "dup_1",
                    NamePattern("dup_<n>", re.compile(r"dup_(?P<n>\d+)")),
                )
            }

    with pytest.raises(ValueError, match="dup_1.*Both"):

        class Both(Backend):  # noqa: F811
            NAMES = {
                "function": (
                    NamePattern("dup_<n>", re.compile(r"dup_(?P<n>\d+)")),
                    "dup_1",
                )
            }


def test_the_same_name_in_another_kind_is_not_a_clash():
    class Elsewhere(Backend):
        NAMES = {"format": ("sops_yaml",)}

        def loads(self, text):
            return {}

    assert Backend.find("sops_yaml", kind="format") is Elsewhere
    assert Backend.find("sops_yaml").__name__ == "SopsBackend"


def test_default_backends_returns_the_five_built_in_classes():
    assert [cls.__name__ for cls in default_backends()] == [
        "YAMLBackend",
        "JSONBackend",
        "HOCONBackend",
        "SopsBackend",
        "EyamlBackend",
    ]
