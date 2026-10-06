"""``hyera._output.explain``: the ported ``explainer.rb`` tree and its text
rendering. No engine code is exercised here -- every tree is built by hand
with direct ``push``/``accept_*`` calls, the same primitives a lookup's
``Invocation`` will call once the hooks land.

Tests 1-3 replay the literal event sequence a real lookup produces against
``tests/conformance/cases/lookup-explain-basic/golden.json``, recorded from
Puppet 8.10 itself -- so a mismatch here means the renderer, not the
fixture, is wrong.
"""

import collections
import json
from pathlib import Path

import pytest

from hyera._output.explain import Explainer, _Location, _Node, _Top
from hyera._output.explain_refs import _dump_value, _LocationRef, _ProviderRef

_GOLDEN = json.loads(
    (
        Path(__file__).resolve().parent
        / "conformance"
        / "cases"
        / "lookup-explain-basic"
        / "golden.json"
    ).read_text(encoding="utf-8")
)
_RESULTS = _GOLDEN["results"]

#: A merge qualifier duck-typing Puppet's ``MergeStrategy``: a ``KEY`` class
#: attribute and an ``options`` dict attribute (never ``None``).
_FakeMerge = collections.namedtuple("_FakeMerge", ["KEY", "options"])

_GLOBAL = "Global Data Provider (hiera configuration version 5)"
_HIERA_YAML = "<case>/hiera.yaml"


def _location(original, path):
    return _LocationRef(original, path, "path")


def _build_lookup_options_search(e):
    """The ``h``/``greet``/``arr-options`` fixture's shared meta search:
    ``lookup_options`` found via a hash merge on the ``common`` entry, with
    ``os``/``multi`` contributing nothing and ``missing`` absent."""
    e.push("meta", "lookup_options")
    e.push("data_provider", _ProviderRef(_GLOBAL, _HIERA_YAML))
    e.push("merge", _FakeMerge("hash", {}))
    e.push("data_provider", _ProviderRef('Hierarchy entry "os"'))
    e.push(
        "location",
        _location("os/%{facts.os.family}.yaml", "<case>/data/os/RedHat.yaml"),
    )
    e.accept_not_found("lookup_options")
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "missing"'))
    e.push(
        "location", _location("nodes/%{facts.nothere}.yaml", "<case>/data/nodes/.yaml")
    )
    e.accept_location_not_found()
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "multi"'))
    e.push("merge", _FakeMerge("hash", {}))
    e.push("location", _location("a.yaml", "<case>/data/a.yaml"))
    e.accept_not_found("lookup_options")
    e.pop()
    e.push("location", _location("b.yaml", "<case>/data/b.yaml"))
    e.accept_location_not_found()
    e.pop()
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "common"'))
    e.push("location", _location("common.yaml", "<case>/data/common.yaml"))
    lopts = {
        "arr": {"merge": "unique"},
        "^h$": {"merge": {"strategy": "deep", "knockout_prefix": "--"}},
        "num": {"convert_to": "Integer"},
    }
    e.accept_found("lookup_options", lopts)
    e.pop()
    e.pop()
    e.accept_result(lopts)
    e.pop()
    e.pop()
    e.pop()
    return lopts


def _assert_matches(e, result_id):
    want = _RESULTS[result_id]
    assert e.to_hash() == want["tree"]
    assert e.explain().splitlines() == want["text"]


