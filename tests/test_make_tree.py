"""``make_tree`` itself: the config defaults it injects and its byte shape."""

import yaml


def test_make_tree_defaults_bytes_and_encoding(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={"data/common.yaml": 'greeting: "café"\n'},
    )
    config = yaml.safe_load((root / "hiera.yaml").read_bytes())
    assert config["version"] == 5
    assert config["defaults"]["datadir"] == "data"
    assert config["defaults"]["data_hash"] == "yaml_data"

    for path in root.rglob("*"):
        if path.is_file():
            assert b"\r\n" not in path.read_bytes(), path

    text = (root / "data" / "common.yaml").read_text(encoding="utf-8")
    assert "café" in text
