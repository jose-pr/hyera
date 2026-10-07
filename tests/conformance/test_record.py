"""``record.py``'s own command-building logic. Needs no Puppet and no WSL --
these are pure-function checks of the argv ``_command`` builds, not an
actual run of it.
"""

import base64
import datetime
import re
from pathlib import Path

import pytest
import yaml

import _golden
from _golden import (
    CASES,
    EMIT_BEGIN,
    EMIT_END,
    FIXTURE_MODULES,
    _format2_problems,
    _leak_hits,
    apply_argv,
    input_digest,
    missing_requirements,
    parse_emitted,
    string_leaves,
)
from _ours import (
    AdapterUnsupported,
    canonical,
    is_ordered,
    missing_warnings,
    run_expression,
    to_data,
)
from hyera import Sensitive
from record import (
    _apply_command,
    _command,
    _error_text,
    _iso_args,
    _mnt_path,
    _runner_path,
)


def test_wsl_runner_uses_dash_e_not_dash_dash():
    # wsl.exe's `--` relays argv through a Linux shell that strips a lone or wrapping
    # single quote; `-e` does not (`ruby -e 'puts ARGV.inspect' "'a.b'"` delivers
    # `["a.b"]` with `--`, `["'a.b'"]` with `-e`).
    cmd, cwd = _command("wsl:SomeDistro", Path("/some/case"), ["lookup", "k"])
    assert cwd is None
    assert "--" not in cmd
    i = cmd.index("-e")
    assert cmd[i + 1 :] == ["puppet", "lookup", "k"]


def test_wsl_runner_without_named_distro_also_uses_dash_e():
    cmd, cwd = _command("wsl", Path("/some/case"), ["lookup", "k"])
    assert cwd is None
    assert "--" not in cmd
    i = cmd.index("-e")
    assert cmd[i + 1 :] == ["puppet", "lookup", "k"]


def test_wsl_runner_passes_cd_before_the_command():
    cmd, _cwd = _command("wsl:SomeDistro", Path("/some/case"), ["lookup", "k"])
    cd_i = cmd.index("--cd")
    e_i = cmd.index("-e")
    assert cd_i < e_i
    assert cmd[cd_i + 1] == "/some/case" or cmd[cd_i + 1] == str(Path("/some/case"))


def test_local_runner_unaffected():
    cmd, cwd = _command("local", Path("/some/case"), ["lookup", "k"])
    assert cmd == ["puppet", "lookup", "k"]
    assert cwd == Path("/some/case")


def test_error_text_keeps_every_error_line_and_drops_warnings():
    lines = [
        "Warning: first",
        "Error: Could not run: outer",
        "Warning: interleaved",
        "Error: inner cause",
        "",
    ]
    assert _error_text(lines, Path("/x/cases/c"), "/root") == "outer\ninner cause"


def test_error_text_is_empty_without_an_error_line():
    assert _error_text(["Warning: only"], Path("/x/cases/c"), "/root") == ""


def test_missing_requirements_names_only_the_unimportable_modules():
    case = {"requires": ["json", "no_such_module_for_hyera_tests"]}
    assert missing_requirements(case) == ["no_such_module_for_hyera_tests"]
    assert missing_requirements({}) == []


def test_key_order_is_compared_unless_a_query_opts_out():
    assert is_ordered({}) and is_ordered({"ordered": True})
    assert not is_ordered({"ordered": False})
    assert canonical({"a": 1, "b": 2}) != canonical({"b": 2, "a": 1})
    assert canonical({"a": 1, "b": 2}, False) == canonical({"b": 2, "a": 1}, False)


def test_a_recorded_warning_hyera_did_not_log_is_reported():
    case_dir = Path("/x/cases/c")
    recorded = {"warnings": ["Undefined variable 'nope';"]}
    assert missing_warnings(recorded, {"warnings": []}, case_dir) == [
        "Undefined variable 'nope';"
    ]
    logged = {"warnings": ["Undefined variable 'nope'"]}
    assert missing_warnings(recorded, logged, case_dir) == []


def test_format_two_golden_without_exit_status_or_platform_is_flagged():
    golden = {"gems": {}, "results": {"q": {"status": "found"}}}
    problems = _format2_problems(golden)
    assert any("platform" in p for p in problems)
    assert any("exit_status" in p for p in problems)
    ok = {"platform": "x", "gems": {"hocon": "1"}, "results": {"q": {"exit_status": 0}}}
    assert _format2_problems(ok) == []


def test_leak_scan_reads_text_not_json_escaping():
    # An escaped newline after a colon is not a Windows drive prefix.
    multi_line = {"message": "Wrapped exception:\nTried to load"}
    assert _leak_hits(string_leaves(multi_line)) == []
    assert _leak_hits(string_leaves({"m": ["see C:\\Users\\x"]})) != []