def test_h_report_matches_golden():
    """A hash merge over four entries (one a nested ``multi`` sub-merge,
    two contributing nothing) for the meta ``lookup_options`` search, then
    a deep merge with ``knockout_prefix`` for the real ``h`` search, with a
    merge-source line between the two searches."""
    e = Explainer()
    _build_lookup_options_search(e)
    e.accept_merge_source("lookup_options")

    e.push("data", "h")
    e.push("data_provider", _ProviderRef(_GLOBAL, _HIERA_YAML))
    e.push("merge", _FakeMerge("deep", {"knockout_prefix": "--"}))
    e.push("data_provider", _ProviderRef('Hierarchy entry "os"'))
    e.push(
        "location",
        _location("os/%{facts.os.family}.yaml", "<case>/data/os/RedHat.yaml"),
    )
    e.accept_found("h", {"a": 1, "n": {"x": 1}})
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "missing"'))
    e.push(
        "location", _location("nodes/%{facts.nothere}.yaml", "<case>/data/nodes/.yaml")
    )
    e.accept_location_not_found()
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "multi"'))
    e.push("merge", _FakeMerge("deep", {"knockout_prefix": "--"}))
    e.push("location", _location("a.yaml", "<case>/data/a.yaml"))
    e.accept_not_found("h")
    e.pop()
    e.push("location", _location("b.yaml", "<case>/data/b.yaml"))
    e.accept_location_not_found()
    e.pop()
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "common"'))
    e.push("location", _location("common.yaml", "<case>/data/common.yaml"))
    e.accept_found("h", {"b": 2, "n": {"y": 2}})
    e.pop()
    e.pop()
    e.accept_result({"b": 2, "n": {"y": 2, "x": 1}, "a": 1})
    e.pop()
    e.pop()
    e.pop()

    _assert_matches(e, "h")


def test_greet_report_matches_golden():
    """Interpolation on a bare ``%{facts.os.family}``: a ``Global Scope``
    node for the root ``facts`` read, then a ``sub_key``/``segment`` walk
    for ``os.family``."""
    e = Explainer()
    _build_lookup_options_search(e)

    e.push("data", "greet")
    e.push("data_provider", _ProviderRef(_GLOBAL, _HIERA_YAML))
    e.push("data_provider", _ProviderRef('Hierarchy entry "os"'))
    e.push(
        "location",
        _location("os/%{facts.os.family}.yaml", "<case>/data/os/RedHat.yaml"),
    )
    e.push("interpolate", "hi %{facts.os.family}")
    e.push("scope", "Global Scope")
    e.accept_found("facts", {"os": {"family": "RedHat"}})
    e.pop()
    e.push("sub_lookup", ["os", "family"])
    e.push("segment", "os")
    e.accept_found("os", {"family": "RedHat"})
    e.pop()
    e.push("segment", "family")
    e.accept_found("family", "RedHat")
    e.pop()
    e.pop()
    e.pop()
    e.accept_found("greet", "hi RedHat")
    e.pop()
    e.pop()
    e.pop()
    e.pop()

    _assert_matches(e, "greet")


def test_options_only_report_matches_golden():
    """``Explainer(True, True)``: no root ``Searching for`` node at all --
    the provider/merge stack is pushed directly under the explainer, so
    ``to_hash()`` collapses to that one ``data_provider`` object."""
    e = Explainer(True, True)
    lopts = _build_lookup_options_search
    e.push("data_provider", _ProviderRef(_GLOBAL, _HIERA_YAML))
    e.push("merge", _FakeMerge("hash", {}))
    e.push("data_provider", _ProviderRef('Hierarchy entry "os"'))
    e.push(
        "location",
        _location("os/%{facts.os.family}.yaml", "<case>/data/os/RedHat.yaml"),
    )
    e.accept_not_found("lookup_options")
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "missing"'))
    e.push(
        "location", _location("nodes/%{facts.nothere}.yaml", "<case>/data/nodes/.yaml")
    )
    e.accept_location_not_found()
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "multi"'))
    e.push("merge", _FakeMerge("hash", {}))
    e.push("location", _location("a.yaml", "<case>/data/a.yaml"))
    e.accept_not_found("lookup_options")
    e.pop()
    e.push("location", _location("b.yaml", "<case>/data/b.yaml"))
    e.accept_location_not_found()
    e.pop()
    e.pop()
    e.pop()
    e.push("data_provider", _ProviderRef('Hierarchy entry "common"'))
    e.push("location", _location("common.yaml", "<case>/data/common.yaml"))
    value = {
        "arr": {"merge": "unique"},
        "^h$": {"merge": {"strategy": "deep", "knockout_prefix": "--"}},
        "num": {"convert_to": "Integer"},
    }
    e.accept_found("lookup_options", value)
    e.pop()
    e.pop()
    e.accept_result(value)
    e.pop()
    e.pop()

    _assert_matches(e, "arr-options")
    # Collapsed to the data_provider node itself, never wrapped in a root.
    assert e.to_hash()["type"] == "data_provider"


