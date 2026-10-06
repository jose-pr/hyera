"""Runs ``README.md``'s examples as tests and checks its install hints and extras
quoting.
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

# Differences-from-Puppet slugs with no conformance golden, each with one sentence
# on why the harness cannot record one. Every golden `deviation:` id plus these keys
# must equal the README's tagged bullets, and none may collide with a golden id.
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
    "render-yaml-equivalent-not-identical": (
        "the harness compares parsed values, never the YAML text; a round-trip "
        "test in tests/test_render.py covers the text"
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
    "string-format-subset": (
        "the harness records what Puppet formats; the refused forms are "
        "inputs Puppet accepts and hyera rejects, which no golden can pin"
    ),
    "ruby-regex-constructs": (
        "the harness cannot record a Ruby construct hyera refuses, and its "
        "Unicode POSIX-class behaviour differs only for non-ASCII subjects"
    ),
    "interpolation-chain-depth": (
        "the harness records no chain longer than the shortest depth either "
        "side reaches, so no query separates the two limits"
    ),
    "interpolation-key-shapes": (
        "the harness's golden schema cannot hold an Array as a hash key, and "
        "a key spelled with four colons is not in any recorded case"
    ),
    "error-exit-status": (
        "the harness asserts a lookup's outcome, never the exit status or "
        "the stderr line of the command"
    ),
    "error-message-text": (
        "the harness holds an error to its status, not its text, so no golden "
        "asserts the wording"
    ),
    "schema-error-line-suffix": (
        "the harness holds an error to its status, not its text, so a "
        "line suffix or the count of mismatches reported is never compared"
    ),
    "puppet-crashes-hyera-answers": (
        "a golden records Puppet's answer, and Puppet crashes on these "
        "inputs, so there is no value for hyera's answer to be compared with"
    ),
    "environment-trailing-slash": (
        "the harness names environments by their plain directory names"
    ),
    "dir-glob-ruby-quirks": (
        "the recording host's Ruby and the harness's case trees do not "
        "combine a brace group with ** or an empty brace alternative"
    ),
    "glob-case-folded-spelling": (
        "the recording host and CI both run case-sensitive filesystems, so "
        "no fixture can match a segment by case folding"
    ),
}

# The extras-bracket spelling not immediately preceded by a double quote: a real
# install command must read ``pip install "hyera[extra]"``, or a shell fails on it.
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


def test_readme_sh_examples_run(monkeypatch, capsys):
    """Every ``hyera ...`` / ``python -m hyera ...`` command line in a ``sh``
    block runs from the repository root and exits 0, so each one names the
    example config, its facts and a key the example data holds."""
    pytest.importorskip("duho")
    import hyera.cli

    monkeypatch.chdir(ROOT)
    text = README.read_text(encoding="utf-8")
    ran = 0
    for _line, body in _blocks(text, "sh"):
        for cmd_line in body.splitlines():
            if not cmd_line.startswith(("hyera --", "python -m hyera ")):
                continue
            args = shlex.split(cmd_line)
            if args[0] == "python":
                args = args[2:]
            capsys.readouterr()
            rc = hyera.cli.main(args[1:])
            assert rc == 0, "{} exited {}: {}".format(
                cmd_line, rc, capsys.readouterr().err
            )
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


def test_badges_and_sections_follow_the_standard_order():
    text = README.read_text(encoding="utf-8")
    badges = re.findall(r"^\[!\[([^\]]+)\]", text, re.M)
    assert badges == ["Version", "Python versions", "License", "Docs", "CI"]

    headings = re.findall(r"^## (.+)$", text, re.M)
    standard = [
        "Features",
        "Installation",
        "Quick start",
        "Command line",
        "API overview",
    ]
    assert headings[: len(standard)] == standard
    assert headings[-2:] == ["Development", "License"]


def test_installation_table_names_each_extra_without_a_version_range():
    text = README.read_text(encoding="utf-8")
    install_section = text.split("## Installation", 1)[1].split("\n## ", 1)[0]
    rows = {
        match.group(1): match.group(0)
        for match in re.finditer(r"^\| `(\w+)` \|.*$", install_section, re.M)
    }
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = {
        name: re.findall(r'"([A-Za-z0-9_.-]+)', body)
        for name, body in re.findall(
            r"^(cli|hocon|eyaml)\s*=\s*\[(.*?)\]", pyproject, re.M
        )
    }
    assert set(rows) == set(declared) == {"cli", "hocon", "eyaml"}
    for extra, row in rows.items():
        for requirement in declared[extra]:
            assert "`{}`".format(requirement) in row
        assert re.search(r"[<>=!~]=?\s*\d", row) is None, row


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
