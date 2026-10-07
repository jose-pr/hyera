"""The golden linter's ``hash_inspect`` rule and the expression-query schema."""

import pytest

from _golden import _apply_result_problems, _expression_problems, hash_inspect_problems


def _expression_query(**overrides):
    query = {
        "id": "q",
        "expression": 'lookup("k")',
        "python": {"target": "hiera", "method": "lookup", "args": ["k"]},
    }
    query.update(overrides)
    return query


def test_a_well_formed_expression_query_has_no_problems():
    assert _expression_problems(0, _expression_query()) == []


def test_a_key_query_with_a_python_spec_is_refused():
    query = {"key": "k", "python": {"target": "hiera", "method": "lookup"}}
    assert any("python belongs" in p for p in _expression_problems(0, query))


@pytest.mark.parametrize(
    "query",
    [
        {"key": "k", "expression": "1", "python": {}},
        {"id": "q"},
    ],
)
def test_exactly_one_of_key_and_expression(query):
    assert any("exactly one of" in p for p in _expression_problems(0, query))


@pytest.mark.parametrize(
    "field", ["merge", "default", "type", "explain", "hash_inspect"]
)
def test_a_key_only_field_is_refused_with_an_expression(field):
    problems = _expression_problems(0, _expression_query(**{field: "x"}))
    assert any("not allowed with expression" in p for p in problems)


def test_an_expression_query_needs_an_id_and_a_python_spec():
    query = _expression_query()
    del query["id"], query["python"]
    problems = _expression_problems(0, query)
    assert any("needs an id" in p for p in problems)
    assert any("needs a python call spec" in p for p in problems)


@pytest.mark.parametrize(
    "spec,fragment",
    [
        ("lookup", "mapping"),
        ({"target": "shell", "method": "lookup"}, "target must be"),
        ({"target": "hiera", "method": "keys"}, "hiera method"),
        ({"target": "types", "method": "lookup"}, "types method"),
        ({"target": "hiera", "method": "lookup", "extra": 1}, "unknown fields"),
        ({"target": "hiera", "method": "lookup", "args": "k"}, "args must be a list"),
        ({"target": "hiera", "method": "lookup", "kwargs": []}, "kwargs must be"),
        ({"target": "hiera", "method": "lookup", "block": "sh"}, "block must be"),
        ({"target": "hiera", "method": "lookup", "params": []}, "params belongs"),
        ({"target": "types", "method": "Integer", "block": "echo"}, "takes no block"),
    ],
)
def test_a_malformed_python_spec_is_refused(spec, fragment):
    problems = _expression_problems(0, _expression_query(python=spec))
    assert any(fragment in p for p in problems), problems


def test_an_apply_result_must_carry_its_channel_and_a_status():
    assert (
        _apply_result_problems("q", {"channel": "apply", "status": "found", "value": 1})
        == []
    )
    assert _apply_result_problems("q", {"status": "found", "value": 1})
    assert _apply_result_problems("q", {"channel": "apply", "status": "explained"})
    assert _apply_result_problems("q", {"channel": "apply", "status": "error"})


@pytest.mark.parametrize(
    "raw",
    ["{'a' => 1}", "{'a' => 1, 'b' => {'c' => 2}}", "[{'k' => 'v'}]"],
)
def test_hash_inspect_is_refused_for_text_puppets_formatter_produced(raw):
    result = {"status": "found", "raw_value": raw, "value": raw.replace(" => ", "=>")}
    problems = hash_inspect_problems("q", result)
    assert len(problems) == 1
    assert "not Ruby inspect output" in problems[0]


@pytest.mark.parametrize(
    "result",
    [
        {"status": "found", "raw_value": '{"a" => 1}', "value": '{"a"=>1}'},
        {"status": "found", "value": '{"a"=>1}'},
        {"status": "error", "message": "x"},
    ],
)
def test_hash_inspect_is_allowed_for_ruby_inspect_output(result):
    assert hash_inspect_problems("q", result) == []
