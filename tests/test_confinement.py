"""``Hiera(confine_locations=True)`` keeps data file locations inside each level's
datadir: an escape is absent, never opened, warned about once and shown by
``explain()`` as a path not found."""

import copy
import logging
import os
import pickle
import subprocess

import pytest

import hyera
from hyera._config import confinement

SECRET = {"leak": "from-outside"}
FILES = {
    "data/common.yaml": "inside: yes\n",
    "data/nodes/placeholder.yaml": "x: 1\n",
    "data/real/f.yaml": "linked: inside\n",
    "other/secret.yaml": "leak: from-outside\n",
}


def _dir_link(link, target):
    """A directory symlink on POSIX, a junction on Windows (no privilege
    needed); skips the test when the platform refuses either."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
            )
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip("cannot create a directory link here: {}".format(e))


def _build(make_tree, entry, facts=None, defaults=None, **options):
    root = make_tree({"hierarchy": [entry], "defaults": defaults or {}}, files=FILES)
    scope = hyera.Scope(facts=facts or {})
    return root, hyera.Hiera(root / "hiera.yaml", scope=scope, **options)


def _read(h, key="leak"):
    return h.lookup(key, None, None, "ABSENT")


@pytest.mark.parametrize(
    "entry, facts",
    [
        (
            {"name": "n", "path": "%{facts.x}.yaml"},
            {"x": "../other/secret"},
        ),
        (
            {"name": "n", "paths": ["nodes/%{facts.x}.yaml"]},
            {"x": "../../other/secret"},
        ),
        (
            {"name": "n", "mapped_paths": ["facts.names", "n", "%{n}.yaml"]},
            {"names": ["../other/secret"]},
        ),
    ],
    ids=["path-dotdot", "paths-dotdot", "mapped_paths"],
)
def test_a_climbing_location_is_read_off_and_absent_on(make_tree, entry, facts):
    _, off = _build(make_tree, copy.deepcopy(entry), facts)
    assert _read(off) == "from-outside"
    _, on = _build(make_tree, copy.deepcopy(entry), facts, confine_locations=True)
    assert _read(on) == "ABSENT"


def test_an_absolute_location_is_read_off_and_absent_on(make_tree, tmp_path):
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    facts = {"x": (tmp_path / "other" / "secret").as_posix()}
    _, off = _build(make_tree, dict(entry), facts)
    assert _read(off) == "from-outside"
    _, on = _build(make_tree, dict(entry), facts, confine_locations=True)
    assert _read(on) == "ABSENT"


def test_a_link_pointing_out_is_read_off_and_absent_on(make_tree):
    entry = {"name": "n", "path": "lnk/secret.yaml"}
    root, off = _build(make_tree, dict(entry))
    _dir_link(root / "data" / "lnk", root / "other")
    assert _read(off) == "from-outside"
    on = hyera.Hiera(root / "hiera.yaml", confine_locations=True)
    assert _read(on) == "ABSENT"


def test_a_glob_match_through_a_link_out_is_absent_on(make_tree):
    entry = {"name": "n", "glob": "lnk/*.yaml"}
    root, off = _build(make_tree, dict(entry))
    _dir_link(root / "data" / "lnk", root / "other")
    assert _read(off) == "from-outside"
    on = hyera.Hiera(root / "hiera.yaml", confine_locations=True)
    assert _read(on) == "ABSENT"


def test_a_link_that_stays_inside_is_allowed(make_tree):
    entry = {"name": "n", "path": "lnk/f.yaml"}
    root, _ = _build(make_tree, dict(entry))
    _dir_link(root / "data" / "lnk", root / "data" / "real")
    on = hyera.Hiera(root / "hiera.yaml", confine_locations=True)
    assert _read(on, "linked") == "inside"


@pytest.fixture
def scandirs(monkeypatch):
    """Every directory ``os.scandir`` is asked to list."""
    seen = []
    real = os.scandir

    def recording(path="."):
        seen.append(os.path.normcase(os.path.abspath(os.fspath(path))))
        return real(path)

    monkeypatch.setattr(os, "scandir", recording)
    return seen


def _listed_outside(seen, root):
    """The listed directories that are the tree's ``other`` directory or under it."""
    other = os.path.normcase(str(root / "other"))
    return [d for d in seen if d == other or d.startswith(other + os.sep)]


