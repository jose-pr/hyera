"""Replay the recorded differential corpus: hyera against Puppet's recorded answers.

No Puppet is needed. Each area's scenarios are rebuilt from the recorded seed in a
temporary directory, hyera answers every recorded query, and the verdict must be
the recorded one: an agreement stays an agreement, and a query that was explained
by a declared difference stays explained by it, so hyera's declared behaviour is
asserted and not Puppet's answer. Re-record with ``differential/run.py
--record-corpus`` after a change to the generator or a new difference.
"""

from __future__ import annotations

import pytest

from _golden import _leak_hits, string_leaves

from differential import corpus, drive_hyera, outcomes
from differential.generate import build, make_jobs, write_area
from differential.merge import ours as merge_ours
from differential.merge.run import build_cases
from differential.rules import OPEN, OPEN_PREFIX, RULES, judge
from differential.scenarios import AREAS

RERECORD = "re-record the corpus with differential/run.py --record-corpus"


def _recorded(area):
    header, lines = corpus.read(area)
    sha = {line["scn"]: line["sha"] for line in lines if "scn" in line}
    entries = [line for line in lines if "q" in line]
    return header, sha, entries


@pytest.mark.parametrize("area", AREAS)
def test_replay_matches_the_recorded_verdicts(area, tmp_path):
    header, sha, entries = _recorded(area)
    plan = corpus.PLAN[area]
    assert header["format"] == corpus.FORMAT
    assert (
        header["seed"],
        header["count"],
        header["agree"],
        header["per_rule"],
    ) == tuple(plan)
    scenarios = [
        s for s in build(area, header["seed"], header["count"]) if s.name in sha
    ]
    assert sorted(sha) == [s.name for s in scenarios], RERECORD
    for scn in scenarios:
        assert scn.digest()[:16] == sha[scn.name], "{} changed: {}".format(
            scn.name, RERECORD
        )
    write_area(scenarios, tmp_path)
    by_short_id = {job["id"].split("/", 1)[1]: job for job in make_jobs(scenarios)}
    jobs = []
    for entry in entries:
        assert entry["q"] in by_short_id, "{} is gone: {}".format(entry["q"], RERECORD)
        jobs.append(by_short_id[entry["q"]])
    wrong = []
    for job, entry, raw in zip(
        jobs, entries, drive_hyera.run_jobs(jobs, tmp_path, workers=1)
    ):
        render = job.get("render")
        local = str(tmp_path / job["dir"])
        hyera = outcomes.hyera_outcomes(raw, local, render)
        got = judge(job, entry["p"], hyera)
        want = (entry.get("k", "AGREE"), entry.get("r"))
        if got != want:
            wrong.append("{}: recorded {}, now {}".format(job["id"], want, got))
    assert not wrong, "\n".join(wrong[:20])


def test_replay_of_the_merge_corpus():
    header, lines = corpus.read("merge")
    assert header["format"] == corpus.FORMAT
    assert (header["seed"], header["count"]) == (
        corpus.MERGE_PLAN.seed,
        corpus.MERGE_PLAN.count,
    )
    cases = build_cases(header["seed"], header["count"])
    assert [line["i"] for line in lines] == [c["id"] for c in cases], RERECORD
    wrong = []
    for case, line, mine in zip(cases, lines, merge_ours.run_cases(cases)):
        ruby = dict(line["o"], id=case["id"])
        got = merge_ours.judge(case, ruby, mine)
        want = (line.get("k", "AGREE"), line.get("r"))
        if got != want:
            wrong.append("{}: recorded {}, now {}".format(case["id"], want, got))
    assert not wrong, "\n".join(wrong[:20])


def test_the_corpus_headers_record_the_reference():
    for area in list(AREAS) + ["merge"]:
        header, _ = corpus.read(area)
        assert header["puppet"] == "8.10.0"
        assert header["ruby"]
        assert "deep_merge" in header["gems"]


def test_the_corpus_holds_no_unclassified_disagreement():
    for area in AREAS:
        _, _, entries = _recorded(area)
        assert entries
        for entry in entries:
            assert entry.get("k") is None or entry.get("r"), entry["q"]


def test_the_corpus_reaches_every_declared_difference_and_open_defect():
    seen = set()
    for area in list(AREAS) + ["merge"]:
        _, lines = corpus.read(area)
        seen.update(line["r"] for line in lines if "r" in line)
    expected = {rule.id for rule in RULES} | {OPEN_PREFIX + d.key for d in OPEN}
    expected |= {"error-message-text", "aio-hash-rendering"}
    # its only generated query depends on how deep the interpreter's stack is
    expected -= {"nesting-bound"}
    assert expected - seen == set(), RERECORD


def test_the_corpus_leaks_no_host_or_private_path():
    for area in list(AREAS) + ["merge"]:
        header, lines = corpus.read(area)
        for line in [header] + lines:
            assert not _leak_hits(string_leaves(line)), (area, line)


def test_the_corpus_stays_under_a_megabyte():
    total = sum(p.stat().st_size for p in corpus.CORPUS.glob("*.jsonl"))
    assert total <= corpus.SIZE_LIMIT
