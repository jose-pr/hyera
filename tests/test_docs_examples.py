"""Runs the landing page's own examples as tests.

``docs/index.md`` is hand-written and its examples are not otherwise
exercised by the suite: this module runs its ``pycon`` blocks as doctests,
its ``console`` block through the real CLI entry point, and checks its
extras table against ``pyproject.toml``. An API change that would make the
page wrong turns this test red instead of the page silently going stale.
"""

import doctest
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "index.md"
PYPROJECT = ROOT / "pyproject.toml"
FENCE = re.compile(r"^```(\w+)[^\n]*\n(.*?)^```$", re.M | re.S)


def _blocks(lang):
    """Return ``[(line, body)]`` for every fenced code block of ``lang``."""
    text = DOC.read_text(encoding="utf-8")
    found = []
    for match in FENCE.finditer(text):
        if match.group(1) != lang:
            continue
        line = text.count("\n", 0, match.start()) + 1
        found.append((line, match.group(2)))
    return found


@pytest.fixture()
def tour(tmp_path, monkeypatch):
    """Materialize every ``yaml`` block whose first line names its path."""
    for _line, body in _blocks("yaml"):
        first, _, rest = body.partition("\n")
        m = re.match(r"^#\s*(\S+)\s*$", first)
        if not m:
            continue
        dest = tmp_path / m.group(1)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rest, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_python_examples(tour):
    blocks = _blocks("pycon")
    assert blocks
    parser = doctest.DocTestParser()
    runner = doctest.DocTestRunner(verbose=False)
    for line, body in blocks:
        test = parser.get_doctest(
            body, {}, "index.md:<{}>".format(line), str(DOC), line
        )
        runner.run(test)
    results = runner.summarize(verbose=False)
    assert results.failed == 0


def test_console_examples(tour, capsys):
    pytest.importorskip("duho")
    import hyera.cli

    blocks = _blocks("console")
    assert blocks
    for _line, body in blocks:
        for chunk in body.split("$ ")[1:]:
            cmd_line, _, expected = chunk.partition("\n")
            args = shlex.split(cmd_line)
            assert args[:3] == ["python", "-m", "hyera"]
            capsys.readouterr()
            rc = hyera.cli.main(args[3:])
            assert rc == 0
            out = capsys.readouterr().out
            assert out.rstrip("\n") == expected.rstrip("\n")


def test_extras_table_matches_pyproject():
    doc_text = DOC.read_text(encoding="utf-8")
    table_extras = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", doc_text, re.M))

    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    section = re.search(
        r"^\[project\.optional-dependencies\]\n(.*?)(?=^\[|\Z)",
        pyproject_text,
        re.M | re.S,
    ).group(1)
    pyproject_extras = set(re.findall(r"^([A-Za-z0-9_-]+)\s*=", section, re.M))
    pyproject_extras -= {"dev", "docs"}

    assert table_extras == pyproject_extras


def test_every_api_page_documents_one_module_and_is_linked():
    pages = {
        page.name: page.read_text(encoding="utf-8").strip()
        for page in (ROOT / "docs" / "api").glob("*.md")
    }
    modules = sorted(body.split(" ", 1)[1] for body in pages.values())
    assert all(body.startswith("::: hyera") for body in pages.values())
    assert len(set(modules)) == len(modules)
    assert "hyera.exceptions" in modules
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    index = DOC.read_text(encoding="utf-8")
    for name in pages:
        assert "api/" + name in nav, name
        assert "api/" + name in index, name
