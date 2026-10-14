"""CLI arguments: merge flags, scope variables, strict modes and ``--``."""

import logging
import os
import subprocess
import sys
import typing

import pytest

duho = pytest.importorskip("duho")

from hyera.cli import Lookup, main  # noqa: E402
from cli_support import (  # noqa: F401
    _SRC,
    _error_records,
    _flags_argv,
    flags_root,
)

_DEEP_ONLY_TEXT = (
    "The options --knock-out-prefix, --sort-merged-arrays, and "
    "--merge-hash-arrays are only available with '--merge deep'"
)


_MERGE_UNKNOWN_TEXT = (
    "The --merge option only accepts 'first', 'hash', 'unique', or 'deep'"
)


@pytest.mark.parametrize(
    "flag", ["--knock-out-prefix=--", "--sort-merged-arrays", "--merge-hash-arrays"]
)
def test_deep_only_options_need_merge_deep(flag, flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "--merge", "hash", flag, "h"))
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == _DEEP_ONLY_TEXT


def test_deep_only_option_without_merge(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "--knock-out-prefix", "x", "h"))
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == _DEEP_ONLY_TEXT


@pytest.mark.parametrize(
    "value", ["bogus", "reverse_deep", "unconstrained_deep", "array", "set"]
)
def test_merge_rejects_unknown(value, flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "--merge", value, "h"))
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == _MERGE_UNKNOWN_TEXT


@pytest.mark.parametrize(
    "case,flags,expected",
    [
        ("plain", [], '{"items":["b","a","c","d","--b"],"rows":[{"y":2},{"x":1}]}'),
        (
            "knockout",
            ["--knock-out-prefix", "--"],
            '{"items":["a","c","d"],"rows":[{"y":2},{"x":1}]}',
        ),
        (
            "hash_arrays",
            ["--merge-hash-arrays"],
            '{"items":["b","a","c","d","--b"],"rows":[{"y":2,"x":1}]}',
        ),
        (
            "all_three",
            ["--knock-out-prefix", "--", "--merge-hash-arrays"],
            '{"items":["a","c","d"],"rows":[{"y":2,"x":1}]}',
        ),
    ],
)
def test_deep_merge_flags(case, flags, expected, flags_root, capsys):
    rc = main(
        _flags_argv(flags_root, "--merge", "deep", *flags, "--render-as", "json", "h")
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == expected


@pytest.mark.parametrize(
    "spelling",
    [["--knock-out-prefix", "--"], ["--knock-out-prefix=--"]],
    ids=["two-token", "equals"],
)
def test_knock_out_prefix_double_dash_in_either_spelling(spelling, flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root, "--merge", "deep", *spelling, "--render-as", "json", "h"
        )
    )
    assert rc == 0
    assert (
        capsys.readouterr().out.strip()
        == '{"items":["a","c","d"],"rows":[{"y":2},{"x":1}]}'
    )


@pytest.mark.parametrize(
    "spelling", [["--default", "--"], ["--default=--"]], ids=["two-token", "equals"]
)
def test_default_double_dash_in_either_spelling(spelling, flags_root, capsys):
    rc = main(_flags_argv(flags_root, *spelling, "--render-as", "s", "nokey"))
    assert rc == 0
    assert capsys.readouterr().out == "--\n"


@pytest.mark.parametrize(
    "spelling",
    [["--scope", "--"], ["--scope=--"], ["--strict", "--"], ["--strict=--"]],
)
def test_double_dash_value_is_reported_as_given(spelling, flags_root, caplog, capfd):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, *spelling, "h"))
    captured = capfd.readouterr()
    assert rc == 2
    assert "hyera-literal" not in caplog.text + captured.err + captured.out
    assert "--" in (caplog.text + captured.err)


def test_knock_out_prefix_double_dash_equals_form_in_a_subprocess(flags_root):
    argv = _flags_argv(
        flags_root,
        "--merge",
        "deep",
        "--knock-out-prefix=--",
        "--render-as",
        "json",
        "h",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"] + argv,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": _SRC},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == '{"items":["a","c","d"],"rows":[{"y":2},{"x":1}]}'


def test_scope_variables(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "v", "--render-as", "s"))
    assert rc == 0
    assert capsys.readouterr().out == "web-RedHat|production|production|[]|8.10.0\n"


def _warning_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "hyera._scope.scope" and r.levelno == logging.WARNING
    ]


def test_strict_modes_off(flags_root, capsys, caplog):
    with caplog.at_level(logging.WARNING):
        rc = main(_flags_argv(flags_root, "--strict", "off", "u", "--render-as", "s"))
    assert rc == 0
    assert capsys.readouterr().out == "[]\n"
    assert not _warning_records(caplog)


