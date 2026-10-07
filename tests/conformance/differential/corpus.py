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
import sys
from pathlib import Path
from typing import Dict, Iterator, List, NamedTuple, Optional, Tuple

from record import _leak_scan

from .generate import build

CORPUS = Path(__file__).resolve().parent / "corpus"
FORMAT = 1
#: Bytes the corpus may take on disk; ``write`` refuses to exceed it.
SIZE_LIMIT = 1_000_000


class Plan(NamedTuple):
    """The fixed inputs of one area's corpus.

    ``agree`` queries that agree plainly and ``per_rule`` queries of each
    classified kind of disagreement are kept, chosen by seed from the whole area.
    """

    seed: int
    count: int
    agree: int
    per_rule: int


#: area -> seed, scenarios generated, plain agreements kept, queries kept per rule.
PLAN: Dict[str, Plan] = {
    "backends": Plan(1, 200, 60, 5),
    "config": Plan(1, 200, 60, 5),
    "extra": Plan(1, 200, 40, 5),
    "interp": Plan(1, 200, 60, 5),
    "interp_sweep": Plan(1, 200, 60, 5),
    "keys": Plan(1, 200, 60, 5),
    "layers": Plan(1, 200, 60, 5),
    "locations": Plan(1, 200, 50, 5),
    "lopts": Plan(1, 200, 60, 5),
    "strategies": Plan(1, 200, 75, 5),
    "yaml_data": Plan(1, 200, 75, 5),
}
#: The merge area: the seed and the number of ``values`` cases; all are kept.
MERGE_PLAN = Plan(1, 300, 0, 0)


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
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":"))


def select(rows: list, plan: Plan, area: str) -> set:
    """The ids of the jobs a corpus keeps: a seeded sample of the plain
    agreements, and a seeded few of each (kind, rule) group."""
    groups: Dict[tuple, List[str]] = {}
    for row in rows:
        groups.setdefault((row.kind, row.rule or ""), []).append(row.job["id"])
    kept: set = set()
    for key in sorted(groups):
        ids = sorted(groups[key])
        want = plan.agree if key == ("AGREE", "") else plan.per_rule
        label = "{}:{}:{}:{}".format(plan.seed, area, key[0], key[1])
        kept.update(random.Random(label).sample(ids, min(want, len(ids))))
    return kept


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
        "agree": plan.agree,
        "per_rule": plan.per_rule,
        "puppet": v["puppet"],
        "ruby": v["ruby"],
        "gems": v["gems"],
        "node": "golden.example.com",
    }


def scenario_lines(rec: Recording, plan: Plan, identities: tuple) -> List[str]:
    """The corpus lines for a scenario area's run.

    A query whose answer the leak scan refuses (a path-like string in the data,
    a host's name) is left out and named on standard error: it stays covered by
    a live run, and the scan still guards everything that is written.
    """
    scenarios = {s.name: s for s in build(rec.area, rec.seed, rec.count)}
    clean = []
    for row in rec.rows:
        if row.job.get("volatile"):
            continue
        if _leak_scan(row.puppet, "", identities):
            print(
                "not recorded, the answer looks like a path or a host:",
                row.job["id"],
                file=sys.stderr,
            )
        else:
            clean.append(row)
    keep = select(clean, plan, rec.area)
    lines: List[str] = []
    current = None
    for row in clean:
        job = row.job
        if job["id"] not in keep:
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
            body = scenario_lines(rec, plan, versions["identities"])
        head = header(rec.area, plan, versions)
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
    lines = path.read_bytes().decode("utf-8").split("\n")[:-1]
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
