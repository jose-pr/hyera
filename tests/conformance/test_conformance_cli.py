"""Replay the recorded Puppet goldens against hyera's CLI, in-process.

``hyera.cli.main`` does not accept ``puppet lookup``'s own flags today, so
every query here is a strict xfail under `_ours.CLI_CHANNEL_DIVERGENCE`
until `cli_puppet_lookup_parity` teaches it to (that plan sets the
constant to ``None`` in the same commit it removes this file's blanket
marker).
"""

import pytest

pytest.importorskip("duho")

from _golden import case_dirs, load_case, query_id, read_golden
from _ours import CLI_CHANNEL_DIVERGENCE, canonical, expected, run_cli


def _cli_params():
    for case_dir in case_dirs():
        case = load_case(case_dir)
        for req in case.get("requires") or []:
            pytest.importorskip(req)
        for query in case["queries"]:
            marks = []
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

    actual = run_cli(case_dir, case, query, golden)

    assert actual["status"] == want["status"], (actual, want)
    if want["status"] == "found":
        assert canonical(actual["value"], query.get("ordered")) == canonical(
            want["value"], query.get("ordered")
        )
