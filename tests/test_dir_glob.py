"""hyera's own Ruby ``Dir.glob`` port: brace expansion, the segment matcher,
the depth-first walker, and the datadir/config-root split -- the pieces
`_location_resolver.glob` is built from, replacing the guarded
``pathlib_next.Path.glob`` call a glob hierarchy level used to delegate to.
"""

import json
import os
import pathlib
import subprocess
import threading

import pytest

from hyera._config.location_resolver import (
    _entry_is_dir,
    _expand_braces,
    _fnmatch,
    _glob_one,
    _glob_root_and_pattern,
    _has_magic,
    _is_link,
    _prepare_segments,
    _segment_matcher,
    _segment_matches,
    glob,
)


def _write(root, rel, content=""):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    return path


def _dir_link(link, target):
    """A directory symlink on POSIX, a junction on Windows. Never skipped:
    a junction needs no elevated privilege, unlike a Windows symlink."""
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def _rel(root, matches):
    return [os.path.relpath(m, root).replace(os.sep, "/") for m in matches]


# --- _expand_braces (Ruby ruby_brace_expand) ------------------------------


@pytest.mark.parametrize(
    "pattern, expected",
    [
        ("{a,b}", ["a", "b"]),
        ("{b,a}", ["b", "a"]),
        ("{a,{b,c}}", ["a", "b", "c"]),
        ("x{,-y}", ["x", "x-y"]),
        ("{a,a}", ["a", "a"]),
        ("x{", ["x{"]),
        ("x\\{1\\}", ["x\\{1\\}"]),
        ("{a/b,c}", ["a/b", "c"]),
        ("{top}", ["top"]),
        # An escaped char *inside* an (unescaped, expanding) brace pair --
        # distinct from "x\\{1\\}" above, where the brace itself is
        # escaped and the inner scan never runs at all.
        ("{a\\,b,c}", ["a\\,b", "c"]),
    ],
)
def test_expand_braces(pattern, expected):
    assert _expand_braces(pattern) == expected


# --- _segment_matcher ------------------------------------------------------


@pytest.mark.parametrize(
    "seg, name, matches",
    [
        ("*", ".h", False),
        (".*", ".h", True),
        ("?h", ".h", False),
        ("[.]h", ".h", False),
        ("*.YAML", "a.yaml", False),
        ("[!a-z].yaml", "Z.yaml", True),
        ("[^a-z].yaml", "z.yaml", False),
        ("x\\*.yaml", "x*.yaml", True),
        ("x\\*.yaml", "xa.yaml", False),
        ("a**b.yaml", "aXYb.yaml", True),
        # An escaped plain (non-magic) character.
        ("x\\yz.yaml", "xyz.yaml", True),
        # An empty bracket class ("[]a]") never matches anything.
        ("[]a].yaml", "].yaml", False),
        # An unclosed bracket class never matches anything.
        ("a[bc.yaml", "a[bc.yaml", False),
        # An escaped character inside a bracket class.
        ("[\\]a].yaml", "].yaml", True),
    ],
)
def test_segment_matches(seg, name, matches):
    assert _segment_matcher(seg)(name) is matches


# --- _has_magic --------------------------------------------------------


@pytest.mark.parametrize(
    "seg, magic",
    [
        ("common.yaml", False),
        ("*.yaml", True),
        ("a[b", True),
        ("a[b]", True),
        ("x\\*.yaml", False),
    ],
)
def test_has_magic(seg, magic):
    assert _has_magic(seg) is magic


# --- _prepare_segments / _segment_matches / _glob_one edge cases ---------


def test_prepare_segments_collapses_consecutive_recursive():
    # "a/**/**/b": the second non-trailing "**" collapses into the first
    # rather than adding a second "recursive" segment.
    assert _prepare_segments("a/**/**/b") == [
        ("literal", "a"),
        ("recursive", None),
        ("literal", "b"),
    ]