def test_expression_command_runs_apply_with_the_fixture_module_and_facts_terminus():
    case = {"puppet_args": ["--environment", "dev"]}
    query = {"expression": 'lookup("k")'}
    args = _apply_command("wsl:SomeDistro", case, query, _iso_args("/tmp/iso"))
    assert args[0] == "apply"
    modules = args[args.index("--basemodulepath") + 1]
    assert modules == "./modules:" + _runner_path("wsl", FIXTURE_MODULES)
    assert modules.endswith("/puppet_modules")
    assert args[args.index("--facts_terminus") + 1] == "hyera_file"
    assert args[args.index("--node_name_value") + 1] == "golden.example.com"
    assert args[args.index("--strict") + 1] == "warning"
    assert args[args.index("--environment") + 1] == "dev"
    assert args[-2:] == ["-e", 'hyera_fixture::emit(lookup("k"))']


def test_apply_arguments_take_a_query_strict_over_the_default():
    assert apply_argv({}, {"puppet_args": ["--strict", "error"]}) == [
        "--strict",
        "error",
    ]
    assert apply_argv({}, {}) == ["--strict", "warning"]


def test_a_local_runner_names_the_fixture_directory_as_it_is():
    assert _runner_path("local", FIXTURE_MODULES) == str(FIXTURE_MODULES)


def test_a_wsl_runner_names_a_windows_directory_under_mnt():
    assert _mnt_path("C:/a/b") == "/mnt/c/a/b"
    assert _mnt_path("/home/a/b") == "/home/a/b"
    assert _runner_path("wsl", FIXTURE_MODULES) == _mnt_path(
        FIXTURE_MODULES.resolve().as_posix()
    )


def test_the_emitted_value_is_read_between_the_markers():
    document = '{"a": [1, 2.0]}'
    out = "noise\n{}\n{}\n{}\nNotice: done\n".format(EMIT_BEGIN, document, EMIT_END)
    assert parse_emitted(out) == (True, {"a": [1, 2.0]})
    assert parse_emitted("{}\nnull\n{}\n".format(EMIT_BEGIN, EMIT_END)) == (True, None)


@pytest.mark.parametrize(
    "out",
    ["", "no markers", EMIT_BEGIN + "\n1\n", EMIT_BEGIN + "\nnot json\n" + EMIT_END],
)
def test_output_without_a_complete_json_document_is_not_a_value(out):
    assert parse_emitted(out) == (False, None)


def test_the_fixture_function_and_terminus_ship_with_the_harness():
    lib = FIXTURE_MODULES / "hyera_fixture" / "lib" / "puppet"
    assert (lib / "functions" / "hyera_fixture" / "emit.rb").is_file()
    assert (lib / "indirector" / "facts" / "hyera_file.rb").is_file()


def _case_in(path, queries):
    path.mkdir()
    (path / "case.yaml").write_text(yaml.safe_dump({"queries": queries}))
    return path


def test_the_fixture_module_is_part_of_an_expression_case_digest_only(
    tmp_path, monkeypatch
):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "f.rb").write_text("one\n")
    monkeypatch.setattr(_golden, "FIXTURE_MODULES", fixtures)
    expr_dir = _case_in(tmp_path / "expr", [{"id": "q", "expression": "1"}])
    key_dir = _case_in(tmp_path / "key", [{"key": "k"}])
    before = (input_digest(expr_dir), input_digest(key_dir))
    (fixtures / "f.rb").write_text("two\n")
    after = (input_digest(expr_dir), input_digest(key_dir))
    assert before[0] != after[0]
    assert before[1] == after[1]


def test_to_data_tags_what_json_lacks():
    when = datetime.datetime(2020, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    assert to_data(Sensitive("s")) == {"__ptype": "Sensitive", "__pvalue": "s"}
    assert to_data(re.compile("a.b")) == {"__ptype": "Regexp", "__pvalue": "a.b"}
    assert to_data(when) == {
        "__ptype": "Timestamp",
        "__pvalue": "2020-01-02T03:04:05.000000000 UTC",
    }
    assert to_data(b"hi") == {
        "__ptype": "Binary",
        "__pvalue": base64.b64encode(b"hi").decode(),
    }
    assert to_data({1: 2, "a": 3}) == {"__ptype": "Hash", "__pvalue": [1, 2, "a", 3]}
    assert to_data({"a": [1, 2.0, None, True]}) == {"a": [1, 2.0, None, True]}
    assert to_data(Sensitive({"k": b"x"})) == {
        "__ptype": "Sensitive",
        "__pvalue": {"k": {"__ptype": "Binary", "__pvalue": "eA=="}},
    }


def test_to_data_refuses_what_puppet_data_cannot_hold():
    with pytest.raises(TypeError):
        to_data(float("nan"))
    with pytest.raises(TypeError):
        to_data(object())


def test_a_python_spec_naming_nothing_hyera_has_fails_naming_the_query():
    query = {
        "id": "bad-spec",
        "expression": "1",
        "python": {"target": "types", "method": "NoSuchType"},
    }
    golden = {"puppet_version": "8.10.0", "node": "golden.example.com"}
    case_dir = CASES / "apply-getvar"
    with pytest.raises(AdapterUnsupported, match="bad-spec"):
        run_expression(case_dir, {}, query, golden)
