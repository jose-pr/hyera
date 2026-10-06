"""Stat revalidation, one probe per candidate and the glob listing memo."""

import collections
import os

import pytest

from hyera import Hiera, KeyNotFoundError, Scope
from hyera._lookup import locations
from hyera.backends import YAMLBackend

# --- stat revalidation, one probe per candidate, glob listing memo --------


def _bump_mtime(p):
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))


def test_changed_file_is_reread(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v1\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v1"

    p = root / "data" / "common.yaml"
    p.write_bytes(b"k: v22\n")
    _bump_mtime(p)

    assert h.lookup("k") == "v22"


def test_revalidate_false_keeps_first_read_until_clear_cache(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v1\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), revalidate=False)
    assert h.lookup("k") == "v1"

    p = root / "data" / "common.yaml"
    p.write_bytes(b"k: v22\n")
    _bump_mtime(p)

    assert h.lookup("k") == "v1"
    h.clear_cache()
    assert h.lookup("k") == "v22"


def test_replaced_file_detected_by_inode(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v1\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v1"

    original = root / "data" / "common.yaml"
    st = original.stat()
    sibling = root / "data" / "common.yaml.new"
    sibling.write_bytes(b"k: v2\n")
    os.utime(sibling, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(str(sibling), str(original))

    assert h.lookup("k") == "v2"


def test_deleted_file_reads_as_absent(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "high", "path": "high.yaml"},
                {"name": "low", "path": "low.yaml"},
            ]
        },
        files={"data/high.yaml": "k: high\n", "data/low.yaml": "k: low\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "high"

    os.remove(str(root / "data" / "high.yaml"))
    assert h.lookup("k") == "low"

    os.remove(str(root / "data" / "low.yaml"))
    with pytest.raises(KeyNotFoundError):
        h.lookup("k")


def test_load_file_cached_entry_vanishing_before_a_revalidation_probe(make_tree):
    # load_file's absent-after-cached branch: a path read and cached once, then found
    # gone by a later call's fresh probe, reads as absent, not an error. Unlike
    # test_deleted_file_reads_as_absent above, the hierarchy still lists the location.
    # A real two-lookup sequence cannot reach it through the scope-interpolation-stable
    # caches above, so load_file is called directly.
    from hyera._lookup.navigation import _MISSING

    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml"}]},
        files={"data/a.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    path = root / "data" / "a.yaml"
    backend = YAMLBackend()

    first = h._store.load_file(path, backend, {})
    assert first == {"k": "v"}
    cache_key = (path, backend.strict, "{}")
    assert cache_key in h._store._file_cache
    assert path in h._store._loaded_paths

    os.remove(str(path))
    second = h._store.load_file(path, backend, {})
    assert second is _MISSING
    assert cache_key not in h._store._file_cache
    assert path not in h._store._loaded_paths


def test_new_file_at_literal_location_is_seen(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "node", "path": "nodes/%{trusted.certname}.yaml"}]},
        files={},
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(variables={"clientcert": "n1"}))
    with pytest.raises(KeyNotFoundError):
        h.lookup("k")

    node_dir = root / "data" / "nodes"
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "n1.yaml").write_bytes(b"k: v\n")

    assert h.lookup("k") == "v"


@pytest.mark.parametrize("revalidate", [True, False])
def test_new_glob_match_after_directory_change(make_tree, revalidate):
    root = make_tree(
        {"hierarchy": [{"name": "mods", "glob": "mods/*.yaml"}]},
        files={"data/mods/existing.yaml": "existing: yes\n"},
    )
    h = Hiera(str(root / "hiera.yaml"), revalidate=revalidate)
    with pytest.raises(KeyNotFoundError):
        h.lookup("only_new")

    mods_dir = root / "data" / "mods"
    (mods_dir / "new.yaml").write_bytes(b"only_new: x\n")
    _bump_mtime(mods_dir)

    if revalidate:
        assert h.lookup("only_new") == "x"
    else:
        with pytest.raises(KeyNotFoundError):
            h.lookup("only_new")
        h.clear_cache()
        assert h.lookup("only_new") == "x"


