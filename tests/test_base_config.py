"""``Hiera.load`` reads a path-configured ``hiera.yaml`` as UTF-8 bytes and
holds no open file handle: a non-ASCII config resolves correctly regardless
of the host locale, the config file can be replaced or removed while the
instance is alive, and the instance survives ``pickle``/``deepcopy``.
"""

import copy
import io
import os
import pathlib
import pickle

from hyera import Hiera

CONFIG_TEXT = (
    "version: 5\n"
    "defaults:\n"
    "  data_hash: yaml_data\n"
    "  datadir: données\n"
    "hierarchy:\n"
    "  - name: Café\n"
    "    path: common.yaml\n"
)


def _write_non_ascii_config(tmp_path, prefix=b""):
    datadir = tmp_path / "données"
    datadir.mkdir()
    (datadir / "common.yaml").write_bytes("k: accented\n".encode("utf-8"))
    config = tmp_path / "hiera.yaml"
    config.write_bytes(prefix + CONFIG_TEXT.encode("utf-8"))
    return config


def _write_simple_config(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "common.yaml").write_bytes(b"k: v\n")
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        b"version: 5\n"
        b"defaults:\n"
        b"  data_hash: yaml_data\n"
        b"  datadir: data\n"
        b"hierarchy:\n"
        b"  - name: common\n"
        b"    path: common.yaml\n"
    )
    return config


def test_non_ascii_config_is_read_as_utf8(tmp_path):
    config = _write_non_ascii_config(tmp_path)
    h = Hiera(str(config))
    assert h.lookup("k") == "accented"


def test_utf8_bom_config_parses(tmp_path):
    # Flow style, not `CONFIG_TEXT`'s block style: `puppet lookup` reads
    # hiera.yaml via `HieraConfig.create` -> `cached_file_data` ->
    # `Puppet::Util::Yaml.safe_load(content, ...)` directly on the file's
    # content, the same "keep a literal BOM character" path a *data* file
    # goes through (`context.rb:53`) -- not the BOM-*stripping* file read
    # `Puppet::Util::Yaml.safe_load_file` uses elsewhere. A block-style
    # multi-line mapping right after a BOM (swapped for a space by
    # `_psych.safe_load`, matching Psych) only keeps its *first* key,
    # a genuine Puppet quirk covered separately
    # (`test_psych.py::test_hiera_yaml_bom_without_document_marker_loads_only_the_first_key`);
    # a flow-style mapping has no such line-indentation problem.
    datadir = tmp_path / "données"
    datadir.mkdir()
    (datadir / "common.yaml").write_bytes("k: accented\n".encode("utf-8"))
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        "﻿{version: 5, defaults: {data_hash: yaml_data, datadir: données}, "
        "hierarchy: [{name: Café, path: common.yaml}]}\n".encode("utf-8")
    )
    h = Hiera(str(config))
    assert h.lookup("k") == "accented"


def test_path_config_keeps_the_path(tmp_path):
    config = _write_simple_config(tmp_path)

    h_str = Hiera(str(config))
    assert h_str.base_config == str(config)
    assert not hasattr(h_str.base_config, "read")

    p = pathlib.Path(config)
    h_path = Hiera(p)
    assert h_path.base_config is p


def test_path_configured_hiera_pickles_and_deepcopies(tmp_path):
    config = _write_simple_config(tmp_path)
    h = Hiera(str(config))

    reloaded = pickle.loads(pickle.dumps(h))
    assert reloaded.lookup("k") == h.lookup("k")

    cloned = copy.deepcopy(h)
    assert cloned.lookup("k") == h.lookup("k")


def test_config_replaceable_while_instance_alive(tmp_path):
    config = _write_simple_config(tmp_path)
    h = Hiera(str(config))
    assert h.lookup("k") == "v"

    replacement = tmp_path / "hiera.yaml.new"
    replacement.write_bytes(
        b"version: 5\n"
        b"defaults:\n"
        b"  data_hash: yaml_data\n"
        b"hierarchy:\n"
        b"  - name: common\n"
        b"    path: other.yaml\n"
    )
    os.replace(str(replacement), str(config))
    os.remove(str(config))

    assert h.lookup("k") == "v"


def test_file_like_config_is_left_to_the_caller(tmp_path):
    config = _write_simple_config(tmp_path)
    config_bytes = config.read_bytes()
    stream = io.BytesIO(config_bytes)

    h = Hiera(stream, base_path=str(tmp_path))
    assert h.base_config is stream
    assert not stream.closed
    assert h.lookup("k") == "v"