ESCAPING_GLOBS = [
    pytest.param("%{facts.x}/*.yaml", "../other", id="dotdot-in-the-prefix"),
    pytest.param("%{facts.x}/*.yaml", "OTHER", id="absolute"),
    pytest.param("*/../../other/*.yaml", "", id="dotdot-after-a-wildcard"),
    pytest.param("{..,real}/../../other/*.yaml", "", id="dotdot-in-a-brace"),
    pytest.param(
        "%{facts.x}*/*.yaml", "../oth", id="dotdot-then-a-wildcard-in-one-segment"
    ),
]


def _escaping_x(root, x):
    return (root / "other").as_posix() if x == "OTHER" else x


@pytest.mark.parametrize("pattern, x", ESCAPING_GLOBS)
def test_a_glob_that_leaves_the_datadir_lists_nothing_outside_it(
    make_tree, scandirs, pattern, x
):
    entry = {"name": "n", "glob": pattern}
    root = make_tree({"hierarchy": [entry], "defaults": {}}, files=FILES)
    facts = {"x": _escaping_x(root, x)}
    off = hyera.Hiera(root / "hiera.yaml", scope=hyera.Scope(facts=facts))
    on = hyera.Hiera(
        root / "hiera.yaml", scope=hyera.Scope(facts=facts), confine_locations=True
    )
    del scandirs[:]
    assert _read(on) == "ABSENT"
    assert _listed_outside(scandirs, root) == []
    assert _read(off) == "from-outside"
    assert _listed_outside(scandirs, root) != []


def test_a_refused_glob_is_warned_about_once(make_tree, caplog):
    entry = {"name": "n", "glob": "%{facts.x}/*.yaml"}
    _, on = _build(make_tree, entry, {"x": "../other"}, confine_locations=True)
    with caplog.at_level(logging.WARNING, logger="hyera.core"):
        for _ in range(3):
            assert _read(on) == "ABSENT"
    ours = [r for r in caplog.records if "confine_locations" in r.getMessage()]
    assert len(ours) == 1


@pytest.mark.parametrize("pattern", ["real/*.yaml", "real/../real/*.yaml", "*/*.yaml"])
def test_a_glob_that_stays_inside_the_datadir_is_walked(make_tree, pattern):
    _, on = _build(make_tree, {"name": "n", "glob": pattern}, confine_locations=True)
    assert _read(on, "linked") == "inside"


def _paths_level(entry):
    return hyera.HieraLevel.new(entry, hyera.YAMLBackend())


@pytest.mark.parametrize(
    "entry, x",
    [
        ({"name": "n", "path": "%{facts.x}.yaml"}, "../other/secret"),
        ({"name": "n", "glob": "%{facts.x}/*.yaml"}, "../other"),
    ],
    ids=["path", "glob"],
)
def test_level_paths_confine_drops_what_the_engine_drops(make_tree, scandirs, entry, x):
    root = make_tree({"hierarchy": [entry], "defaults": {}}, files=FILES)
    level = _paths_level(entry)
    scope = hyera.Scope(facts={"x": x})
    outside = (root / "other" / "secret.yaml").resolve()

    unconfined = level.paths(root, scope)
    assert any(os.path.samefile(p, outside) for p in unconfined if os.path.exists(p))
    del scandirs[:]
    assert level.paths(root, scope, confine=True) == []
    assert _listed_outside(scandirs, root) == []


