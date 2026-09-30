"""Tuple key paths in ``lookup()``: a tuple name is an exact key
path -- element 0 the root key, the rest dig segments, each taken
verbatim (no dot splitting, no quote syntax, no whitespace stripping). A
``list`` keeps Puppet's own "names tried in order" meaning and may now
hold tuple paths too. ``h[...]`` is unchanged (a tuple subscript still
unpacks into ``lookup(*item)``), so a path there is ``h[(path,)]``.

Equivalence with the matching quoted dotted string is checked directly
against the conformance goldens (``test_tuple_path_matches_conformance_
goldens``, below) -- real Puppet is the oracle there. The rest of this
module covers validation and the Python-only call shapes (a name list, a
form-4 name hash, ``[]``, ``in``, ``explain``, ``block``) a golden can't
reach, plus the display/error text a tuple path produces.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

import hyera
from hyera import Hiera, KeyNotFoundError
from hyera.exceptions import HieraLookupError
from hyera._lookup.navigation import join_key, split_key

_HIERARCHY = {"hierarchy": [{"name": "common", "path": "common.yaml"}]}

_DATA = """\
---
a:
  b: nested
  "0": zero-str
  "x.y": dotted-segment
  "it's": apostrophe-value
  'she said "hi"': doublequote-value
"a.b": literal-dot-root
"p.q":
  r: found-under-pq
lst:
  - z0
  - z1
  - z2
h:
  0: int-zero
  "0": str-zero
