"""``hyera.Limits``: opt-in ceilings on YAML alias expansion and glob brace
expansion, enforced before the cost is paid, and the ``Backend.limits`` read.
The HOCON field has its own module."""

import copy
import pickle
import time

import pytest

import hyera
from hyera._config import dir_glob
from hyera.backends import _psych_loader

_CEILING_SECONDS = 2


def _bomb(levels, fan=9):
    lines = ["a0: &a0 [{}]".format(", ".join(["x"] * fan))]
    for i in range(1, levels):
        refs = ", ".join(["*a{}".format(i - 1)] * fan)
        lines.append("a{0}: &a{0} [{1}]".format(i, refs))
    return "\n".join(lines) + "\n"


def _yaml_tree(make_tree, text, **options):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]}, files={"data/c.yaml": text}
    )
    return hyera.Hiera(root / "hiera.yaml", **options), root


# --- the value class -------------------------------------------------------


def test_limits_default_to_unbounded():
    limits = hyera.Limits()
    assert limits.yaml_alias_nodes is None
    assert limits.glob_patterns is None


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"yaml_alias_nodes": 0}, ValueError),
        ({"glob_patterns": -1}, ValueError),
        ({"yaml_alias_nodes": "10"}, TypeError),
        ({"glob_patterns": 1.5}, TypeError),
        ({"glob_patterns": True}, TypeError),
    ],
)
def test_a_field_must_be_a_positive_int_or_none(kwargs, error):
    with pytest.raises(error):
        hyera.Limits(**kwargs)


def test_limits_are_keyword_only_immutable_hashable_and_comparable():
    with pytest.raises(TypeError):
        hyera.Limits(10)  # type: ignore[misc]
    limits = hyera.Limits(yaml_alias_nodes=10, glob_patterns=4)
    with pytest.raises(AttributeError):
        limits.glob_patterns = 5  # type: ignore[misc]
    assert limits == hyera.Limits(yaml_alias_nodes=10, glob_patterns=4)
    assert limits != hyera.Limits(yaml_alias_nodes=10)
    assert len({limits, hyera.Limits(yaml_alias_nodes=10, glob_patterns=4)}) == 1
    assert repr(limits) == (
        "Limits(yaml_alias_nodes=10, glob_patterns=4, hocon_substitution_size=None)"
    )
    for clone in (pickle.loads(pickle.dumps(limits)), copy.deepcopy(limits)):
        assert clone == limits


def test_hiera_rejects_limits_of_the_wrong_type(make_tree):
    root = make_tree({"hierarchy": []})
    with pytest.raises(TypeError, match="limits"):
        hyera.Hiera(root / "hiera.yaml", limits={"yaml_alias_nodes": 5})


# --- YAML alias expansion --------------------------------------------------


def test_an_alias_bomb_is_refused_before_any_node_is_built(make_tree, monkeypatch):
    text = _bomb(7)
    h, root = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=10_000))

    def never(*args, **kwargs):
        raise AssertionError("the loader built nodes for a refused document")

    monkeypatch.setattr(_psych_loader.yaml, "load_all", never)
    started = time.monotonic()
    with pytest.raises(hyera.BackendError) as caught:
        h.lookup("a0")
    assert time.monotonic() - started < _CEILING_SECONDS
    message = str(caught.value)
    assert "limits.yaml_alias_nodes" in message
    assert "10000" in message
    assert "c.yaml" in message


def test_a_document_at_the_limit_loads_as_without_limits(make_tree):
    text = "a: &x [1, 2, 3]\nb: *x\n"
    plain, _ = _yaml_tree(make_tree, text)
    expected = plain.lookup("b")
    for limit in (4, 100):
        h, _ = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=limit))
        assert h.lookup("b") == expected == [1, 2, 3]


def test_one_node_over_the_limit_is_refused(make_tree):
    h, _ = _yaml_tree(
        make_tree,
        "a: &x [1, 2, 3]\nb: *x\n",
        limits=hyera.Limits(yaml_alias_nodes=3),
    )
    with pytest.raises(hyera.BackendError, match="yaml_alias_nodes"):
        h.lookup("b")


def test_aliases_inside_an_anchored_node_count_at_their_expansion(make_tree):
    text = "a: &x [1, 2]\nb: &y [*x, *x]\nc: [*y, *y]\n"
    # *x yields 3 nodes twice inside y (6); *y then yields 1 + 3 + 3 nodes twice (14).
    h, _ = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=20))
    assert h.lookup("c") == [[[1, 2], [1, 2]], [[1, 2], [1, 2]]]
    h, _ = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=19))
    with pytest.raises(hyera.BackendError, match="yaml_alias_nodes"):
        h.lookup("c")


def test_a_merge_key_alias_counts(make_tree):
    text = "base: &b {p: 1, q: 2}\nk:\n  <<: *b\n"
    h, _ = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=5))
    assert h.lookup("k") == {"p": 1, "q": 2}
    h, _ = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=4))
    with pytest.raises(hyera.BackendError, match="yaml_alias_nodes"):
        h.lookup("k")