def test_strict_modes_warning(flags_root, capsys, caplog):
    with caplog.at_level(logging.WARNING):
        rc = main(
            _flags_argv(flags_root, "--strict", "warning", "u", "--render-as", "s")
        )
    assert rc == 0
    assert capsys.readouterr().out == "[]\n"
    assert any(
        "Undefined variable 'nosuch'" in r.getMessage()
        for r in _warning_records(caplog)
    )


def test_strict_modes_error(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "--strict", "error", "u", "--render-as", "s"))
    assert rc == 2
    assert "Undefined variable 'nosuch'" in _error_records(caplog)[-1].getMessage()


def test_scope_flag_values_are_yaml(flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root,
            "--scope",
            "n=0",
            "--scope",
            "b=false",
            "--scope",
            "o.x=1",
            "--scope",
            "l=[a,b]",
            "sv",
            "--render-as",
            "s",
        )
    )
    assert rc == 0
    assert capsys.readouterr().out == '0|false|1|["a", "b"]\n'


def test_scope_flag_dotted_segments_share_an_existing_hash():
    from hyera.cli import _parse_scope

    # A second --scope reusing an already-built nested Hash segment (not
    # the scalar-conflict case test_scope_flag_errors covers) is never an
    # error.
    assert _parse_scope(["a.b=1", "a.c=2"]) == {"a": {"b": 1, "c": 2}}


def test_parse_scope_empty_item_and_empty_value_direct():
    from hyera.cli import _parse_scope, _parse_scope_value

    # An empty value (--scope k=) is None -- distinct from the yaml_data
    # loader's own empty-document False.
    assert _parse_scope_value("") is None
    # An empty item (an empty string among the --scope values) is skipped
    # outright, not a parse error.
    assert _parse_scope(["", "a=1"]) == {"a": 1}


@pytest.mark.parametrize(
    "case,scope_arg",
    [("noequals", "noequals"), ("eqx", "=x"), ("nested-under-scalar", None)],
)
def test_scope_flag_errors(case, scope_arg, flags_root, caplog):
    if case == "nested-under-scalar":
        args = _flags_argv(flags_root, "--scope", "n=1", "--scope", "n.x=2", "str")
    else:
        args = _flags_argv(flags_root, "--scope", scope_arg, "str")
    with caplog.at_level(logging.ERROR):
        rc = main(args)
    assert rc == 2


@pytest.mark.parametrize(
    "flag",
    ["-c", "--config", "--deep", "--knockout-prefix", "--compile", "--trusted"],
)
def test_removed_flags_rejected(flag, flags_root):
    with pytest.raises(SystemExit) as exc:
        main(_flags_argv(flags_root, flag, "x", "str"))
    assert exc.value.code == 2


def test_keys_after_double_dash(flags_root, capsys):
    # Everything after a bare "--" is treated as keys (Puppet's own
    # convention), so "--render-as s" here never reaches the option parser
    # and the default (yaml) format applies.
    rc = main(_flags_argv(flags_root, "--", "str"))
    assert rc == 0
    assert capsys.readouterr().out == "--- one\n"


@pytest.mark.parametrize(
    "flag, field",
    [
        ("--merge", "merge"),
        ("--knock-out-prefix", "knock_out_prefix"),
        ("--type", "value_type"),
        ("--default", "default"),
        ("--facts", "facts"),
        ("--node", "node"),
        ("--hiera_config", "hiera_config"),
        ("--environment", "environment"),
        ("--environmentpath", "environmentpath"),
        ("--modulepath", "modulepath"),
        ("--basemodulepath", "basemodulepath"),
        ("--codedir", "codedir"),
        ("--strict", "strict"),
        ("--render-as", "render_as"),
    ],
)
@pytest.mark.parametrize("value", ["--", "-x", "--explain"])
def test_a_value_option_takes_the_next_word_whatever_it_looks_like(flag, field, value):
    command = duho.parse(Lookup, [flag, value, "k"])
    assert getattr(command, field) == value
    assert command.keys == ["k"]
    assert command.explain is False


def test_scope_takes_the_next_word_whatever_it_looks_like():
    command = duho.parse(Lookup, ["--scope", "--", "-s", "-x", "k"])
    assert command.scope == ["--", "-x"]
    assert command.keys == ["k"]


def test_every_single_value_option_is_covered_by_the_test_above():
    hints = typing.get_type_hints(Lookup, include_extras=True)
    valued = {
        name
        for name, hint in hints.items()
        if not name.startswith("_")
        and typing.get_origin(hint) is typing.Annotated
        and typing.get_args(hint)[0] in (typing.Optional[str], typing.List[str])
        and name != "keys"
    }
    assert valued == {
        "merge",
        "knock_out_prefix",
        "value_type",
        "default",
        "facts",
        "node",
        "scope",
        "hiera_config",
        "environment",
        "environmentpath",
        "modulepath",
        "basemodulepath",
        "codedir",
        "strict",
        "render_as",
    }
