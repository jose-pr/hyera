"""The per-lookup ``DEBUG`` trace (Puppet's ``DebugExplainer``) and the two
logging leftovers this phase also fixes: an earlier dotted-only miss log
(already gone by the time this landed) and ``cli.py``'s hard-coded
package-name logger.
"""

import logging

import pytest

from hyera import Hiera


def _explain_records(caplog):
    return [r for r in caplog.records if r.name == "hyera._output.explain"]


def test_lookup_logs_one_trace(make_tree, caplog):
    caplog.set_level(logging.DEBUG, logger="hyera")
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    h.lookup("k")
    records = _explain_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    lines = message.splitlines()
    assert lines[0] == "Lookup of 'k'"
    assert all(line.startswith("  ") for line in lines[1:])


def test_trace_matches_explain_report(make_tree, caplog):
    caplog.set_level(logging.DEBUG, logger="hyera")
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    h.lookup("k")
    trace_lines = _explain_records(caplog)[0].getMessage().splitlines()[1:]
    report_lines = h.explain("k").text().splitlines()
    search_start = report_lines.index('Searching for "k"')
    expected = ["  " + line for line in report_lines[search_start:]]
    trace_search_start = trace_lines.index('  Searching for "k"')
    assert trace_lines[trace_search_start:] == expected


def test_miss_traces_before_raising(make_tree, caplog):
    caplog.set_level(logging.DEBUG, logger="hyera")
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    with pytest.raises(KeyError):
        h.lookup("db.nope")
    records = _explain_records(caplog)
    assert any(r.getMessage().startswith("Lookup of 'db.nope'") for r in records)
    assert not any("ensure it is provided" in r.getMessage() for r in records)
    assert not any("did not find" in r.getMessage() for r in records)


def test_name_list_preamble(make_tree, caplog):
    caplog.set_level(logging.DEBUG, logger="hyera")
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    h.lookup(["nope", "k"])
    records = _explain_records(caplog)
    assert any(r.getMessage().startswith("Lookup of 'nope', 'k'") for r in records)


def test_nested_lookup_traces_first(make_tree, caplog):
    caplog.set_level(logging.DEBUG, logger="hyera")
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "t: v\ns: \"%{lookup('t')}\"\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    h.lookup("s")
    records = _explain_records(caplog)
    assert len(records) == 2
    first, second = (r.getMessage() for r in records)
    assert first.startswith("Lookup of 't'")
    assert first.splitlines()[1] == "  Interpolation on \"%{lookup('t')}\""
    assert second.startswith("Lookup of 's'")


def test_no_trace_above_debug(make_tree, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="hyera")
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )

    def boom(self, *a, **k):
        raise AssertionError("_DebugExplainer must never be built below DEBUG")

    import hyera._output.explain

    monkeypatch.setattr(hyera._output.explain._DebugExplainer, "__init__", boom)
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("k") == "v"
    assert not _explain_records(caplog)


def test_cli_logger_is_module_logger():
    pytest.importorskip("duho")
    import hyera.cli

    assert hyera.cli._LOGGER.name == "hyera.cli"
