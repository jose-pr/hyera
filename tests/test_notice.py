"""NOTICE, the per-file "Ported from" headers and the citations agree.

A module that cites ``file.rb:line`` for the code it mirrors is derived from
that file, so it carries the upstream's header and has a line in ``NOTICE``.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "hyera"
CITATION = re.compile(r"\b[a-z_]+\.rb:\d+")
HEADER = re.compile(r"\A# Ported from ")
NOTICE_PATH = re.compile(r"^\s+(src/hyera/\S+\.py)\b", re.M)
LICENSE_PATH = re.compile(r"\bLICENSES/\S+\.txt")


def _modules():
    return sorted(SRC.rglob("*.py"))


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def _notice():
    return (ROOT / "NOTICE").read_text(encoding="utf-8")


def test_every_module_citing_a_ruby_line_carries_a_header():
    missing = [
        _rel(p)
        for p in _modules()
        if CITATION.search(p.read_text(encoding="utf-8"))
        and not HEADER.match(p.read_text(encoding="utf-8"))
    ]
    assert not missing, "cites file.rb:N but has no 'Ported from' header: %s" % missing


def test_every_header_has_a_notice_line():
    listed = set(NOTICE_PATH.findall(_notice()))
    unlisted = [
        _rel(p)
        for p in _modules()
        if HEADER.match(p.read_text(encoding="utf-8")) and _rel(p) not in listed
    ]
    assert not unlisted, "header without a NOTICE line: %s" % unlisted


def test_every_notice_path_exists_and_has_a_header():
    for rel in sorted(set(NOTICE_PATH.findall(_notice()))):
        path = ROOT / rel
        assert path.is_file(), "NOTICE names a missing file: %s" % rel
        assert HEADER.match(path.read_text(encoding="utf-8")), (
            "NOTICE lists %s but it has no header" % rel
        )


def test_every_licence_named_in_notice_exists():
    names = LICENSE_PATH.findall(_notice())
    assert names
    for name in names:
        assert (ROOT / name).is_file(), name


def test_hiera_eyaml_is_credited_with_its_licence():
    text = _notice()
    assert "hiera-eyaml" in text
    licence = (ROOT / "LICENSES" / "hiera-eyaml-MIT.txt").read_text(encoding="utf-8")
    assert licence.strip().startswith("The MIT License (MIT)")
    assert "Copyright (c) 2013 Tom Poulton" in licence
