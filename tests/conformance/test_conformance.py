"""Replay the recorded Puppet goldens against pyera's Python API.

Needs no Puppet: every expectation comes from ``cases/<case>/golden.json``,
written by ``record.py``. A query marked ``divergence:`` in ``case.yaml``
is a known, explained difference and runs as a strict xfail, so fixing it
turns the run red until the marker is removed (XPASS(strict) fails).
"""

import re
import sys

import pytest

import pyera
from _golden import (
    RUNTIME_PREDICATES,
    case_dirs,
    load_case,
    query_id,
    read_golden,
    lint_case,
)
from _ours import AdapterUnsupported, canonical, expected, run_api


def _divergence_marks(query):
    d = query.get("divergence")
    if not d:
        return []
    entries = d if isinstance(d, list) else [d]
    ids = []
    skip_platforms = set()
    when_keys = set()
    for entry in entries:
        if isinstance(entry, dict):
            ids.append(entry["id"])
            if "on" in entry:
                skip_platforms.add(tuple(entry["on"]))
            if "when" in entry:
                when_keys.add(entry["when"])
        else:
            ids.append(entry)

    condition = True
    if skip_platforms:
        condition = any(sys.platform in platforms for platforms in skip_platforms)
    if when_keys:
        # Evaluated fresh every run (never cached), so a dependency
        # reinstall between CI legs is picked up rather than pinned to
        # whatever was true at collection time in some other process.
        condition = condition and all(RUNTIME_PREDICATES[w]() for w in when_keys)
    return [pytest.mark.xfail(condition, strict=True, reason=",".join(ids))]


def _lint_params():
    for case_dir in case_dirs():
        yield pytest.param(case_dir, id=case_dir.name)


@pytest.mark.parametrize("case_dir", list(_lint_params()))
def test_case_is_current(case_dir):
    problems = lint_case(case_dir)
    assert not problems, "\n".join(problems)


def _api_params():
    for case_dir in case_dirs():
        case = load_case(case_dir)
        for req in case.get("requires") or []:
            pytest.importorskip(req)
        for query in case["queries"]:
            marks = _divergence_marks(query)
            yield pytest.param(
                case_dir,
                case,
                query,
                id="{}::{}".format(case_dir.name, query_id(query)),
                marks=marks,
            )


@pytest.mark.parametrize("case_dir,case,query", list(_api_params()))
def test_api_matches_puppet(case_dir, case, query):
    golden = read_golden(case_dir)
    golden_result = golden["results"][query_id(query)]
    want = expected(query, golden_result)
    try:
        actual = run_api(case_dir, case, query, golden)
    except AdapterUnsupported as e:
        pytest.fail("adapter does not support this query yet: {}".format(e))

    assert actual["status"] == want["status"], (actual, want)
    if want["status"] == "found":
        assert canonical(actual["value"], query.get("ordered")) == canonical(
            want["value"], query.get("ordered")
        )
    elif want["status"] == "error":
        error_match = query.get("error_match")
        if error_match:
            assert re.search(error_match, actual.get("message", "")), actual.get(
                "message"
            )
        error_class = query.get("error_class")
        if error_class:
            assert actual.get("exc_class") == getattr(pyera, error_class).__name__
