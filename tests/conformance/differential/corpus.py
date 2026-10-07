"""The recorded corpus: Puppet's answers for a few fixed seeds, replayed without
Puppet.

``corpus/<area>.jsonl`` is JSON Lines. The first line is the header (format,
the area, the seed and the sizes, Puppet's and Ruby's versions, the gem
versions); then, for each scenario, a ``{"scn", "sha"}`` line pinning the tree
the answers were recorded for, followed by one ``{"q", "p", "k"?, "r"?}`` line
per recorded query: its id, Puppet's outcome and, when it is not a plain
agreement, the kind and the declared-difference id. A replay rebuilds the
scenarios from the seed, checks each digest, runs hyera and expects the same
verdict.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple

from record import _leak_scan

from .generate import build, make_jobs
from .scenario import Scn

CORPUS = Path(__file__).resolve().parent / "corpus"
FORMAT = 1
#: Bytes the corpus may take on disk; ``write`` refuses to exceed it.
SIZE_LIMIT = 1_000_000


class Plan(NamedTuple):
    """The fixed inputs of one area's corpus."""

    seed: int
    count: int
    max_queries: int


#: area -> the seed, the scenarios kept and the queries kept per scenario.
PLAN: Dict[str, Plan] = {
    "backends": Plan(1, 14, 6),
    "config": Plan(1, 24, 3),
    "extra": Plan(1, 14, 4),
    "interp": Plan(1, 8, 12),
    "interp_sweep": Plan(1, 3, 40),
    "keys": Plan(1, 3, 40),
    "layers": Plan(1, 10, 8),
    "locations": Plan(1, 12, 6),
    "lopts": Plan(1, 14, 5),
    "strategies": Plan(1, 24, 6),
    "yaml_data": Plan(1, 24, 3),
}
#: The merge area: the seed and the number of ``values`` cases.
MERGE_PLAN = Plan(1, 300, 0)


class Recording:
    """What one scenario area's run records."""

    def __init__(self, area: str, seed: int, count: int, rows: list):
        self.area = area
        self.seed = seed
        self.count = count
        self.rows = rows


class MergeRecording:
    """What the merge area's run records."""

    area = "merge"

    def __init__(self, seed: int, count: int, cases, ruby, verdicts):
        self.seed = seed
        self.count = count
        self.cases = cases
        self.ruby = ruby
        self.verdicts = verdicts


def _line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sample_queries(scn: Scn, area: str, seed: int, max_queries: int) -> List[str]:
    """The ids of the queries kept for a scenario: all of them, or a seeded
    sample in the scenario's own order."""
    ids = [q["id"] for q in scn.queries]
    if len(ids) <= max_queries:
        return ids
    pick = random.Random("{}:{}:{}:q".format(seed, area, scn.name)).sample(
        range(len(ids)), max_queries
    )
    return [ids[i] for i in sorted(pick)]


def _short_id(job: dict) -> str:
    """``scn::qid[@render]``: a job id without its area."""
    return job["id"].split("/", 1)[1]


def header(area: str, plan: Plan, versions: dict) -> dict:
    v = versions["versions"]
    return {
        "format": FORMAT,
        "area": area,
        "seed": plan.seed,
        "count": plan.count,
        "max_queries": plan.max_queries,
        "puppet": v["puppet"],
        "ruby": v["ruby"],
        "gems": v["gems"],
        "node": "golden.example.com",
    }


def scenario_lines(rec: Recording, plan: Plan) -> List[str]:
    """The corpus lines for a scenario area's run."""
    scenarios = {s.name: s for s in build(rec.area, rec.seed, rec.count)}
    keep: Dict[str, set] = {
        name: set(sample_queries(s, rec.area, rec.seed, plan.max_queries))
        for name, s in scenarios.items()
    }
    lines: List[str] = []
    current = None
    for row in rec.rows:
        job = row.job
        if job["qid"] not in keep[job["scn"]]:
            continue
        if job["scn"] != current:
            current = job["scn"]
            lines.append(
                _line({"scn": current, "sha": scenarios[current].digest()[:16]})
            )
        entry: dict = {"q": _short_id(job), "p": row.puppet}
        if row.kind != "AGREE":
            entry["k"] = row.kind
        if row.rule:
            entry["r"] = row.rule
        lines.append(_line(entry))
    return lines


def merge_lines(rec: MergeRecording) -> List[str]:
    lines = []
    for case, ruby, (kind, rule) in zip(rec.cases, rec.ruby, rec.verdicts):
        entry: dict = {
            "i": case["id"],
            "o": {k: v for k, v in ruby.items() if k != "id"},
        }
        if kind != "AGREE":
            entry["k"] = kind
        if rule:
            entry["r"] = rule
        lines.append(_line(entry))
    return lines


def write(recordings: list, versions: dict) -> None:
    """Write ``corpus/<area>.jsonl`` for each recording.

    :param recordings: :class:`Recording` and :class:`MergeRecording` objects.
    :param versions: from :func:`differential.reference.reference_versions`.
    :raises SystemExit: when a record holds the recording host's name or a
        private path, or the corpus would be over :data:`SIZE_LIMIT`.
    """
    CORPUS.mkdir(exist_ok=True)
    texts: Dict[str, str] = {}
    for rec in recordings:
        if isinstance(rec, MergeRecording):
            plan = MERGE_PLAN
            body = merge_lines(rec)
        else:
            plan = PLAN[rec.area]
            body = scenario_lines(rec, plan)
        head = header(rec.area, Plan(rec.seed, rec.count, plan.max_queries), versions)
        for line in body:
            hits = _leak_scan(json.loads(line), "", versions["identities"])
            if hits:
                raise SystemExit(
                    "refusing to record a leaking result in {}: {} ({})".format(
                        rec.area, hits, line[:120]
                    )
                )
        texts[rec.area] = "\n".join([_line(head)] + body) + "\n"
    total = sum(len(t.encode("utf-8")) for t in texts.values())
    if total > SIZE_LIMIT:
        raise SystemExit(
            "the corpus would be {} bytes, over {}".format(total, SIZE_LIMIT)
        )
    for area, text in texts.items():
        (CORPUS / (area + ".jsonl")).write_bytes(text.encode("utf-8"))


def read(area: str) -> Tuple[dict, List[dict]]:
    """The header and the remaining lines of ``corpus/<area>.jsonl``."""
    path = CORPUS / (area + ".jsonl")
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(line) for line in lines[1:]]


def entries(area: str) -> Iterator[Tuple[Optional[str], dict]]:
    """``(scenario name, entry)`` for each query line of a scenario area."""
    _, lines = read(area)
    current = None
    for line in lines:
        if "scn" in line:
            current = line["scn"]
            continue
        yield current, line
