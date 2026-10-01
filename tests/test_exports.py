"""A direct test for every public export: a name that never appears as a bare ``ast.Name``/``ast.Attribute``
inside some ``test_*`` function's body has regressed unnoticed the moment
nothing else in the suite references it.

This is a presence check, not a depth check: a name collision (a local
variable that happens to share a class member's name) makes a param pass
without real coverage of that member. Branch coverage (``coverage run
--branch``) is what proves depth; this guard only proves the property holds
for the *next* export too, at near-zero runtime.
"""

import ast
import re
from pathlib import Path

import pytest

import hyera
from hyera import types as hyera_types

_TESTS_DIR = Path(__file__).resolve().parent
_THIS_FILE = Path(__file__).resolve()


def _collect_test_referenced_names():
    """Every bare ``Name`` id and ``Attribute`` attr string that appears
    anywhere inside a ``def test_*``/``async def test_*`` function's body,
    across every ``tests/**/*.py`` file except ``tests/conformance/`` (its
    own oracle-replay harness, not hand-written direct tests) and this
    file (whose own body would otherwise trivially satisfy every param by
    naming every export once)."""
    names = set()
    for path in sorted(_TESTS_DIR.rglob("*.py")):
        if path == _THIS_FILE:
            continue
        if "conformance" in path.relative_to(_TESTS_DIR).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("test_"):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        names.add(sub.attr)
    return names


_REFERENCED = _collect_test_referenced_names()


def _is_public_member(name, value):
    if name.startswith("_"):
        return False
    if name.isupper():
        return False
    if isinstance(value, (staticmethod, classmethod, property)):
        return True
    return callable(value)


def _member_ids():
    out = []
    for export_name in sorted(hyera.__all__):
        obj = getattr(hyera, export_name)
        if not isinstance(obj, type):
            continue
        for member_name, value in sorted(vars(obj).items()):
            if _is_public_member(member_name, value):
                out.append("{}.{}".format(export_name, member_name))
    return out


@pytest.mark.parametrize(
    "export_name", sorted(hyera.__all__), ids=lambda n: "export:{}".format(n)
)
def test_export_has_direct_test(export_name):
    assert export_name in _REFERENCED, (
        "hyera.{} is never referenced (as a bare name or an attribute) "
        "inside a test_* function".format(export_name)
    )


@pytest.mark.parametrize(
    "qualified", _member_ids(), ids=lambda n: "member:{}".format(n)
)
def test_member_has_direct_test(qualified):
    _, member_name = qualified.split(".", 1)
    assert member_name in _REFERENCED, (
        "hyera.{} is never referenced (as a bare name or an attribute) "
        "inside a test_* function".format(qualified)
    )


def _type_member_ids():
    """The same per-member presence check as :func:`_member_ids`, over
    :data:`hyera.types`'s own ``__all__`` instead of top-level ``hyera``'s
    (a separate public module, not re-exported there -- see
    ``src/hyera/AGENTS.md``'s "hyera.types" section)."""
    out = []
    for export_name in sorted(hyera_types.__all__):
        obj = getattr(hyera_types, export_name)
        if not isinstance(obj, type):
            continue
        for member_name, value in sorted(vars(obj).items()):
            if _is_public_member(member_name, value):
                out.append("types.{}.{}".format(export_name, member_name))
    return out


@pytest.mark.parametrize(
    "export_name",
    sorted(hyera_types.__all__),
    ids=lambda n: "types-export:{}".format(n),
)
def test_type_export_has_direct_test(export_name):
    assert export_name in _REFERENCED, (
        "hyera.types.{} is never referenced (as a bare name or an "
        "attribute) inside a test_* function".format(export_name)
    )


@pytest.mark.parametrize(
    "qualified", _type_member_ids(), ids=lambda n: "types-member:{}".format(n)
)
def test_type_member_has_direct_test(qualified):
    _, _, member_name = qualified.split(".", 2)
    assert member_name in _REFERENCED, (
        "hyera.{} is never referenced (as a bare name or an attribute) "
        "inside a test_* function".format(qualified)
    )


def test_version_string():
    assert re.match(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?$", hyera.__version__)