"""


@pytest.fixture
def tree(make_tree):
    return make_tree(_HIERARCHY, {"data/common.yaml": _DATA})


@pytest.fixture
def h(tree):
    return Hiera(str(tree / "hiera.yaml"))


# --- validation: a malformed tuple always raises TypeError -------------


def _generator():
    yield "a"


@pytest.mark.parametrize(
    "path",
    [(), (1,), ("a", True), ("a", 1.5), _generator(), {"a"}],
    ids=["empty", "int-root", "bool-segment", "float-segment", "generator", "set"],
)
def test_invalid_name_raises_type_error(h, path):
    with pytest.raises(TypeError):
        h.lookup(path)


# --- a tuple path is exact: no dot splitting, no quote syntax, no strip -


def test_tuple_root_is_exact_no_dot_split(h):
    # The literal top-level key "a.b" is reachable only as a tuple path;
    # the string form dot-splits into root "a", segment "b" instead.
    assert h.lookup(("a.b",)) == "literal-dot-root"
    assert h.lookup("a.b") == "nested"


def test_tuple_segment_with_dot_is_exact(h):
    assert h.lookup(("a", "x.y")) == "dotted-segment"


@pytest.mark.parametrize(
    "segment,expect",
    [("it's", "apostrophe-value"), ('she said "hi"', "doublequote-value")],
    ids=["single-quote-content", "double-quote-content"],
)
def test_tuple_segment_quote_content_is_exact(h, segment, expect):
    assert h.lookup(("a", segment)) == expect


def test_tuple_segment_int_index(h):
    assert h.lookup(("lst", 1)) == "z1"


def test_tuple_segment_negative_index_misses(h):
    assert h.lookup(("lst", -1), default_value="D") == "D"


def test_tuple_str_segment_against_array_is_a_type_mismatch(h):
    # An exact string "0" against a list is Puppet's own quoted '0'
    # segment: a hash-key lookup, not an index -- it never becomes 0.
    with pytest.raises(HieraLookupError, match="Got Array"):
        h.lookup(("lst", "0"))


def test_tuple_str_vs_int_segment_against_a_hash(h):
    assert h.lookup(("h", 0)) == "int-zero"
    assert h.lookup(("h", "0")) == "str-zero"


# --- a tuple path inside the other call shapes --------------------------


def test_tuple_inside_name_list(h):
    assert h.lookup(["missing", ("a.b",)]) == "literal-dot-root"


def test_tuple_inside_name_hash(h):
    assert h.lookup({"name": ("a.b",)}) == "literal-dot-root"


def test_tuple_path_in_contains(h):
    assert ("p.q", "r") in h
    assert ("p.q", "missing") not in h


def test_tuple_path_via_getitem(h):
    assert h[("p.q", "r"),] == "found-under-pq"


def test_explain_tuple_path_matches_quoted_string(h):
    tuple_result = h.explain(("p.q", "r"))
    string_result = h.explain('"p.q".r')
    assert tuple_result.text() == string_result.text()
    assert tuple_result.to_hash() == string_result.to_hash()


def test_block_receives_the_tuple(h):
    calls = []

    def block(name):
        calls.append(name)
        return "from-block"

    result = h.lookup(("missing", "x"), block=block)
    assert result == "from-block"
    assert calls == [("missing", "x")]


# --- KeyNotFoundError message text for a tuple path ---------------------


def test_key_not_found_message_for_tuple_path(h):
    with pytest.raises(KeyNotFoundError) as exc_info:
        h.lookup(("missing", "x"))
    assert str(exc_info.value) == (
        "Function lookup() did not find a value for the name 'missing.x'"
    )


def test_key_not_found_message_for_mixed_list(h):
    with pytest.raises(KeyNotFoundError) as exc_info:
        h.lookup(["missing", ("also.missing", "x")])
    message = str(exc_info.value)
    assert "missing" in message
    assert '"also.missing".x' in message


# --- equivalence with the matching quoted string, over real Puppet's ----
# --- own recorded conformance goldens ------------------------------------


def _conformance_module(name):
    """Load ``tests/conformance/<name>.py`` by its real path, registering
    it in ``sys.modules`` under its own bare name so ``_ours.py``'s own
    top-level ``import _golden`` (also a bare, sys.path-relative import)
    resolves -- regardless of whether ``tests/conformance`` tests were
    collected in this same run (this module needs to work standalone,
    including under ``--ignore=tests/conformance``)."""
    module = sys.modules.get(name)
    if module is not None:
        return module
    conformance_dir = (
        Path(hyera.__file__).resolve().parents[2] / "tests" / "conformance"
    )
    spec = importlib.util.spec_from_file_location(
        name, conformance_dir / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _tuple_candidates(golden):
    """``(case_dir, case, query, path)`` for every conformance query whose
    ``key`` needs more than :func:`~hyera._lookup.navigation.split_key`'s
    single-segment identity -- multiple segments, or a quote -- and whose
    split segments start with a ``str`` root (an int-root query has no
    tuple-path equivalent at all: a tuple path's own root is always a
    ``str``).
    ``explain:`` queries and ones already carrying a known ``divergence``
    are skipped -- this test is about tuple/string equivalence, not
    re-litigating an unrelated, already-tracked Puppet-fidelity gap.
    """
    for case_dir in golden.case_dirs():
        case = golden.load_case(case_dir)
        unmet = False
        for req in case.get("requires") or []:
            try:
                __import__(req)
            except ImportError:
                unmet = True
                break
        if unmet:
            continue
        for query in case["queries"]:
            if query.get("explain") or query.get("divergence"):
                continue
            key = query.get("key")
            if not isinstance(key, str):
                continue
            try:
                segments = split_key(key, lambda problem: ValueError(problem))
            except Exception:
                continue
            if not segments or not isinstance(segments[0], str):
                continue
            if len(segments) <= 1 and "'" not in key and '"' not in key:
                continue
            yield case_dir, case, query, tuple(segments)


def test_tuple_path_matches_conformance_goldens():
    golden = _conformance_module("_golden")
    ours = _conformance_module("_ours")
    seen = 0
    for case_dir, case, query, path in _tuple_candidates(golden):
        golden_data = golden.read_golden(case_dir)
        golden_result = golden_data["results"][golden.query_id(query)]
        want = ours.expected(query, golden_result)

        try:
            string_actual = ours.run_api(case_dir, case, query, golden_data)
        except ours.AdapterUnsupported:
            continue

        tuple_query = dict(query)
        tuple_query["key"] = path
        try:
            tuple_actual = ours.run_api(case_dir, case, tuple_query, golden_data)
        except ours.AdapterUnsupported:
            continue

        context = (case_dir.name, query, path)
        assert tuple_actual["status"] == string_actual["status"], (
            context,
            tuple_actual,
            string_actual,
        )
        assert tuple_actual["status"] == want["status"], (context, tuple_actual, want)
        if want["status"] == "found":
            assert ours.canonical(
                tuple_actual["value"], query.get("ordered")
            ) == ours.canonical(want["value"], query.get("ordered"))
        elif want["status"] == "error":
            error_match = query.get("error_match")
            if error_match:
                assert re.search(error_match, tuple_actual.get("message", "")), (
                    context,
                    tuple_actual,
                )
            # Byte-identical display text is only guaranteed when the
            # golden's own key spelling is already join_key's canonical
            # form -- its quote-CHARACTER choice, not just its segments,
            # has to match too (a hand-written golden may prefer single
            # quotes where join_key's own default is double).
            if join_key(path) == query["key"]:
                assert tuple_actual["message"] == string_actual["message"], context
        seen += 1
    assert seen >= 20, "covered only {} conformance queries".format(seen)
    print("tuple-path equivalence covered {} conformance queries".format(seen))
