"""CLI output: render formats, Sensitive redaction, large numbers and ``--explain``."""

import json
import logging

import pytest

duho = pytest.importorskip("duho")

import hyera  # noqa: E402
from hyera.cli import main  # noqa: E402
from cli_support import (  # noqa: F401
    _error_records,
    _flags_argv,
    flags_root,
    hiera_root,
    render_root,
)


@pytest.fixture
def values_root(make_tree):
    """A tree exercising every CLI output type, including ``Sensitive``."""
    return make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": """\
                str: hello
                int: 42
                bool: true
                flt: 1.5
                nested:
                  b: 2
                  a:
                    - 1
                    - z: 1
                      y: 2
                lst:
                  - 1
                  - - 2
                    - 3
                  - k: v
                secret: hunter2
                secret_hash:
                  user: admin
                  pass: hunter2
                lookup_options:
                  secret: { convert_to: Sensitive }
                  secret_hash: { convert_to: Sensitive }
                """},
        facts={"role": "web"},
    )


_RENDER_AS_EXPECTED = {
    "str": ("one\n", "--- one\n", '"one"\n'),
    "nil": ("\n", "---\n", "null\n"),
    "nested": (
        '{"b"=>2, "a"=>[1, {"z"=>1, "y"=>2}]}\n',
        '---\nb: 2\na:\n- 1\n- z: 1\n  "y": 2\n',
        '{"b":2,"a":[1,{"z":1,"y":2}]}\n',
    ),
    "secret": (
        "Sensitive [value redacted]\n",
        "--- Sensitive [value redacted]\n",
        '"Sensitive [value redacted]"\n',
    ),
    "unicode": ("café ☃\n", "--- café ☃\n", '"café ☃"\n'),
}


@pytest.mark.parametrize("key", list(_RENDER_AS_EXPECTED))
@pytest.mark.parametrize("fmt,idx", [("s", 0), ("yaml", 1), ("json", 2)])
def test_render_as(fmt, idx, key, render_root, capsys):
    rc = main(
        [
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--render-as",
            fmt,
            key,
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert out == _RENDER_AS_EXPECTED[key][idx]


def test_default_render_is_yaml(render_root, capsys):
    rc = main(
        [
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "str",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "--- one\n"


def test_render_as_is_case_insensitive(render_root, capsys):
    rc = main(
        [
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--render-as",
            "JSON",
            "str",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == '"one"\n'


def test_unknown_render_format_exit_2(render_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "--hiera_config",
                str(render_root / "hiera.yaml"),
                "--facts",
                str(render_root / "facts.yaml"),
                "--render-as",
                "foo",
                "str",
            ]
        )
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == "Unknown rendering format 'foo'"


def test_json_nonfinite_exit_2(render_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "--hiera_config",
                str(render_root / "hiera.yaml"),
                "--facts",
                str(render_root / "facts.yaml"),
                "--render-as",
                "json",
                "nan",
            ]
        )
    assert rc == 2
    assert "NaN not allowed in JSON" in _error_records(caplog)[-1].getMessage()


@pytest.mark.parametrize(
    "fmt,expected",
    [("s", "fallback\n"), ("json", '"fallback"\n'), ("yaml", "--- fallback\n")],
)
def test_default_in_each_format(fmt, expected, render_root, capsys):
    rc = main(
        [
            "nope::key",
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--default",
            "fallback",
            "--render-as",
            fmt,
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == expected


@pytest.mark.parametrize("flag", ["-o", "--output"])
def test_output_flag_removed(flag, hiera_root):
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "app::name",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
                flag,
                "json",
            ]
        )
    assert exc.value.code == 2


def test_sensitive_redacted_in_output(values_root, capsys):
    """A ``Sensitive``-converted value is redacted in every output format,
    including the CLI's default (``s``-like) output, and never the wrapped
    secret.
    """
    rc = main(
        [
            "--hiera_config",
            str(values_root / "hiera.yaml"),
            "--facts",
            str(values_root / "facts.yaml"),
            "--render-as",
            "s",
            "secret",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert out.strip() == "Sensitive [value redacted]"

    rc = main(
        [
            "--hiera_config",
            str(values_root / "hiera.yaml"),
            "--facts",
            str(values_root / "facts.yaml"),
            "--render-as",
            "json",
            "secret",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert json.loads(out) == "Sensitive [value redacted]"


def test_hocon_duration_survives_yaml_output(make_tree, capsys):
    pytest.importorskip("pyhocon")
    root = make_tree(
        {
            "defaults": {"data_hash": "hocon_data"},
            "hierarchy": [{"name": "c", "path": "common.conf"}],
        },
        files={"data/common.conf": "dur = 10s\n"},
        facts={"role": "web"},
    )
    rc = main(
        [
            "dur",
            "--hiera_config",
            str(root / "hiera.yaml"),
            "--facts",
            str(root / "facts.yaml"),
            "--render-as",
            "yaml",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "--- 10s"


# -- --explain / --explain-options ------------------------------------------


def _hiera_for(flags_root):
    return hyera.Hiera(
        str(flags_root / "hiera.yaml"),
        scope=hyera.Scope(
            facts={"role": "web", "os": {"family": "RedHat"}},
            server_facts={"serverversion": "8.10.0"},
            node_name="web01.example.com",
        ),
    )


def test_explain_text(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "--explain", "str"))
    assert rc == 0
    expected = _hiera_for(flags_root).explain("str").text()
    if not expected.endswith("\n"):
        expected += "\n"
    assert capsys.readouterr().out == expected


def test_explain_miss_exits_0(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "--explain", "nope"))
    assert rc == 0


def test_explain_json(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "--explain", "--render-as", "json", "nope"))
    assert rc == 0
    out = capsys.readouterr().out
    expected = json.loads(json.dumps(_hiera_for(flags_root).explain("nope").to_hash()))
    assert json.loads(out) == expected


def test_explain_options_without_key(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "--explain-options"))
    assert rc == 0
    expected = _hiera_for(flags_root).explain("__global__", explain_options=True).text()
    if not expected.endswith("\n"):
        expected += "\n"
    assert capsys.readouterr().out == expected


def test_explain_config_error_exit_2(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "--hiera_config",
                str(flags_root / "nosuch.yaml"),
                "--facts",
                str(flags_root / "facts.yaml"),
                "--node",
                "web01.example.com",
                "--explain",
                "str",
            ]
        )
    assert rc == 2


@pytest.fixture
def bigint_root(make_tree):
    return make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": "big: 1" + "0" * 4999 + "\n"},
        facts={"role": "web"},
    )


@pytest.mark.parametrize("fmt", ["s", "json", "yaml"])
def test_integer_of_five_thousand_digits_renders_in_every_format(
    fmt, bigint_root, capsys
):
    rc = main(_bigint_argv(bigint_root, "--render-as", fmt, "big"))
    assert rc == 0
    assert "1" + "0" * 4999 in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["s", "json", "yaml"])
def test_explain_of_a_key_with_a_4301_digit_segment_in_every_format(
    fmt, bigint_root, capsys
):
    key = "big." + "1" * 4301
    rc = main(_bigint_argv(bigint_root, "--explain", "--render-as", fmt, key))
    assert rc in (0, 1)
    assert "1" * 4301 in capsys.readouterr().out


def _bigint_argv(root, *extra):
    return [
        "--hiera_config",
        str(root / "hiera.yaml"),
        "--facts",
        str(root / "facts.yaml"),
    ] + list(extra)


def test_render_as_empty_is_an_unknown_format(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "--render-as", "", "h"))
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == "Unknown rendering format ''"