def _create(base, rel, text):
    """Write ``base/rel``, creating directories, then bump the mtime of the deepest
    directory that already existed: the only one a real creation changes, made
    visible on filesystems with coarse timestamps."""
    target = base / rel
    gained = target.parent
    while not gained.is_dir():
        gained = gained.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    _bump_mtime(gained)


@pytest.mark.parametrize(
    "pattern, before, new_file",
    [
        ("conf.d/*.yaml", {"conf.d/zz.yaml": "other: 1\n"}, "conf.d/aa.yaml"),
        ("conf.d/*.yaml", {}, "conf.d/aa.yaml"),
        ("a/b/*.yaml", {"a/keep.txt": "x\n"}, "a/b/aa.yaml"),
        ("nodes/*/over.yaml", {"nodes/n1/other.yaml": "o: 1\n"}, "nodes/n1/over.yaml"),
        ("nodes/*/over.yaml", {"nodes/n1/other.yaml": "o: 1\n"}, "nodes/n2/over.yaml"),
        ("conf.d/exact.yaml", {"conf.d/zz.yaml": "other: 1\n"}, "conf.d/exact.yaml"),
        ("tree/**/*.yaml", {"tree/x/y/zz.yaml": "o: 1\n"}, "tree/x/y/aa.yaml"),
        ("tree/**/*.yaml", {"tree/x/y/zz.yaml": "o: 1\n"}, "tree/x/new/aa.yaml"),
        ("{one,two}/*.yaml", {"one/zz.yaml": "o: 1\n"}, "two/aa.yaml"),
        ("*.yml", {}, "aa.yml"),
    ],
    ids=[
        "dir-exists",
        "dir-absent",
        "literal-subdir-created",
        "file-under-wildcard-dir",
        "new-wildcard-dir",
        "literal-file",
        "recursive-file",
        "recursive-dir",
        "braces-dir-absent",
        "datadir-absent",
    ],
)
def test_glob_level_sees_a_file_that_starts_matching(
    make_tree, pattern, before, new_file
):
    files = {"data/common.yaml": "k: common\n"}
    files.update({"data/" + rel: text for rel, text in before.items()})
    root = make_tree(
        {
            "hierarchy": [
                {"name": "g", "glob": pattern},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files=files,
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "common"

    _create(root / "data", new_file, "k: from-new-file\n")

    assert h.lookup("k") == "from-new-file"
    assert h.lookup("k") == Hiera(str(root / "hiera.yaml")).lookup("k")


def test_glob_level_sees_a_vanished_literal_match(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "g", "glob": "conf.d/exact.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": "k: common\n",
            "data/conf.d/exact.yaml": "k: exact\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "exact"
    (root / "data" / "conf.d" / "exact.yaml").unlink()
    _bump_mtime(root / "data" / "conf.d")
    assert h.lookup("k") == "common"


def test_changed_lookup_options_reapplied(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "high", "path": "high.yaml"},
                {"name": "low", "path": "low.yaml"},
            ]
        },
        files={
            "data/high.yaml": "l: [h]\n",
            "data/low.yaml": "l: [lo]\nlookup_options: {l: {merge: unique}}\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("l") == ["h", "lo"]

    p = root / "data" / "low.yaml"
    p.write_bytes(b"l: [lo]\n")
    _bump_mtime(p)

    assert h.lookup("l") == ["h"]


@pytest.mark.parametrize("revalidate", [True, False])
def test_filesystem_probes_per_lookup(make_tree, monkeypatch, revalidate):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "node", "path": "nodes/%{trusted.certname}.yaml"},
                {"name": "role", "path": "roles/%{role}.yaml"},
                {"name": "missing", "path": "never/exists.yaml"},
                {"name": "apps", "mapped_paths": ["apps", "app", "apps/%{app}.yaml"]},
                {"name": "mods", "glob": "mods/*.yaml"},
            ]
        },
        files={
            "data/nodes/a.yaml": "k: node_a\n",
            "data/nodes/b.yaml": "k: node_b\n",
            "data/roles/web.yaml": "k: role_web\n",
            "data/apps/x1.yaml": "k: app_x1\n",
            "data/apps/x2.yaml": "k: app_x2\n",
            **{
                "data/mods/mod{}.yaml".format(i): "k: mod{}\n".format(i)
                for i in range(5)
            },
        },
    )
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(
            variables={"clientcert": "a", "role": "web"}, facts={"apps": ["x1", "x2"]}
        ),
        revalidate=revalidate,
    )
    # Untracked warm-up: builds and caches everything before counting.
    assert h.lookup("k") == "node_a"

    # Keyed by (function name, path): os.scandir (a glob's directory listing) and
    # os.stat (a location's existence probe) are different syscalls even on the same
    # path, and "probed once" is a per-function claim.
    counts = collections.Counter()

    def _wrap(name, fn):
        def wrapped(path, *a, **kw):
            counts[(name, os.fspath(path))] += 1
            return fn(path, *a, **kw)

        return wrapped

    for name in ("stat", "lstat", "scandir", "listdir"):
        monkeypatch.setattr(os, name, _wrap(name, getattr(os, name)))
    for name in ("exists", "isfile", "isdir"):
        monkeypatch.setattr(os.path, name, _wrap(name, getattr(os.path, name)))

    def scandir_count():
        return sum(c for (fn, _p), c in counts.items() if fn == "scandir")

    def max_stat_per_path():
        stat_counts = collections.Counter()
        for (fn, p), c in counts.items():
            if fn in ("stat", "lstat"):
                stat_counts[p] += c
        return max(stat_counts.values(), default=0)

    # Every candidate (a plain location or the glob-walked directory) is probed at most
    # once per lookup: a location build and its materialization share one memo, and
    # `_glob_one`'s descent through the literal "mods" segment uses it too, not a bare
    # `os.path.isdir`.

    assert h.lookup("k") == "node_a"
    if revalidate:
        assert max_stat_per_path() <= 1, counts
        assert scandir_count() == 0, counts
    else:
        assert sum(counts.values()) == 0, counts

    counts.clear()
    new_view = h.scoped(variables={"clientcert": "b"})
    assert new_view.lookup("k") == "node_b"
    # A new clientcert rebuilds the location entry (referenced variables changed), yet
    # every candidate is still probed at most once (build and materialize share one
    # memo), and the mods glob's cache, whose key never depended on clientcert, is
    # untouched: no scandir happens.
    assert max_stat_per_path() <= 1, counts
    assert scandir_count() == 0, counts

    counts.clear()
    h.clear_cache()
    assert h.lookup("k") == "node_a"
    assert max_stat_per_path() <= 1, counts
    if revalidate:
        assert scandir_count() == 1, counts