def test_a_redefined_anchor_takes_the_latest_size(make_tree):
    text = "a: &x [1, 2, 3, 4]\nb: &x [1]\nc: *x\n"
    h, _ = _yaml_tree(make_tree, text, limits=hyera.Limits(yaml_alias_nodes=2))
    assert h.lookup("c") == [1]


def test_without_limits_a_bomb_is_unchanged(make_tree):
    h, _ = _yaml_tree(make_tree, _bomb(3))
    assert len(h.lookup("a2")) == 9


def test_yaml_without_an_alias_is_not_scanned(make_tree, monkeypatch):
    calls = []
    original = _psych_loader._AliasNodes.feed
    monkeypatch.setattr(
        _psych_loader._AliasNodes,
        "feed",
        lambda s, e: calls.append(e) or original(s, e),
    )
    h, _ = _yaml_tree(make_tree, "a: 1\n", limits=hyera.Limits(yaml_alias_nodes=1))
    assert h.lookup("a") == 1
    assert calls == []


def test_a_view_and_a_pickle_keep_the_limits(make_tree):
    h, _ = _yaml_tree(make_tree, _bomb(7), limits=hyera.Limits(yaml_alias_nodes=10_000))
    for clone in (h.scoped(), pickle.loads(pickle.dumps(h)), copy.copy(h)):
        with pytest.raises(hyera.BackendError, match="yaml_alias_nodes"):
            clone.lookup("a0")


# --- glob brace expansion --------------------------------------------------


def test_brace_expansion_stops_at_the_limit_before_building_the_rest():
    pattern = "{a,b}" * 60
    started = time.monotonic()
    with pytest.raises(hyera.BackendError, match="limits.glob_patterns"):
        dir_glob._expand_braces(pattern, 100)
    assert time.monotonic() - started < _CEILING_SECONDS


def test_a_glob_bomb_never_walks_the_filesystem(tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("the walk started for a refused pattern")

    monkeypatch.setattr(dir_glob, "_glob_one", never)
    with pytest.raises(hyera.BackendError, match="limits.glob_patterns"):
        dir_glob.glob(str(tmp_path), "{a,b}" * 14, max_patterns=1000)


def test_a_pattern_at_the_limit_expands_as_without_one(tmp_path):
    (tmp_path / "a.yaml").write_text("k: 1\n", encoding="utf-8")
    pattern = "{a,b}{.yaml,.yml}"
    unlimited = dir_glob.glob(str(tmp_path), pattern)
    assert dir_glob.glob(str(tmp_path), pattern, max_patterns=4) == unlimited
    with pytest.raises(hyera.BackendError):
        dir_glob.glob(str(tmp_path), pattern, max_patterns=3)


def test_a_glob_level_refuses_a_bomb_from_a_scope_value(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "g", "glob": "%{facts.pat}"}]},
        files={"data/a.yaml": "k: 1\n"},
    )
    scope = hyera.Scope(facts={"pat": "{a,b}" * 14 + ".yaml"})
    started = time.monotonic()
    limited = hyera.Hiera(
        root / "hiera.yaml", scope=scope, limits=hyera.Limits(glob_patterns=1000)
    )
    with pytest.raises(hyera.BackendError, match="limits.glob_patterns"):
        limited.lookup("k")
    assert time.monotonic() - started < _CEILING_SECONDS
    ok = hyera.Hiera(
        root / "hiera.yaml",
        scope=hyera.Scope(facts={"pat": "{a,b}.yaml"}),
        limits=hyera.Limits(glob_patterns=2),
    )
    assert ok.lookup("k") == 1


def test_without_limits_a_modest_brace_pattern_still_expands(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "g", "glob": "{a,b}{a,b}{a,b}.yaml"}]},
        files={"data/aaa.yaml": "k: 1\n"},
    )
    assert hyera.Hiera(root / "hiera.yaml").lookup("k") == 1


# --- Backend.limits --------------------------------------------------------


def test_a_backend_reads_the_limits_of_the_read_in_progress(make_tree, monkeypatch):
    seen = []
    monkeypatch.setattr(
        hyera.Backend, "_REGISTRY", copy.deepcopy(hyera.Backend._REGISTRY)
    )

    class Probe(hyera.Backend):
        NAMES = {"function": ("probe_data",)}

        def data_hash(self, path, options):
            seen.append(self.limits)
            return {"k": 1}

    root = make_tree(
        {
            "hierarchy": [{"name": "p", "path": "x.probe"}],
            "defaults": {"data_hash": "probe_data"},
        },
        files={"data/x.probe": ""},
    )
    limits = hyera.Limits(glob_patterns=7)
    plain = hyera.Hiera(root / "hiera.yaml", backends=[Probe])
    assert plain.lookup("k") == 1
    bounded = hyera.Hiera(root / "hiera.yaml", backends=[Probe], limits=limits)
    assert bounded.lookup("k") == 1
    assert seen == [None, limits]
    assert Probe().limits is None
