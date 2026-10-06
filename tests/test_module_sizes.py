"""Every module under ``src/hyera`` stays at 600 lines or fewer; the listed
exceptions give the reason each stays whole.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "hyera"
LIMIT = 600

#: Path (relative to the repository root, ``/``-separated) -> why it stays whole.
ALLOWED = {
    "src/hyera/core.py": (
        "one public class, Hiera, whose docstrings (about 480 lines) are the "
        "API reference the docs site is built from"
    ),
    "src/hyera/backends/__init__.py": (
        "defines the public Backend class (about 465 lines, largely API "
        "documentation) and the registry it keys"
    ),
}


def _sizes():
    return {
        path.relative_to(ROOT).as_posix(): len(
            path.read_text(encoding="utf-8").splitlines()
        )
        for path in sorted(SRC.rglob("*.py"))
    }


def test_no_module_is_over_the_limit_unless_listed():
    over = {
        path: lines
        for path, lines in _sizes().items()
        if lines > LIMIT and path not in ALLOWED
    }
    assert not over, "over %d lines and not listed: %s" % (LIMIT, over)


def test_every_listed_module_is_still_over_the_limit():
    sizes = _sizes()
    stale = [path for path in ALLOWED if path not in sizes or sizes[path] <= LIMIT]
    assert not stale, "listed but not over %d lines (remove them): %s" % (
        LIMIT,
        stale,
    )


def test_every_listed_module_has_a_reason():
    empty = [path for path, reason in ALLOWED.items() if not reason.strip()]
    assert not empty, "listed without a reason: %s" % empty
