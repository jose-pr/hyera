"""Deterministic scenario generation: ``build(area, seed, count)``.

An area is the scenarios of its builders, sorted by name. When there are more
than ``count`` of them a seeded sample is kept, so a seed picks both the random
data inside the builders and which scenarios a small run keeps. Nothing here
depends on set order, ``hash()`` or the locale.
"""

from __future__ import annotations

import random
from pathlib import Path, PurePosixPath
from typing import Dict, List

from .scenario import REGISTRY, Ctx, Scn, iter_builders, lookup_argv
from .scenarios import AREAS


def build(area: str, seed: int, count: int) -> List[Scn]:
    """The scenarios one area keeps for ``seed`` and ``count``, sorted by name.

    :param area: one of :data:`differential.scenarios.AREAS`.
    :param seed: the seed every builder draws from.
    :param count: the most scenarios the area keeps (at least 1).
    :returns: scenarios with unique, case-insensitively unique names.
    :raises ValueError: when the area is unknown or a name repeats.
    """
    if area not in REGISTRY:
        raise ValueError("unknown area {!r}".format(area))
    if count < 1:
        raise ValueError("count must be at least 1")
    ctx = Ctx(seed, count, area)
    found: Dict[str, Scn] = {}
    seen_folded: Dict[str, str] = {}
    for builder in iter_builders(area):
        for scn in builder(ctx):
            scn.area = area
            folded = scn.name.lower()
            if folded in seen_folded:
                raise ValueError(
                    "scenario {!r} repeats {!r} in area {}".format(
                        scn.name, seen_folded[folded], area
                    )
                )
            seen_folded[folded] = scn.name
            found[scn.name] = scn
    names = sorted(found)
    if len(names) > count:
        picked = random.Random("{}:{}:select".format(seed, area)).sample(names, count)
        names = sorted(picked)
    return [found[name] for name in names]


def build_all(seed: int, count: int, areas=AREAS) -> Dict[str, List[Scn]]:
    """:func:`build` for each of ``areas``, keyed by area."""
    return {area: build(area, seed, count) for area in areas}


def write_area(scenarios: List[Scn], root: Path) -> Dict[str, Path]:
    """Write each scenario under ``root/<area>`` and return ``{name: directory}``."""
    return {scn.name: scn.write(Path(root) / scn.area) for scn in scenarios}


def make_jobs(scenarios: List[Scn]) -> List[dict]:
    """One job per query, plus one per extra ``render`` format of the scenario.

    A job is ``{"id", "area", "scn", "qid", "dir", "query", "argv"}`` and, for a
    render variant, ``"render"``. ``dir`` is relative to the root the trees were
    written under. Ids are unique across the list.
    """
    jobs: List[dict] = []
    for scn in scenarios:
        base = "{}/{}".format(scn.area, scn.name)
        exts = sorted({PurePosixPath(rel).suffix.lower() for rel in scn.files} - {""})
        for query in scn.queries:
            for render in (None,) + tuple(scn.render):
                job = {
                    "id": "{}::{}".format(base, query["id"])
                    + ("@" + render if render else ""),
                    "area": scn.area,
                    "scn": scn.name,
                    "qid": query["id"],
                    "dir": base,
                    "exts": exts,
                    "query": query,
                    "argv": lookup_argv(query),
                }
                if render:
                    job["render"] = render
                jobs.append(job)
    return jobs
