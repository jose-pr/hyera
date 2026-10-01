"""A glob/globs hierarchy level whose directory does not exist matches
nothing, instead of ``Hiera()``/``.lookup()`` raising ``FileNotFoundError``
(Puppet's ``expand_globs`` returns no matches for a missing directory).
"""

import os
import re
import shutil
from pathlib import Path as StdPath

import pytest

from hyera import BackendError, Hiera, Scope
from test_dir_glob import _dir_link

REPO_ROOT = StdPath(__file__).resolve().parents[1]


def test_glob_over_missing_constant_dir(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "mods", "glob": "modules/*.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: common\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "common"


def test_glob_over_missing_per_node_dir(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "pernode", "glob": "nodes/%{facts.node_id}/*.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/nodes/web1/a.yaml": "k: web1\n",
            "data/common.yaml": "k: common\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"node_id": "db7"}))
    web1 = h.scoped(facts={"node_id": "web1"})
    assert web1.lookup("k") == "web1"
    db7 = h.scoped(facts={"node_id": "db7"})
    assert db7.lookup("k") == "common"


def test_glob_level_with_missing_datadir(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "nowhere-mods", "datadir": "nowhere", "glob": "*.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: common\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "common"


def test_glob_mid_wildcard_matching_a_file_is_not_descended(make_tree):
    # A non-final "*" segment matches any name, directory or not (only an
    # os.scandir attempt right after actually tells the two apart) -- a
    # plain file sitting beside a real subdirectory at that position must
    # not stop the walk from finding a later match through the real
    # subdirectory; recursing "into" the file fails harmlessly (caught the
    # same way an unreadable directory already is), unlike a *final*
    # segment, where the same file would be a legitimate match.
    root = make_tree(
        {"hierarchy": [{"name": "mods", "glob": "mods/*/*.yaml"}]},
        files={
            "data/mods/sub/x.yaml": "k: v\n",
            "data/mods/notadir.yaml": "not actually a directory\n",
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"


def _first_hierarchy_yaml_block(readme_text: str) -> str:
    blocks = re.findall(r"```yaml\n(.*?)\n```", readme_text, flags=re.DOTALL)
    for block in blocks:
        if "hierarchy:" in block:
            return block
    raise AssertionError(
        "no fenced yaml block containing 'hierarchy:' found in README.md"
    )


def test_readme_example_config_constructs(make_tree):
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    config_text = _first_hierarchy_yaml_block(readme_text)

    root = make_tree(config_text, files={"data/common.yaml": "k: common\n"})
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "common"


def test_glob_level_rejects_directories(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "g", "glob": "*"}]},
        files={"data/z.yaml": "k: z\n", "data/sub/in.yaml": "k2: in\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "z"
    assert "k2" not in h

    only_dir = make_tree(
        {"hierarchy": [{"name": "g", "glob": "sub"}]},
        files={"data/sub/in.yaml": "k2: in\n"},
        root="only_dir",
    )
    h2 = Hiera(str(only_dir / "hiera.yaml"))
    assert "k2" not in h2


def test_dangling_link_match_raises_backend_error(make_tree):
    root = make_tree({"hierarchy": [{"name": "g", "glob": "*.yaml"}]})
    data = root / "data"
    data.mkdir(exist_ok=True)
    link = data / "a.yaml"
    if os.name == "nt":
        target = root / "gone"
        target.mkdir()
        _dir_link(link, target)
        shutil.rmtree(target)
    else:
        os.symlink(str(root / "gone"), str(link))

    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(BackendError) as exc:
        h.lookup("anything")
    assert str(exc.value.path).replace(os.sep, "/").endswith("a.yaml")
