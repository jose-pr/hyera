"""A glob/globs hierarchy level whose directory does not exist matches
nothing, instead of ``Hiera()``/``.get()`` raising ``FileNotFoundError``
(Puppet's ``expand_globs`` returns no matches for a missing directory).
"""

import re
from pathlib import Path as StdPath

from pyera import Hiera

REPO_ROOT = StdPath(__file__).resolve().parents[1]


def test_glob_over_missing_constant_dir(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text("k: common\n", encoding="utf-8")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: yaml_data\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: mods\n"
        "    glob: modules/*.yaml\n"
        "  - name: common\n"
        "    path: common.yaml\n",
        encoding="utf-8",
    )

    h = Hiera(str(config))
    assert h.get("k") == "common"


def test_glob_over_missing_per_node_dir(tmp_path):
    data = tmp_path / "data"
    (data / "nodes" / "web1").mkdir(parents=True)
    (data / "nodes" / "web1" / "a.yaml").write_text("k: web1\n", encoding="utf-8")
    (data / "common.yaml").write_text("k: common\n", encoding="utf-8")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: yaml_data\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: pernode\n"
        "    glob: nodes/%{facts.node_id}/*.yaml\n"
        "  - name: common\n"
        "    path: common.yaml\n",
        encoding="utf-8",
    )

    h = Hiera(str(config), context={"facts": {"node_id": "db7"}})
    assert h.get("k", context={"facts": {"node_id": "web1"}}) == "web1"
    assert h.get("k", context={"facts": {"node_id": "db7"}}) == "common"


def test_glob_level_with_missing_datadir(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text("k: common\n", encoding="utf-8")
    config = tmp_path / "hiera.yaml"
    config.write_text(
        "version: 5\n"
        "defaults:\n"
        "  data_hash: yaml_data\n"
        "  data_dir: data\n"
        "hierarchy:\n"
        "  - name: nowhere-mods\n"
        "    datadir: nowhere\n"
        "    glob: '*.yaml'\n"
        "  - name: common\n"
        "    path: common.yaml\n",
        encoding="utf-8",
    )

    h = Hiera(str(config))
    assert h.get("k") == "common"


def _first_hierarchy_yaml_block(readme_text: str) -> str:
    blocks = re.findall(r"```yaml\n(.*?)\n```", readme_text, flags=re.DOTALL)
    for block in blocks:
        if "hierarchy:" in block:
            return block
    raise AssertionError(
        "no fenced yaml block containing 'hierarchy:' found in README.md"
    )


def test_readme_example_config_constructs(tmp_path):
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    config_text = _first_hierarchy_yaml_block(readme_text)

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_text("k: common\n", encoding="utf-8")
    config = tmp_path / "hiera.yaml"
    config.write_text(config_text, encoding="utf-8")

    h = Hiera(str(config))
    assert h.get("k") == "common"
