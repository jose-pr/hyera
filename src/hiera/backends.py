import json
import subprocess
from pathlib import Path

import yaml

from .util import LookupDict


class Backend(object):
    """
    Backends provide a way of loading data from files. They should
    override .load with a custom loading method.
    """

    NAME = None

    def __init__(self, conf: dict = None):
        self.conf = conf or {}
        self.datadir: str = self.conf["datadir"] or ""

    def read_file(self, path: Path):
        return path.read_bytes()

    def load(self, data: bytes):
        raise NotImplementedError("Subclasses must implement .load")


class YAMLBackend(Backend):
    NAME = "yaml_data"

    def load(self, data):
        return self.load_ordered(data)

    @staticmethod
    def load_ordered(stream, Loader=yaml.Loader, object_pairs_hook=LookupDict):
        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))

        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        return yaml.load(stream, OrderedLoader)


class SopsYAMLBackend(YAMLBackend):
    NAME = "yaml.enc"

    def read_file(self, path):
        return subprocess.check_output(
            ["sops", "--input-type=yaml", "--output-type=yaml", "-d", path]
        )


class JSONBackend(Backend):
    NAME = "json"

    def load(self, data):
        return json.loads(data, object_pairs_hook=LookupDict)
