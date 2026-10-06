"""The golden linter's ``hash_inspect`` rule."""

import pytest

from _golden import hash_inspect_problems


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
