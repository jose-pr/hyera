"""Guard against internal working-artefact labels in tracked files.

Scans every ``git``-tracked text file for the label shapes and fails naming each
``path:line``; a short allowlist covers genuine domain uses. Skipped outside git.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_THIS_FILE = Path(__file__).resolve()

# Label shapes: each entry is (name, compiled pattern), kept as data so the
# planted-label test can drive every shape. "work-item-code-*" and "review-code-bare"
# cover capital-letter-plus-digits codes, parenthesized, bold or bare in prose.

_CODE_LETTERS = "F|G|M|H|L|S|C|R|B|P|T|X"

_PATTERNS = [
    ("numbered-plan", re.compile(r"\b[Pp]lan[ _-]?\d")),
    ("numbered-phase", re.compile(r"\b[Pp]hase[ _-]?\d")),
    ("numbered-item", re.compile(r"\b[Ii]tem[ _-]?\d")),
    ("task-range", re.compile(r"\bT\d+-T\d+")),
    (
        "work-item-code-paren",
        re.compile(r"\((?:" + _CODE_LETTERS + r")-?\d{1,3}[a-z]?\)"),
    ),
    (
        "work-item-code-bold",
        re.compile(r"\*\*(?:" + _CODE_LETTERS + r")\d{1,3}[a-z]?\*\*"),
    ),
    ("work-item-code-bare", re.compile(r"\b[FP]\d\b")),
    # Bare work-item codes in running prose ("see R7a", "R4: ..."), scoped to R/S/X so
    # unrelated tokens like flake8's "F401" or a Unicode block name "C0" do not match.
    ("review-code-bare", re.compile(r"\b(?:R|S|X)\d{1,2}[a-z]?\b")),
    # A kebab-case id of four or more words in backticks is a working-note
    # slug unless README.md publishes it as a difference id (see _line_hits).
    ("finding-slug", re.compile(r"`+([a-z][a-z0-9]*(?:-[a-z0-9]+){3,})`+")),
    ("session-reference", re.compile(r"\bsessions?\b", re.IGNORECASE)),
    ("hardening-pass", re.compile(r"hardening pass", re.IGNORECASE)),
    (
        "review-said",
        re.compile(
            r"\b(?:review|audit)s? (?:found|flagged|said|caught|fixtures?)\b"
            r"|\b(?:security|code) review\b",
            re.IGNORECASE,
        ),
    ),
    # Matched on its own so a "the" that a line wrap separated from
    # "plan's" is still found.
    ("plan-possessive", re.compile(r"\bplan's\b", re.IGNORECASE)),
    ("reviewer-reference", re.compile(r"\breviewer\b", re.IGNORECASE)),
    ("in-depth-review", re.compile(r"in-depth review", re.IGNORECASE)),
    ("review-finding", re.compile(r"review finding", re.IGNORECASE)),
    ("code-review-findings", re.compile(r"code-review findings?", re.IGNORECASE)),
    ("the-finding", re.compile(r"\bthe finding\b", re.IGNORECASE)),
    ("the-plan", re.compile(r"\bthe plan(?:'s)?\b", re.IGNORECASE)),
    ("decision-id", re.compile(r"\bD\d{2}\b")),
    ("backlog-item", re.compile(r"\bbacklog\b", re.IGNORECASE)),
    # Built via concatenation (not a literal dotted path) so this detector's
    # own source is never itself a textual match for what it detects.
    ("private-working-dir", re.compile(r"\." + r"agents\b")),
]

# (path, substring, reason): a match on `path` whose line contains `substring` is a
# genuine domain use, not an internal reference. `_DOTTED_DIR` is assembled like the
# pattern above, for the same reason.
_DOTTED_DIR = "." + "agents"
_ALLOWLIST = [
    (
        ".gitignore",
        _DOTTED_DIR,
        "this repo's own ignore rule for its private working-notes "
        "directory, not a leaked reference to its contents",
    ),
    (
        "src/hyera/AGENTS.md",
        "an MCP session",
        "a Model Context Protocol session, the transport's own term",
    ),
    (
        "benchmarks/README.md",
        "scope=S0",
        "S0 is a literal example hyera.Scope variable name in the "
        "benchmark snippet, not a review-finding code",
    ),
]


def _is_allowed(path: str, line: str) -> bool:
    return any(
        path == allowed_path and substring in line
        for allowed_path, substring, _reason in _ALLOWLIST
    )


#: A difference id README.md publishes, written ``(id: `<slug>`)``; a
#: conformance case's directory name is public too.
_PUBLIC_ID_RE = re.compile(r"\(id: `([a-z0-9-]+)`\)")


def _public_ids() -> "frozenset[str]":
    try:
        text = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    cases = (_REPO_ROOT / "tests" / "conformance" / "cases").glob("*")
    return frozenset(_PUBLIC_ID_RE.findall(text)) | {c.name for c in cases}


_PUBLIC_IDS = _public_ids()


def _line_hits(name: str, pattern: "re.Pattern[str]", line: str) -> bool:
    if name == "finding-slug":
        return any(m.group(1) not in _PUBLIC_IDS for m in pattern.finditer(line))
    return bool(pattern.search(line))


def _scan_text(text: str):
    """Yield (pattern_name, line_no, line) for every label match in `text`."""
    for line_no, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _PATTERNS:
            if _line_hits(name, pattern, line):
                yield name, line_no, line


def _git_tracked_files():
    # -z (NUL-separated): git otherwise C-quotes a path with a non-ASCII byte in double
    # quotes with octal escapes, as tests/conformance/cases/backend-utf8-path has.
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=_REPO_ROOT,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


def test_no_internal_labels_in_tracked_files():
    tracked = _git_tracked_files()
    if tracked is None:
        pytest.skip("not inside a git checkout -- nothing to scan")

    offenses = []
    for rel_path in tracked:
        path = _REPO_ROOT / rel_path
        if path.resolve() == _THIS_FILE:
            continue  # this file's own docstrings describe the label shapes
        if not path.is_file():
            continue  # a tracked submodule/symlink target that isn't here
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable -- not a text file we can scan

        posix_path = Path(rel_path).as_posix()
        for name, line_no, line in _scan_text(text):
            if _is_allowed(posix_path, line):
                continue
            offenses.append(f"{posix_path}:{line_no}: [{name}] {line.strip()}")

    assert not offenses, "internal-artefact labels found:\n" + "\n".join(offenses)


def test_label_patterns_catch_a_planted_offender():
    """Each pattern above actually matches a realistic offending line.

    Every sample is built from separate pieces and joined at the assertion,
    so the literal offending text never appears in this file's own source.
    """
    samples = {
        "numbered-plan": " ".join(["See", "Pl" + "an", "7", "for context."]),
        "numbered-phase": " ".join(["Start", "Ph" + "ase", "2", "now."]),
        "numbered-item": " ".join(["Fixes", "it" + "em", "3", "from the list."]),
        "task-range": "Measured after " + "T1" + "-" + "T7" + " landed.",
        "work-item-code-paren": "Async support " + "(" + "F4" + ")" + " added.",
        "work-item-code-bold": "- **" + "F4" + "** Async support added.",
        "work-item-code-bare": "See " + "F4" + "/" + "P2" + " for the change.",
        "review-code-bare": "Fixed per " + "R4" + "b" + " during hardening.",
        "reviewer-reference": "Mirrors the " + "review" + "er's fixture.",
        "in-depth-review": "Findings from the " + "in-depth" + " review.",
        "review-finding": "A " + "review find" + "ing about MCP.",
        "code-review-findings": "The 2026-07-18 " + "code-review find" + "ings.",
        "the-finding": "Regression test for " + "the find" + "ing that it broke.",
        "the-plan": "See " + "the pl" + "an's Known Facts.",
        "decision-id": "Per " + "D" + "01" + ", the SDK stays optional.",
        "backlog-item": "Three fixes from the " + "backl" + "og.",
        "finding-slug": "Filed as `" + "hocon-file-include" + "-globs-extra" + "`.",
        "session-reference": "Verified in the reviewing " + "ses" + "sion.",
        "hardening-pass": "Found by a later " + "hardening" + " pass.",
        "review-said": "A security " + "review fou" + "nd the leak.",
        "plan-possessive": "Listed in the parent " + "pl" + "an's facts.",
        "private-working-dir": "Notes live in " + "." + "agents" + "/plans/.",
    }
    assert set(samples) == {
        name for name, _ in _PATTERNS
    }, "every pattern above must have a planted-offender sample"
    for name, pattern in _PATTERNS:
        matches = list(_scan_text(samples[name]))
        matched_names = {matched_name for matched_name, _, _ in matches}
        assert (
            name in matched_names
        ), f"pattern {name!r} failed to catch its own planted offender"


def test_a_published_difference_id_is_not_a_finding_slug():
    assert _PUBLIC_IDS, "README.md publishes difference ids"
    public = sorted(_PUBLIC_IDS, key=len)[-1]
    assert not [hit for hit in _scan_text("See `" + public + "`.")]
    assert [hit for hit in _scan_text("See `" + public + "-extra`.")]
