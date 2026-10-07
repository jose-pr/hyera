"""Replay the recorded Puppet goldens against hyera's Python API.

Needs no Puppet. A query marked ``divergence:`` runs as a strict xfail; errors are
asserted by status only; every warning Puppet recorded must also be logged.
"""

import difflib
import re
import sys

import pytest

import hyera
from _golden import (
    RUNTIME_PREDICATES,
    case_dirs,
    lint_case,
    load_case,
    missing_requirements,
    query_id,
    read_golden,
)
from _ours import (
    AdapterUnsupported,
    canonical,
    expected,
    is_ordered,
    missing_warnings,
    run_api,
    run_expression,
    run_explain,
)


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
        missing = missing_requirements(case)
        for query in case["queries"]:
            marks = _divergence_marks(query)
            if missing:
                marks.append(pytest.mark.skip(reason="needs " + ", ".join(missing)))
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
    if "expression" in query:
        runner = run_expression
    else:
        runner = run_explain if query.get("explain") else run_api
    try:
        actual = runner(case_dir, case, query, golden)
    except AdapterUnsupported as e:
        pytest.fail("adapter does not support this query yet: {}".format(e))

    assert actual["status"] == want["status"], (query_id(query), actual, want)
    ordered = is_ordered(query)
    if want["status"] == "found":
        assert canonical(actual["value"], ordered) == canonical(want["value"], ordered)
    elif want["status"] == "explained":
        assert canonical(actual["tree"], ordered) == canonical(want["tree"], ordered)
        if actual["text"] != want["text"]:
            diff = "\n".join(
                difflib.unified_diff(
                    want["text"],
                    actual["text"],
                    lineterm="",
                    fromfile="puppet",
                    tofile="ours",
                )
            )
            pytest.fail(diff)
    elif want["status"] == "error":
        error_match = query.get("error_match")
        if error_match:
            assert re.search(error_match, actual.get("message", "")), actual.get(
                "message"
            )
        error_class = query.get("error_class")
        if error_class:
            assert actual.get("exc_class") == getattr(hyera, error_class).__name__
    if not query.get("deviation") and not query.get("explain"):
        lost = missing_warnings(golden_result, actual, case_dir)
        assert not lost, "Puppet warned, hyera did not: {}".format(lost)
