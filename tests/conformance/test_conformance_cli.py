"""Replay the recorded Puppet goldens against hyera's CLI, in-process.

Every CLI query replays the golden's own ``puppet lookup`` argv; divergence markers
apply to both channels.
"""

import difflib

import pytest

pytest.importorskip("duho")

import _golden
from _golden import case_dirs, load_case, missing_requirements, query_id, read_golden
from _ours import (
    CLI_CHANNEL_DIVERGENCE,
    canonical,
    expected,
    is_ordered,
    run_cli,
    run_cli_explain,
)


def test_cli_server_version_matches_oracle():
    import hyera.cli

    assert hyera.cli._PUPPET_VERSION == _golden.ORACLE["puppet"]


def _cli_params():
    for case_dir in case_dirs():
        case = load_case(case_dir)
        missing = missing_requirements(case)
        for query in case["queries"]:
            marks = []
            if missing:
                marks.append(pytest.mark.skip(reason="needs " + ", ".join(missing)))
            if CLI_CHANNEL_DIVERGENCE:
                marks.append(
                    pytest.mark.xfail(strict=True, reason=CLI_CHANNEL_DIVERGENCE)
                )
            yield pytest.param(
                case_dir,
                case,
                query,
                id="{}::{}".format(case_dir.name, query_id(query)),
                marks=marks,
            )


@pytest.mark.parametrize("case_dir,case,query", list(_cli_params()))
def test_cli_matches_puppet(case_dir, case, query):
    golden = read_golden(case_dir)
    golden_result = golden["results"][query_id(query)]
    want = expected(query, golden_result)

    runner = run_cli_explain if query.get("explain") else run_cli
    actual = runner(case_dir, case, query, golden)

    assert actual["status"] == want["status"], (actual, want)
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
