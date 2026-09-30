"""Runs ``README.md``'s own examples as tests, and checks its install hints.

``README.md`` is the PyPI long description and the only user guide; nothing
else in the suite exercises its code blocks or its extras-quoting rule. A
change that makes the page wrong -- a broken example, a stale extras table,
an install hint that would fail in ``cmd.exe`` -- turns this module red
instead of going unnoticed.
"""

import doctest
import re
import shlex
from importlib.metadata import metadata
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
FENCE = re.compile(r"^```(\w+)[^\n]*\n(.*?)^```$", re.M | re.S)

#: Any occurrence of the extras-bracket spelling not immediately preceded by
#: a double quote. A real install command must read ``pip install
#: "hyera[extra]"``; anything else (unquoted, single-quoted, or bare prose)
#: fails a shell somewhere -- see README.md's own "Installation" section.
_UNQUOTED_EXTRA = re.compile('(^|[^"])hyera' + r"\[", re.M)

#: Files/globs checked for the unquoted spelling above, relative to ROOT.
_SCANNED_GLOBS = (
    "README.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "src/hyera/AGENTS.md",
    "src/hyera/*.py",
    "tests/*.py",
    "docs/**/*.md",
)


def _blocks(text, lang):
    """Return ``[(line, body)]`` for every fenced code block of ``lang``."""
    found = []
    for match in FENCE.finditer(text):
        if match.group(1) != lang:
            continue
        line = text.count("\n", 0, match.start()) + 1
        found.append((line, match.group(2)))
    return found


def _changelog_scan_text():
    """The part of ``CHANGELOG.md`` this module's quoting check applies to.

    Only ``## [Unreleased]`` is live prose; every dated release section
    above it is a frozen historical record (never rewritten once cut, per
    this project's release discipline) and is excluded from the scan --
    including any install hint it happens to still spell without quotes at
    the time it was released.
    """
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[)", text, re.M | re.S)
    assert match, "CHANGELOG.md has no ## [Unreleased] section"
    return match.group(1)


def test_readme_pycon_examples(monkeypatch):
    monkeypatch.chdir(ROOT)
    text = README.read_text(encoding="utf-8")
    results = doctest.testfile(
        str(README),
        module_relative=False,
        encoding="utf-8",
        optionflags=doctest.ELLIPSIS | doctest.NORMALIZE_WHITESPACE,
    )
    assert results.attempted > 0
    assert results.failed == 0
    for _line, body in _blocks(text, "python"):
        compile(body, "README.md", "exec")


def test_readme_console_examples(monkeypatch, capsys):
    pytest.importorskip("duho")
    import hyera.cli

    monkeypatch.chdir(ROOT)
    text = README.read_text(encoding="utf-8")
    blocks = _blocks(text, "console")
    assert blocks
    ran = 0
    for _line, body in blocks:
        for chunk in body.split("$ ")[1:]:
            cmd_line, _, rest = chunk.partition("\n")
            expected, _, _next = rest.partition("\n$ ")
            args = shlex.split(cmd_line)
            assert args[0] == "hyera"
            capsys.readouterr()
            rc = hyera.cli.main(args[1:])
            assert rc == 0
            out = capsys.readouterr().out
            assert out.rstrip("\n") == expected.rstrip("\n")
            ran += 1
    assert ran > 0


def test_readme_config_is_the_example():
    text = README.read_text(encoding="utf-8")
    quick_start = text.split("## Quick start", 1)[1]
    yaml_blocks = _blocks(quick_start, "yaml")
    assert yaml_blocks, "no yaml block found after ## Quick start"
    first_line, first_body = yaml_blocks[0]
    example = (ROOT / "examples" / "hiera.yaml").read_text(encoding="utf-8")
    assert first_body.strip() == example.strip()

    for _line, body in _blocks(text, "yaml"):
        yaml.safe_load(body)


def test_install_commands_quote_extras():
    text = README.read_text(encoding="utf-8")
    install_section = text.split("## Installation", 1)[1].split("\n## ", 1)[0]
    table_extras = set(re.findall(r'pip install "hyera\[(\w+)\]"', install_section))
    declared = set(metadata("hyera").get_all("Provides-Extra")) - {"dev", "docs"}
    assert table_extras == declared

    for pattern in _SCANNED_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.name == "test_readme.py":
                # This module's own patterns are the check itself.
                continue
            scan_text = (
                _changelog_scan_text()
                if path == ROOT / "CHANGELOG.md"
                else path.read_text(encoding="utf-8")
            )
            hit = _UNQUOTED_EXTRA.search(scan_text)
            assert hit is None, "unquoted extra in {}: {!r}".format(
                path.relative_to(ROOT), scan_text[hit.start() : hit.start() + 40]
            )
