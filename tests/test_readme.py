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
HEADER = ROOT / "src" / "hyera" / "AGENTS.md"
FENCE = re.compile(r"^```(\w+)[^\n]*\n(.*?)^```$", re.M | re.S)

#: Differences-from-Puppet slugs with no conformance golden behind them,
#: mapped to one sentence saying why the harness cannot record one. Checked
#: two ways by ``test_differences_match_deviations``: every golden
#: ``deviation:`` id plus every key here must equal the README's tagged
#: bullet set, and none of these slugs may collide with a golden id.
DOC_ONLY = {
    "missing-config-raises": (
        "tests/conformance/_ours.py's run_api always passes an existing hiera.yaml"
    ),
    "config-dir-not-interpolated": (
        "the harness always records cases from a fixed, non-interpolatable "
        "hiera.yaml directory"
    ),
    "config-not-revalidated": (
        "the harness never mutates a data file between two calls in the same case"
    ),
    "environmentpath-none-means-no-layer": (
        "absent environmentpath is the harness's own default for every "
        "non-layers case, so there is nothing Puppet-side to diverge from"
    ),
    "codedir-aio-default": (
        "the harness never varies codedir, so no query exercises the default"
    ),
    "hocon-include-glob": (
        "no case directory exercises a glob-shaped file(...) argument "
        "against the real oracle yet"
    ),
    "hocon-pyhocon-parser": (
        "the differences are pyhocon grammar behaviour, not one recorded "
        "query; the conformance harness has no ordered or error-text "
        "comparison for them"
    ),
    "eyaml-pkcs7-only": (
        "the harness cannot exercise a GPG-encrypted eyaml value, since "
        "recording one would need a GPG keypair and a real Puppet install "
        "with the gpg plugin"
    ),
    "strict-default-warning": (
        "the conformance recorder runs every case with --strict warning"
    ),
    "glob-case-sensitive-byte-order": (
        "the recording host and CI both run case-sensitive filesystems, so "
        "case-insensitivity has no recordable fixture"
    ),
    "render-yaml-sensitive-redacted": (
        "the harness's oracle capture never records a raw Sensitive "
        "plaintext, so there is nothing to compare a redaction against"
    ),
    "aio-hash-rendering": (
        "the harness normalizes the oracle's Ruby 4 hash-inspect form into "
        "the AIO form before comparing"
    ),
    "scope-flag-sets-node-parameters": (
        "the harness always supplies --scope as node parameters uniformly, "
        "so there is no node-classifier fixture to diverge from"
    ),
    "facts-from-file-only": (
        "the harness always runs Puppet with --facts too, so no case "
        "records Puppet falling back to local facter facts"
    ),
    "server-facts-minimal": (
        "the harness's oracle runs one fixed Puppet server version, so no "
        "query could show a richer $server_facts set as a mismatch"
    ),
    "environment-conf-compile-trusted-unsupported": (
        "the harness never invokes puppet lookup --compile/--trusted, so "
        "no case could record what hyera does not implement"
    ),
    "python-equal-hash-keys": (
        "a golden holds the value Puppet returns, and hyera raises instead "
        "of returning one, so no case can assert the two together"
    ),
    "non-utf8-data": (
        "the harness stores every case file as UTF-8 text, so no fixture "
        "can hold a byte that is not valid UTF-8"
    ),
    "nesting-bound": (
        "the oracle answers a document nested past the bound with a value or "
        "its own error text, which no case asserts a different outcome from"
    ),
    "ruby-regex-constructs": (
        "the harness cannot record a Ruby construct hyera refuses, and its "
        "Unicode POSIX-class behaviour differs only for non-ASCII subjects"
    ),
}

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


def _differences_section(text):
    return text.split("## Differences from Puppet", 1)[1].split("\n## ", 1)[0]


def test_differences_match_deviations():
    """The README's tagged Differences list, and the shipped header's
    matching section, together cover exactly the conformance goldens'
    ``deviation:`` ids plus the doc-only slugs above -- no more, no less,
    and in the same order in both files."""
    golden = set()
    for case_file in sorted(
        (ROOT / "tests" / "conformance" / "cases").glob("*/case.yaml")
    ):
        data = yaml.safe_load(case_file.read_text(encoding="utf-8"))
        if not data:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "deviation" in node:
                    value = node["deviation"]
                    golden.add(value["id"] if isinstance(value, dict) else value)
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    assert not golden & set(DOC_ONLY), "a slug is both a golden id and doc-only"

    readme_section = _differences_section(README.read_text(encoding="utf-8"))
    readme_ids = re.findall(r"\(id: `([a-z0-9-]+)`\)", readme_section)
    assert len(readme_ids) == len(set(readme_ids)), "duplicate id in README"
    assert set(readme_ids) == golden | set(DOC_ONLY)

    header_section = _differences_section(HEADER.read_text(encoding="utf-8"))
    header_ids = re.findall(
        r"^- \*\*(?:deviation|difference)\*\* `([a-z0-9-]+)`", header_section, re.M
    )
    header_deviation_ids = re.findall(
        r"^- \*\*deviation\*\* `([a-z0-9-]+)`", header_section, re.M
    )

    assert header_ids == readme_ids
    assert set(header_deviation_ids) == golden