def test_single_branch_merge_is_transparent():
    """A merge node with exactly one branch prints and serializes as that
    branch alone -- no ``Merge strategy`` header, no wrapping hash."""
    leaf = Explainer()
    leaf.push("location", _location("a.yaml", "<case>/data/a.yaml"))
    leaf.accept_found("k", 1)
    leaf.pop()

    wrapped = Explainer()
    wrapped.push("merge", _FakeMerge("unique", {}))
    wrapped.push("location", _location("a.yaml", "<case>/data/a.yaml"))
    wrapped.accept_found("k", 1)
    wrapped.pop()
    wrapped.accept_result(1)
    wrapped.pop()

    assert wrapped.to_hash() == leaf.to_hash()
    assert wrapped.explain() == leaf.explain()
    assert "Merge strategy" not in wrapped.explain()


def test_empty_merge_prints_nothing():
    """A merge node with zero branches (Puppet: ``return if branches.size
    == 0``) contributes nothing to the text at all."""
    e = Explainer()
    e.push("merge", _FakeMerge("unique", {}))
    e.pop()

    assert e.explain() == ""
    assert e.to_hash() == {"merge": "unique", "type": "merge"}


def test_dump_value_layout():
    def dump(value, indent=""):
        parts = []
        _dump_value(parts, indent, value)
        return "".join(parts)

    assert dump({}) == "{}"
    assert dump([]) == "[]"
    assert dump([{"a": 1}]) == '[\n  {\n    "a" => 1\n  }\n]'
    assert dump(None) == "nil"
    assert dump(True) == "true"
    assert dump(1e15) == "1.0e+15"
    assert dump('a"b') == '"a\\"b"'


def test_found_in_overrides_and_defaults_suffixes():
    overrides = Explainer()
    overrides.push("scope", "Overrides")
    overrides.accept_found_in_overrides("x", "o")
    overrides.pop()
    assert (
        overrides.explain() == 'Overrides\n  Found key: "x" value: "o" in overrides\n'
    )
    assert overrides.to_hash()["event"] == "found_in_overrides"

    defaults = Explainer()
    defaults.push("scope", "Defaults")
    defaults.accept_found_in_defaults("y", "d")
    defaults.pop()
    assert defaults.explain() == 'Defaults\n  Found key: "y" value: "d" in defaults\n'
    assert defaults.to_hash()["event"] == "found_in_defaults"


def test_uri_location_labels_and_keys():
    e = Explainer()
    e.push("location", _LocationRef("https://x/a.yaml", "https://x/a.yaml", "uri"))
    e.accept_location_not_found()
    e.pop()

    text = e.explain()
    assert 'URI "https://x/a.yaml"' in text
    assert 'Original uri: "https://x/a.yaml"' in text
    assert "URI not found" in text
    assert "Path" not in text

    hash_ = e.to_hash()
    assert hash_["type"] == "uri"
    assert hash_["original_uri"] == "https://x/a.yaml"
    assert hash_["uri"] == "https://x/a.yaml"
    assert "path" not in hash_ and "original_path" not in hash_


def test_module_nodes():
    not_found = Explainer()
    not_found.accept_module_not_found("nomod")
    assert not_found.explain() == 'Module "nomod" not found\n'
    assert not_found.to_hash() == {"event": "module_not_found", "type": "module"}

    provider_not_found = Explainer()
    provider_not_found.accept_module_provider_not_found("plain")
    assert (
        provider_not_found.explain()
        == 'Module data provider for module "plain" not found\n'
    )
    assert provider_not_found.to_hash() == {
        "event": "module_provider_not_found",
        "type": "module",
    }


