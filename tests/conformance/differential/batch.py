"""One comparison run: generate an area, drive both sides, judge every query."""

from __future__ import annotations

import collections
import json
import shutil
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional

from . import drive_hyera, outcomes, reference
from .generate import build, make_jobs, write_area
from .rules import OPEN_PREFIX, judge


class Row(NamedTuple):
    """One query's result: both outcomes and the verdict."""

    job: dict
    puppet: dict
    hyera: dict
    kind: str
    rule: Optional[str]

    @property
    def unclassified(self) -> bool:
        return self.kind != "AGREE" and self.rule is None


def evaluate(
    runner: str,
    root: Path,
    jobs: List[dict],
    puppet_raw: List[dict],
    hyera_raw: List[dict],
) -> List[Row]:
    """Normalise both sides' raw answers and judge each job.

    :param runner: the reference's runner (its paths differ from this machine's).
    :param root: the directory the jobs' ``dir`` entries are relative to.
    """
    rows = []
    for job, praw, hraw in zip(jobs, puppet_raw, hyera_raw):
        render = job.get("render")
        local = str(Path(root) / job["dir"])
        remote = reference.to_runner_path(runner, Path(root) / job["dir"])
        puppet = outcomes.puppet_outcome(praw, remote, render)
        hyera = outcomes.hyera_outcomes(hraw, local, render)
        kind, rule = judge(job, puppet, hyera)
        rows.append(Row(job, puppet, hyera, kind, rule))
    return rows


def puppet_job(job: dict, root: Path) -> dict:
    """The part of a job the Puppet driver reads, with an absolute directory."""
    item = {"id": job["id"], "dir": str(Path(root) / job["dir"]), "argv": job["argv"]}
    if job.get("render"):
        item["render"] = job["render"]
    return item


def run_area(
    runner: str,
    area: str,
    seed: int,
    count: int,
    root: Path,
    puppet_workers: int = 8,
    hyera_workers: int = 1,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Row]:
    """Generate ``area``, run Puppet and hyera over it and judge every query.

    :param root: where the trees and the driver files are written.
    :raises reference.ReferenceError: when the reference cannot be run.
    """
    scenarios = build(area, seed, count)
    write_area(scenarios, root)
    jobs = make_jobs(scenarios)
    puppet_jobs = [puppet_job(j, root) for j in jobs]
    puppet_raw = reference.run_puppet(
        runner, puppet_jobs, Path(root) / "_work" / area, puppet_workers, progress
    )
    hyera_raw = drive_hyera.run_jobs(jobs, Path(root), hyera_workers)
    return evaluate(runner, Path(root), jobs, puppet_raw, hyera_raw)


def summarize(rows: List[Row]) -> Dict[str, object]:
    """Counts for one area's summary line."""
    declared: collections.Counter = collections.Counter()
    open_defects: collections.Counter = collections.Counter()
    agree = 0
    unclassified = 0
    for row in rows:
        if row.unclassified:
            unclassified += 1
        elif row.rule and row.rule.startswith(OPEN_PREFIX):
            open_defects[row.rule[len(OPEN_PREFIX) :]] += 1
        elif row.rule:
            declared[row.rule] += 1
        else:
            agree += 1
    return {
        "scenarios": len({r.job["scn"] for r in rows}),
        "queries": len(rows),
        "agree": agree,
        "declared": dict(sorted(declared.items())),
        "open": dict(sorted(open_defects.items())),
        "unclassified": unclassified,
    }


def describe(row: Row, limit: int = 500) -> str:
    """A disagreement in full: the argument tail and each side's outcome."""

    def show(outcome) -> str:
        return json.dumps(outcome, ensure_ascii=False, sort_keys=True)[:limit]

    return "\n".join(
        [
            "{} [{}]".format(row.job["id"], row.kind),
            "  argv:   {}".format(" ".join(row.job["argv"])),
            "  puppet: {}".format(show(row.puppet)),
            "  api:    {}".format(show(row.hyera["api"])),
            "  cli:    {}".format(show(row.hyera["cli"])),
        ]
    )


def discard(path: Path) -> None:
    shutil.rmtree(str(path), ignore_errors=True)
