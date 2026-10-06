"""Golden-case loaders and helpers shared by the Puppet type model test modules."""

import json
from pathlib import Path

import yaml

_CASES = Path(__file__).parent / "conformance" / "cases"


def _golden(case):
    return json.loads((_CASES / case / "golden.json").read_text(encoding="utf-8"))


def _data(case):
    return yaml.safe_load(
        (_CASES / case / "data" / "common.yaml").read_text(encoding="utf-8")
    )