def test_invalid_key_and_root_texts():
    e = Explainer()
    e.push("invalid_key", "lookup_options")
    e.pop()
    e.accept_text(
        "Function lookup() did not find a value for the name 'lookup_options'"
    )

    text = e.explain()
    assert text == (
        'Invalid key "lookup_options"\n'
        "Function lookup() did not find a value for the name 'lookup_options'\n"
    )
    assert "texts" not in e.to_hash()  # root texts never reach to_hash


def test_data_provider_config_and_module_keys():
    with_config = Explainer()
    with_config.push("data_provider", _ProviderRef(_GLOBAL, _HIERA_YAML))
    with_config.accept_not_found("k")
    with_config.pop()
    assert 'Using configuration "<case>/hiera.yaml"' in with_config.explain()
    assert with_config.to_hash()["configuration_path"] == _HIERA_YAML

    without_config = Explainer()
    without_config.push("data_provider", _ProviderRef('Hierarchy entry "os"'))
    without_config.accept_not_found("k")
    without_config.pop()
    assert "Using configuration" not in without_config.explain()
    assert "configuration_path" not in without_config.to_hash()

    module_provider = Explainer()
    module_provider.push(
        "data_provider",
        _ProviderRef('Module "m" Data Provider', "<case>/m/hiera.yaml", "m"),
    )
    module_provider.accept_not_found("m::k")
    module_provider.pop()
    assert module_provider.to_hash()["module"] == "m"


def test_to_hash_key_order():
    path_node = _Location(None, _location("a.yaml", "<case>/data/a.yaml"))
    path_node.found("k", 1)
    assert list(path_node.to_hash()) == [
        "key",
        "value",
        "event",
        "type",
        "original_path",
        "path",
    ]

    root = _Top(None, "data", "k")
    root.branches.append(path_node)
    assert list(root.to_hash()) == ["branches", "key", "type"]


def test_bare_node_text_and_dump_on():
    """``_Node`` (``ExplainNode``) is the base every concrete tree node
    overrides ``dump_on`` on top of -- no engine code path ever builds a
    bare instance -- but it is still real, documented behavior (a second
    ``text()`` call appends rather than resetting the queue), so exercise
    it directly the same way this file already builds every other node by
    hand."""
    node = _Node()
    node.text("first")
    node.text("second")
    parts = []
    node.dump_on(parts, "  ", "")
    assert "".join(parts) == "  first\n  second\n"


def test_texts_reach_to_hash_on_a_tree_node():
    """Unlike the root (``test_invalid_key_and_root_texts``), a queued
    ``text()`` on an ordinary ``_TreeNode`` *does* reach ``to_hash()``."""
    e = Explainer()
    e.push("scope", "Global Scope")
    e.accept_text("a note")
    e.accept_not_found("x")
    e.pop()

    hash_ = e.to_hash()
    assert hash_["texts"] == ["a note"]
    assert e.explain() == 'Global Scope\n  No such key: "x"\n  a note\n'


def test_module_node_without_event_is_silent():
    """A ``_Module`` node only ever gets an event through
    ``accept_module_not_found``/``accept_module_provider_not_found``
    (both push, set the event, then pop in one go); pushed directly with
    neither called, its ``if``/``elif`` (no ``else``) matches
    ``ExplainModule#dump_on`` and renders nothing at all."""
    e = Explainer()
    e.push("module", "x")
    e.pop()

    assert e.explain() == ""
    assert e.to_hash() == {"type": "module"}


def test_push_rejects_unknown_kind():
    e = Explainer()
    with pytest.raises(ValueError, match="Unknown Explain type bogus"):
        e.push("bogus", None)


def test_pop_at_root_is_a_noop():
    """``pop()`` with nothing pushed matches Puppet's ``Explainer#pop``,
    a no-op when already at the root."""
    e = Explainer()
    e.pop()
    assert e.current is e
