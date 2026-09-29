"""A glob/globs hierarchy level whose directory does not exist matches
nothing, instead of ``Hiera()``/``.get()`` raising ``FileNotFoundError``
(Puppet's ``expand_globs`` returns no matches for a missing directory).
"""

import re
from pathlib import Path as StdPath

from hyera import Hiera, Scope

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
    assert h.get("k") == "common"


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
    assert web1.get("k") == "web1"
    db7 = h.scoped(facts={"node_id": "db7"})
    assert db7.get("k") == "common"


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
    assert h.get("k") == "common"


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
    assert h.get("k") == "common"