def test_level_paths_confine_keeps_what_is_inside(make_tree):
    entry = {"name": "n", "glob": "real/*.yaml"}
    root = make_tree({"hierarchy": [entry], "defaults": {}}, files=FILES)
    scope = hyera.Scope()
    inside = _paths_level(entry).paths(root, scope, confine=True)
    assert [os.path.basename(p) for p in inside] == ["f.yaml"]
    assert inside == _paths_level(entry).paths(root, scope)


def test_level_paths_default_is_unconfined_and_unbounded(make_tree):
    entry = {"name": "n", "glob": "{real,real}/*.yaml"}
    root = make_tree({"hierarchy": [entry], "defaults": {}}, files=FILES)
    level = _paths_level(entry)
    scope = hyera.Scope()
    assert level.paths(root, scope) == level.paths(
        root, scope, confine=False, limits=None
    )


def test_level_paths_refuses_what_the_limits_refuse(make_tree):
    entry = {"name": "n", "glob": "{a,b,c}/*.yaml"}
    root = make_tree({"hierarchy": [entry], "defaults": {}}, files=FILES)
    level = _paths_level(entry)
    scope = hyera.Scope()
    with pytest.raises(hyera.BackendError, match="limits.glob_patterns"):
        level.paths(root, scope, limits=hyera.Limits(glob_patterns=2))
    assert level.paths(root, scope, limits=hyera.Limits(glob_patterns=3)) == []
    engine = hyera.Hiera(
        root / "hiera.yaml", limits=hyera.Limits(glob_patterns=2), scope=scope
    )
    with pytest.raises(hyera.BackendError, match="limits.glob_patterns"):
        engine.lookup("k", None, None, "d")


def test_level_paths_arguments_are_checked(make_tree):
    entry = {"name": "n", "path": "common.yaml"}
    root = make_tree({"hierarchy": [entry], "defaults": {}}, files=FILES)
    level = _paths_level(entry)
    for bad in (1, "yes", None):
        with pytest.raises(TypeError):
            level.paths(root, hyera.Scope(), confine=bad)
    with pytest.raises(TypeError):
        level.paths(root, hyera.Scope(), limits=5)


def test_a_glob_inside_the_datadir_still_matches(make_tree):
    _, on = _build(
        make_tree, {"name": "n", "glob": "real/*.yaml"}, confine_locations=True
    )
    assert _read(on, "linked") == "inside"


def test_a_plain_location_inside_is_unchanged(make_tree):
    _, on = _build(
        make_tree, {"name": "n", "path": "common.yaml"}, confine_locations=True
    )
    assert _read(on, "inside") is True


def test_an_interpolated_datadir_confines_to_its_literal_directories(make_tree):
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    defaults = {"datadir": "%{facts.d}"}
    inside = {"d": "data", "x": "../other/secret"}
    _, ok = _build(make_tree, dict(entry), inside, defaults, confine_locations=True)
    assert _read(ok) == "from-outside"
    outside = {"d": "data", "x": "../../elsewhere"}
    _, no = _build(make_tree, dict(entry), outside, defaults, confine_locations=True)
    assert _read(no) == "ABSENT"


def test_the_root_of_an_interpolated_datadir_is_its_literal_prefix(tmp_path):
    level = hyera.HieraLevel.new(
        {"name": "n", "datadir": "a/b%{x}/c", "path": "f.yaml"}, hyera.YAMLBackend()
    )
    root = confinement.confinement_root(level, tmp_path)
    assert root == (tmp_path / "a").as_posix() + "/"


def test_an_escape_is_warned_once_per_location(make_tree, caplog):
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    _, on = _build(make_tree, entry, {"x": "../other/secret"}, confine_locations=True)
    with caplog.at_level(logging.WARNING, logger="hyera.core"):
        for _ in range(3):
            assert _read(on) == "ABSENT"
    ours = [r for r in caplog.records if "confine_locations" in r.getMessage()]
    assert len(ours) == 1
    assert ours[0].levelno == logging.WARNING