def test_revalidate_validation(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
    )
    with pytest.raises(TypeError):
        Hiera(str(root / "hiera.yaml"), revalidate="yes")


def _many_level_tree(make_tree, levels):
    hierarchy = [
        {"name": "l%d" % i, "path": "l%d/%%{trusted.certname}.yaml" % i}
        for i in range(levels - 1)
    ] + [{"name": "common", "path": "common.yaml"}]
    return make_tree(
        {"hierarchy": hierarchy},
        files={"data/common.yaml": "k: v\n", "data/l0/n1.yaml": "other: 1\n"},
    )


def test_warm_lookup_probes_each_location_once(make_tree, monkeypatch):
    levels = 40
    root = _many_level_tree(make_tree, levels)
    h = Hiera(
        str(root / "hiera.yaml"),
        scope=Scope(trusted={"certname": "n1"}),
        revalidate=True,
    )
    assert h.lookup("k") == "v"
    probes = [0]
    original = locations._LocationStore.require_not_dir

    def counting(self, path, invocation):
        probes[0] += 1
        return original(self, path, invocation)

    monkeypatch.setattr(locations._LocationStore, "require_not_dir", counting)
    assert h.lookup("k") == "v"
    assert probes[0] == levels


def test_ordinary_lookup_builds_no_explain_location_reference(make_tree, monkeypatch):
    from hyera._lookup import function_provider

    root = _many_level_tree(make_tree, 5)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(trusted={"certname": "n1"}))
    built = [0]
    original = function_provider._location_ref

    def counting(location):
        built[0] += 1
        return original(location)

    monkeypatch.setattr(function_provider, "_location_ref", counting)
    assert h.lookup("k") == "v"
    assert built[0] == 0
    h.explain("k")
    assert built[0] > 0
