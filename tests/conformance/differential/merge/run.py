"""Run the merge comparison: generate cases, merge with Puppet and with hyera."""

from __future__ import annotations

import collections
import json
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from record import _command

from ..corpus import MergeRecording
from ..reference import ReferenceError, to_runner_path
from . import ours
from .gen import MODES, generate

ORACLE = Path(__file__).resolve().parent / "oracle.rb"
#: Cases in ``keys`` mode per case in ``values`` mode.
KEYS_FRACTION = 4


def build_cases(seed: int, count: int) -> List[dict]:
    """``count`` ``values`` cases and ``count // 4`` ``keys`` cases."""
    cases = generate(seed, count, "values")
    cases += generate(seed, max(1, count // KEYS_FRACTION), "keys")
    return cases


def run_oracle(runner: str, cases: List[dict], work: Path) -> List[dict]:
    """Merge every case with Puppet's ``MergeStrategy`` on the reference.

    :raises ReferenceError: when the reference fails or answers for other cases.
    """
    work.mkdir(parents=True, exist_ok=True)
    cases_file = work / "merge-cases.jsonl"
    out_file = work / "merge-results.jsonl"
    text = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)
    cases_file.write_bytes(text.encode("utf-8"))
    if out_file.exists():
        out_file.unlink()
    args = [
        to_runner_path(runner, ORACLE),
        to_runner_path(runner, cases_file),
        to_runner_path(runner, out_file),
    ]
    cmd, cwd = _command(runner, work, args, program="ruby")
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ReferenceError("cannot run the merge oracle: {}".format(e))
    if done.returncode != 0 or not out_file.exists():
        raise ReferenceError(
            "the merge oracle exited {}: {}".format(
                done.returncode, done.stderr.decode("utf-8", "replace")[-400:]
            )
        )
    lines = out_file.read_text(encoding="utf-8").splitlines()
    results = [json.loads(line) for line in lines]
    if [r["id"] for r in results] != [c["id"] for c in cases]:
        raise ReferenceError("the merge oracle answered for different cases")
    return results


def judge_all(cases: List[dict], ruby: List[dict], theirs: List[dict]):
    """``[(kind, rule)]`` for each case."""
    return [ours.judge(c, r, o) for c, r, o in zip(cases, ruby, theirs)]


def describe(case: dict, ruby: dict, mine: dict, kind: str) -> str:
    return "\n".join(
        [
            "merge/{} [{}] spec {}".format(case["id"], kind, json.dumps(case["spec"])),
            "  variants: {}".format(
                json.dumps(case["variants"], ensure_ascii=False)[:500]
            ),
            "  puppet:   {}".format(json.dumps(ruby, ensure_ascii=False)[:500]),
            "  hyera:    {}".format(json.dumps(mine, ensure_ascii=False)[:500]),
        ]
    )


def run(
    runner: str,
    seed: int,
    count: int,
    root: Path,
    progress: Optional[Callable[[str], None]] = None,
) -> Tuple[dict, List[str], MergeRecording]:
    """The merge area for ``run.py``: the summary, the unclassified cases in
    full and what a corpus records."""
    cases = build_cases(seed, count)
    ruby = run_oracle(runner, cases, Path(root) / "_work" / "merge")
    mine = ours.run_cases(cases)
    verdicts = judge_all(cases, ruby, mine)
    declared: collections.Counter = collections.Counter()
    agree = 0
    bad = []
    for case, r, o, (kind, rule) in zip(cases, ruby, mine, verdicts):
        if kind != "AGREE" and rule is None:
            bad.append(describe(case, r, o, kind))
        elif rule:
            declared[rule] += 1
        else:
            agree += 1
    stats = {
        "scenarios": len(MODES),
        "queries": len(cases),
        "agree": agree,
        "declared": dict(sorted(declared.items())),
        "open": {},
        "unclassified": len(bad),
    }
    return stats, bad, MergeRecording(seed, count, cases, ruby, verdicts)