def test_prepare_segments_unescapes_a_literal_segment():
    # A non-magic segment (its only special char is itself escaped, so
    # _has_magic says False) still needs its backslash stripped for the
    # real filename to match against.
    assert _prepare_segments("x\\*.yaml") == [("literal", "x*.yaml")]


def test_segment_matches_unknown_kind_is_false():
    # _segment_matches's own fallback: every real caller only ever builds
    # "literal"/"magic" segments through _prepare_segments (a "recursive"
    # segment is handled by _glob_one's own walk, never passed here) --
    # exercised directly.
    assert _segment_matches("recursive", None, "x") is False


def test_glob_one_empty_pattern_matches_nothing(tmp_path):
    assert _glob_one(str(tmp_path), "") == []


class _FakeEntry:
    """A minimal `os.DirEntry`-shaped double: `_is_link`/`_entry_is_dir`
    only ever call `.is_symlink()`/`.stat()`/`.is_dir()` on their argument,
    never check its type, so a real `os.DirEntry` (only ever produced by
    `os.scandir`, not directly constructible) is not needed to exercise
    their own `except OSError` backstops.
    """

    def is_symlink(self):
        return False

    def stat(self, follow_symlinks=False):
        raise OSError("boom")

    def is_dir(self, follow_symlinks=False):
        raise OSError("boom")


def test_is_link_stat_oserror_is_not_a_link(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert _is_link(_FakeEntry()) is False


def test_entry_is_dir_oserror_is_not_a_dir():
    assert _entry_is_dir(_FakeEntry()) is False


# --- the walker over a real tree -------------------------------------------


@pytest.fixture
def tree(tmp_path):
    for rel in (
        "0.yaml",
        "top.yaml",
        ".dot.yaml",
        "a/mid.yaml",
        "a/x.yaml",
        "a/b/deep.yaml",
        "a-b/x.yaml",
        "B/x.yaml",
        "_u/x.yaml",
        ".hid/h.yaml",
    ):
        _write(tmp_path, rel)
    return tmp_path


@pytest.mark.parametrize(
    "pattern, expected",
    [
        (
            "**/*.yaml",
            [
                "0.yaml",
                "B/x.yaml",
                "_u/x.yaml",
                "a/b/deep.yaml",
                "a/mid.yaml",
                "a/x.yaml",
                "a-b/x.yaml",
                "top.yaml",
            ],
        ),
        ("*/x.yaml", ["B/x.yaml", "_u/x.yaml", "a/x.yaml", "a-b/x.yaml"]),
        ("**/x.yaml", ["B/x.yaml", "_u/x.yaml", "a/x.yaml", "a-b/x.yaml"]),
        ("a/**", ["a/b", "a/mid.yaml", "a/x.yaml"]),
        ("**/.hid/*.yaml", [".hid/h.yaml"]),
        (".*.yaml", [".dot.yaml"]),
        ("{top,0}.yaml", ["top.yaml", "0.yaml"]),
        ("{0,0}.yaml", ["0.yaml", "0.yaml"]),
        ("*/", []),
    ],
)
def test_tree_patterns(tree, pattern, expected):
    assert _rel(tree, glob(str(tree), pattern)) == expected


def test_order_is_bytewise(tmp_path):
    for rel in ("B.yaml", "Z.yaml", "_x.yaml", "a.yaml"):
        _write(tmp_path, rel)
    assert _rel(tmp_path, glob(str(tmp_path), "*.yaml")) == [
        "B.yaml",
        "Z.yaml",
        "_x.yaml",
        "a.yaml",
    ]


def test_missing_directory_is_empty(tmp_path):
    assert glob(str(tmp_path), "nope/*.yaml") == []
    assert glob(str(tmp_path), "nope/x.yaml") == []


def test_unreadable_directory_is_skipped(tmp_path, monkeypatch):
    _write(tmp_path, "z.yaml")
    _write(tmp_path, "locked/x.yaml")
    _write(tmp_path, "open/y.yaml")

    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if os.path.basename(os.fspath(path)) == "locked":
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)
    assert _rel(tmp_path, glob(str(tmp_path), "**/*.yaml")) == [
        "open/y.yaml",
        "z.yaml",
    ]


