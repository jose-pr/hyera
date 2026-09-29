"""hyera's own Ruby ``Dir.glob`` port: brace expansion, the segment matcher,
the depth-first walker, and the datadir/config-root split -- the pieces
`_location_resolver.glob` is built from, replacing the guarded
``pathlib_next.Path.glob`` call a glob hierarchy level used to delegate to.
"""

import os
import subprocess
import threading

import pytest

from hyera._location_resolver import (
    _expand_braces,
    _glob_root_and_pattern,
    _has_magic,
    _segment_matcher,
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
