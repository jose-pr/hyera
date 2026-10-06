"""``record.py``'s own command-building logic. Needs no Puppet and no WSL --
these are pure-function checks of the argv ``_command`` builds, not an
actual run of it.
"""

from pathlib import Path

from _golden import (
    _format2_problems,
    _leak_hits,
    missing_requirements,
    string_leaves,
)
from _ours import canonical, is_ordered, missing_warnings
from record import _command, _error_text


def test_wsl_runner_uses_dash_e_not_dash_dash():
    # wsl.exe's `--` form relays argv through a Linux-side shell that strips a lone or
    # wrapping single quote; `-e` does not. `wsl.exe -d <distro> -- ruby -e 'puts
    # ARGV.inspect' "'a.b'"` delivers `["a.b"]`, the `-e` form `["'a.b'"]` (the true
    # argument).
    cmd, cwd = _command("wsl:FedoraLinux-44", Path("/some/case"), ["lookup", "k"])
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
    cmd, _cwd = _command("wsl:FedoraLinux-44", Path("/some/case"), ["lookup", "k"])
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