def _build_link_tree(tmp_path):
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    _write(tmp_path, "data/z.yaml")
    _write(tmp_path, "outside/o.yaml")
    _dir_link(data / "l1", data)
    _dir_link(data / "l2", data)
    _dir_link(data / "ext", outside)
    return data


def test_links_not_recursed_by_double_star(tmp_path):
    data = _build_link_tree(tmp_path)
    result = []
    thread = threading.Thread(
        target=lambda: result.extend(_rel(data, glob(str(data), "**/*.yaml"))),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert result == ["z.yaml"]


def test_explicit_segment_follows_link(tmp_path):
    data = _build_link_tree(tmp_path)
    assert _rel(data, glob(str(data), "ext/*.yaml")) == ["ext/o.yaml"]


def test_wildcards_are_case_sensitive_on_every_os(tmp_path):
    _write(tmp_path, "a.yaml")
    assert glob(str(tmp_path), "*.YAML") == []
    if os.path.exists(str(tmp_path / "A.yaml")):
        # Case-insensitive filesystem: "a.yaml" already answers to "A.yaml"
        # at the OS level, so a literal pattern finds it through the
        # filesystem (as the literal segment it was given, since a literal
        # match is an existence check, never a directory listing) even
        # though our own wildcard matching stays case-sensitive.
        assert _rel(tmp_path, glob(str(tmp_path), "A.yaml")) == ["A.yaml"]
    else:
        _write(tmp_path, "A.yaml")
        assert _rel(tmp_path, glob(str(tmp_path), "*.yaml")) == [
            "A.yaml",
            "a.yaml",
        ]


# --- _glob_root_and_pattern --------------------------------------------


@pytest.mark.parametrize(
    "datadir, g, expected",
    [
        ("data", "*.yaml", ("/r", "data/*.yaml")),
        ("data", "../x/*.yaml", ("/r", "x/*.yaml")),
        ("/srv/h", "*.yaml", ("/", "srv/h/*.yaml")),
        ("data", "/abs/*.yaml", ("/", "abs/*.yaml")),
    ],
)
def test_glob_root_and_pattern(datadir, g, expected):
    assert _glob_root_and_pattern("/r", datadir, g) == expected


# --- linear matching, bounded depth, character ranges -------------------


class _CountingName(str):
    """A name that counts how many characters the matcher reads from it and
    how many substring searches it asks of it."""

    reads = 0
    finds = 0

    def __getitem__(self, index):
        type(self).reads += 1
        return super().__getitem__(index)

    def find(self, *args):
        type(self).finds += 1
        return super().find(*args)


def test_many_stars_cost_is_bounded_by_pattern_times_name():
    name = _CountingName("a" * 60)
    _CountingName.reads = 0
    pattern = "*a" * 12 + "?b.yaml"
    assert _fnmatch(pattern, name) is False
    assert _CountingName.reads <= len(name) * (len(pattern) + 1)


def test_many_stars_over_plain_text_search_each_chunk_once():
    name = _CountingName("a" * 60)
    _CountingName.finds = 0
    pattern = "*a" * 12 + "*b.yaml"
    assert _segment_matcher(pattern)(name) is False
    assert _CountingName.finds <= 13


@pytest.mark.parametrize(
    "seg, name, matches",
    [
        ("[z-a].yaml", "a.yaml", True),
        ("[z-a].yaml", "z.yaml", True),
        ("[z-a].yaml", "m.yaml", False),
        ("[a-\\z].yaml", "b.yaml", True),
        ("[a-\\z].yaml", "0.yaml", False),
        ("[a-]", "-", True),
        ("[]a]", "]", False),
        ("[!]a]x", "bx", False),
        ("a.yaml\\", "a.yaml", True),
        ("a*\\", "abc", True),
        (".*", ".", True),
    ],
)
def test_bracket_and_escape_rules_follow_ruby(seg, name, matches):
    assert _segment_matcher(seg)(name) is matches


def test_deeply_nested_braces_expand_without_recursion():
    depth = 3000
    assert _expand_braces("{" * depth + "a" + "}" * depth) == ["a"]
    assert _expand_braces("{" * depth + "a,b" + "}" * depth + ".yaml") == [
        "a.yaml",
        "b.yaml",
    ]


def test_brace_expansion_keeps_written_order_across_groups():
    assert _expand_braces("{b,a}{2,1}") == ["b2", "b1", "a2", "a1"]
    assert _expand_braces("{a{b,c},d}e") == ["abe", "ace", "de"]
    assert _expand_braces("{a{b,c") == ["{a{b,c"]


def test_deep_tree_is_walked_without_recursion(tmp_path, monkeypatch):
    depth = 1500

    class Entry:
        def __init__(self, name, is_dir):
            self.name, self._dir = name, is_dir

        def is_symlink(self):
            return False

        def is_dir(self, follow_symlinks=True):
            return self._dir

        def stat(self, follow_symlinks=True):
            return type("S", (), {"st_reparse_tag": 0})()

    def scandir(path):
        levels = str(path).replace("\\", "/").count("/d")
        return [Entry("d", True)] if levels < depth else [Entry("x.yaml", False)]

    monkeypatch.setattr(os, "scandir", scandir)
    (match,) = glob("/virtual", "**/x.yaml")
    assert match.replace("\\", "/") == "/virtual" + "/d" * depth + "/x.yaml"


def test_literal_after_recursive_segment_follows_the_filesystem_case(tmp_path):
    _write(tmp_path, "sub/c.yaml")
    matches = glob(str(tmp_path), "**/C.yaml")
    if os.path.exists(str(tmp_path / "SUB" / "C.YAML")):
        assert _rel(tmp_path, matches) == ["sub/c.yaml"]
    else:
        assert matches == []


# --- the recorded Ruby Dir.glob table ----------------------------------

_TABLE = json.loads(
    (pathlib.Path(__file__).parent / "data" / "dir_glob_table.json").read_text(
        encoding="utf-8"
    )
)


@pytest.fixture(scope="module")
def ruby_tree(tmp_path_factory):
    root = tmp_path_factory.mktemp("ruby_tree")
    for rel in _TABLE["tree"]:
        if rel.endswith("/"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        else:
            _write(root, rel)
    return root


# Rows whose answer depends on the operating system, not on the matcher: a
# literal segment is an existence check, so a case-folding filesystem finds
# "Sub/c.yaml", and Windows resolves "x/.", "x/..", "..." and a bare "\\" itself.
_OS_DEPENDENT = {
    "A.yaml",
    "Sub/c.yaml",
    "Sub/*.yaml",
    "*/C.yaml",
    "**/Sub/c.yaml",
    "**/C.yaml",
    "**/D.txt",
    "*/.",
    "*/..",
    ".*/.",
    ".*/..",
    "...",
    "\\\\",
}


@pytest.mark.parametrize(
    "pattern, expected",
    [pytest.param(p, e, id=p) for p, e in _TABLE["rows"]],
)
def test_glob_matches_the_recorded_ruby_listing(ruby_tree, pattern, expected):
    if os.name == "nt" and pattern in _OS_DEPENDENT:
        pytest.skip("answered by the Windows filesystem, not by the matcher")
    got = [
        m[len(str(ruby_tree)) + 1 :].replace(os.sep, "/")
        for m in glob(str(ruby_tree), pattern)
    ]
    assert got == expected