def test_explain_shows_an_escape_as_a_path_not_found(make_tree):
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    _, on = _build(make_tree, entry, {"x": "../other/secret"}, confine_locations=True)
    text = on.explain("leak").text()
    assert "Path not found" in text
    assert "from-outside" not in text


def test_a_view_inherits_the_setting(make_tree):
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    _, on = _build(make_tree, entry, {"x": "x"}, confine_locations=True)
    view = on.scoped(facts={"x": "../other/secret"})
    assert _read(view) == "ABSENT"


def test_a_pickle_and_a_copy_keep_the_setting(make_tree):
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    _, on = _build(make_tree, entry, {"x": "../other/secret"}, confine_locations=True)
    for clone in (
        pickle.loads(pickle.dumps(on)),
        copy.copy(on),
        copy.deepcopy(on),
    ):
        assert _read(clone) == "ABSENT"


def test_confine_locations_must_be_a_bool(make_tree):
    root = make_tree({"hierarchy": []})
    with pytest.raises(TypeError, match="confine_locations"):
        hyera.Hiera(root / "hiera.yaml", confine_locations="yes")


@pytest.mark.skipif(os.name != "nt", reason="UNC paths exist only on Windows")
def test_a_unc_location_is_absent_and_never_resolved(make_tree, monkeypatch):
    seen = []
    real = confinement._real_normal

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(confinement, "_real_normal", spy)
    entry = {"name": "n", "path": "%{facts.x}.yaml"}
    _, on = _build(
        make_tree,
        entry,
        {"x": "//no-such-host.invalid/share/secret"},
        confine_locations=True,
    )
    assert _read(on) == "ABSENT"
    assert not [p for p in seen if "no-such-host" in p]


def test_the_resolve_count_is_one_per_location_and_zero_when_off(
    make_tree, monkeypatch
):
    calls = []
    real = confinement._real_normal

    def spy(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(confinement, "_real_normal", spy)
    entry = {"name": "n", "paths": ["common.yaml", "nodes/placeholder.yaml"]}
    _, off = _build(make_tree, copy.deepcopy(entry))
    off.lookup("inside")
    assert calls == []
    _, on = _build(make_tree, copy.deepcopy(entry), confine_locations=True)
    on.lookup("inside")
    first = len(calls)
    assert first <= 2 + 1
    on.lookup("inside")
    assert len(calls) - first <= 2


def test_a_hocon_include_outside_the_datadir_fails(make_tree, tmp_path):
    pytest.importorskip("pyhocon")
    outside = tmp_path / "other" / "inc.conf"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("from_include = yes\n", encoding="utf-8")
    text = 'include file("{}")\nmine = 1\n'.format(outside.as_posix())
    root = make_tree(
        {
            "hierarchy": [{"name": "n", "path": "d.conf"}],
            "defaults": {"data_hash": "hocon_data"},
        },
        files={"data/d.conf": text},
    )
    off = hyera.Hiera(root / "hiera.yaml")
    assert off.lookup("from_include") == "yes"
    on = hyera.Hiera(root / "hiera.yaml", confine_locations=True)
    with pytest.raises(hyera.BackendError, match="outside the data directory"):
        on.lookup("mine")


def test_a_hocon_include_inside_the_datadir_resolves(make_tree):
    pytest.importorskip("pyhocon")
    root = make_tree(
        {
            "hierarchy": [{"name": "n", "path": "d.conf"}],
            "defaults": {"data_hash": "hocon_data"},
        },
        files={"data/d.conf": "", "data/inc.conf": "from_include = yes\n"},
    )
    (root / "data" / "d.conf").write_text(
        'include file("{}")\n'.format((root / "data" / "inc.conf").as_posix()),
        encoding="utf-8",
    )
    on = hyera.Hiera(root / "hiera.yaml", confine_locations=True)
    assert on.lookup("from_include") == "yes"
