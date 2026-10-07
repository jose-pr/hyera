"""hyera's side of the merge comparison, and the comparison itself.

The strategies are reached through ``MergeStrategy``, the port of Puppet's own
class, because the public API only names the four strategies Puppet exposes and
the comparison also covers ``unconstrained_deep`` and ``reverse_deep`` with
every option of the deep_merge gem.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

from hyera._lookup.merge_strategy import MergeStrategy
from hyera._lookup.navigation import _MISSING

#: The classification of a pair of results.
Verdict = Tuple[str, Optional[str]]


def decode(j):
    """The inverse of :func:`differential.merge.gen.encode`."""
    if isinstance(j, list):
        return [decode(x) for x in j]
    if isinstance(j, dict):
        if "f" in j:
            return float(j["f"].replace("Infinity", "inf"))
        if "m" in j:
            return _MISSING
        return {decode(k): decode(v) for k, v in j["h"]}
    return j


def encode(v):
    if v is _MISSING:
        return {"m": 1}
    if isinstance(v, float):
        return {"f": repr(v)}
    if isinstance(v, list):
        return [encode(x) for x in v]
    if isinstance(v, dict):
        return {"h": [[encode(k), encode(x)] for k, x in v.items()]}
    return v


def run_cases(cases: List[dict]) -> List[dict]:
    """Merge every case with hyera's strategies: ``{"id", "res"}`` or
    ``{"id", "err": [class, message]}``."""
    out = []
    for case in cases:
        result: dict = {"id": case["id"]}
        try:
            variants = [decode(v) for v in case["variants"]]
            strategy = MergeStrategy.strategy(case["spec"])
            result["res"] = encode(strategy.lookup(variants, lambda v: v, None))
        except RecursionError as e:
            result["err"] = ["RecursionError", str(e)]
        except Exception as e:  # noqa: BLE001 - any failure is a result to compare
            result["err"] = [type(e).__name__, str(e)]
        out.append(result)
    return out


def normal(j) -> str:
    """Canonical text of an encoded value, floats spelled the Python way."""

    def fix(x):
        if isinstance(x, list):
            return [fix(y) for y in x]
        if isinstance(x, dict):
            if "f" in x:
                return {"f": repr(float(x["f"].replace("Infinity", "inf")))}
            if "h" in x:
                return {"h": [[fix(k), fix(v)] for k, v in x["h"]]}
        return x

    return json.dumps(fix(j), ensure_ascii=False).replace(" => ", "=>")


def _ruby_key(k) -> tuple:
    return (type(k).__name__, repr(k))


def has_python_equal_keys(case: dict) -> bool:
    """Whether the case holds two hash keys Ruby keeps apart and Python equates
    (``1``, ``1.0`` and ``true``)."""
    seen: Dict[object, set] = {}

    def walk(j) -> None:
        if isinstance(j, list):
            for x in j:
                walk(x)
        elif isinstance(j, dict) and "h" in j:
            for k, v in j["h"]:
                key = decode(k)
                if not isinstance(key, (list, dict)):
                    seen.setdefault(key, set()).add(_ruby_key(key))
                walk(k)
                walk(v)

    for variant in case["variants"]:
        walk(variant)
    return any(len(kinds) > 1 for kinds in seen.values())


def judge(case: dict, ruby: dict, ours: dict) -> Verdict:
    """Compare Puppet's merge with hyera's.

    :returns: ``("AGREE", None)``, ``("AGREE", "error-message-text")`` when both
        fail with different text, ``(kind, id)`` for a declared difference and
        ``(kind, None)`` for an unclassified one. Kinds are ``VALUE``,
        ``OURS-RAISES`` and ``RUBY-RAISES``.
    """
    if "res" in ruby and "res" in ours:
        if normal(ruby["res"]) == normal(ours["res"]):
            return ("AGREE", None)
        kind = "VALUE"
    elif "err" in ruby and "err" in ours:
        same = ruby["err"][1].replace(" => ", "=>") == ours["err"][1].replace(
            " => ", "=>"
        )
        return ("AGREE", None if same else "error-message-text")
    elif "err" in ours:
        kind = "OURS-RAISES"
    else:
        kind = "RUBY-RAISES"
    if has_python_equal_keys(case):
        return (kind, "python-equal-hash-keys")
    return (kind, None)
