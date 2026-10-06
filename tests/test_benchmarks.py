"""Smoke test for ``benchmarks/run.py`` -- runs the real script, --quick, and
checks the saved JSON's shape. No timing assertions: local numbers are
sanity checks, never a pass/fail signal.
"""

import json
import runpy
import sys
from pathlib import Path

import pytest

_BENCHMARKS_RUN = Path(__file__).resolve().parent.parent / "benchmarks" / "run.py"

_EXPECTED_METRICS = {
    "construct",
    "lookup.cold",
    "lookup.first",
    "lookup.keys100",
    "lookup.miss",
    "lookup.levels40",
    "explain",
    "lookup.deep.glob500",
    "lookup.scope.volatile",
    "lookup.scope.new_node",
    "cli.run",
}


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def test_runner_quick_saves_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run.py",
            "--quick",
            "--save",
            "--name",
            "smoke",
            "--results-dir",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(_BENCHMARKS_RUN), run_name="__main__")
    assert exc_info.value.code == 0

    result_path = tmp_path / "smoke.json"
    assert result_path.is_file()
    data = json.loads(result_path.read_text(encoding="utf-8"))

    for key in (
        "name",
        "hyera_version",
        "python",
        "implementation",
        "platform",
        "processor",
        "timestamp",
        "iterations",
        "tree",
        "metrics",
    ):
        assert key in data, key

    assert set(data["metrics"]) == _EXPECTED_METRICS
    for name, m in data["metrics"].items():
        assert 0 < m["min_ms"] <= m["median_ms"] <= m["max_ms"], name

    assert "node" not in data
    for s in _walk_strings(data):
        assert "hyera-bench-" not in s


def test_default_result_name_tells_the_revalidate_modes_apart(tmp_path, monkeypatch):
    import hyera

    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--quick", "--save", "--revalidate", "off"]
        + ["--results-dir", str(tmp_path)],
    )
    with pytest.raises(SystemExit):
        runpy.run_path(str(_BENCHMARKS_RUN), run_name="__main__")
    expected = "hyera-{}-py{}{}-norevalidate.json".format(
        hyera.__version__, sys.version_info.major, sys.version_info.minor
    )
    assert [p.name for p in tmp_path.iterdir()] == [expected]
    assert (
        json.loads((tmp_path / expected).read_text(encoding="utf-8"))["revalidate"]
        is False
    )
