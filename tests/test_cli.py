"""CLI behavior and exit codes (unattended-friendly)."""

import json
import logging
import os
import runpy
import subprocess
import sys

import pytest
import yaml

duho = pytest.importorskip("duho")

import hyera  # noqa: E402
from hyera.cli import main  # noqa: E402


def _error_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "hyera.cli" and r.levelno == logging.ERROR
    ]


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
    )


@pytest.fixture
def mergefirst_root(make_tree):
    """A tree where ``lookup_options`` declares ``unique`` for ``classes``,
    so an explicit ``--merge first`` overriding it is observable.

    Uses a flat ``os_family`` scope var, not a dotted ``facts.os.family``
    one: ``--scope`` rejects a dotted name outright now (a Puppet variable
    name cannot contain ``.``), and this fixture only needs *some*
    per-scope hierarchy level, not specifically a dotted one.
    """
    return make_tree(
        {
            "hierarchy": [
                {"name": "os", "path": "os/%{os_family}.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/common.yaml": """\
                classes:
                  - base
                lookup_options:
                  classes: { merge: unique }
                """,
            "data/os/RedHat.yaml": """\
                classes:
                  - redhat
                """,
        },
    )


@pytest.fixture
def render_root(make_tree):
    """A tree exercising Puppet-shaped rendering end to end (parsing,
    ``convert_to: Sensitive``, then ``--render-as``). ``big`` is large
    enough (20 000 20-char strings) to overflow an OS pipe buffer, so a
    reader that closes early forces a real ``BrokenPipeError``.
    """
    common_yaml = (
        "str: one\n"
        "nil: ~\n"
        "nested:\n"
        "  b: 2\n"
        "  a:\n"
        "    - 1\n"
        "    - z: 1\n"
        "      y: 2\n"
        'unicode: "café \u2603"\n'
        "nan: .nan\n"
        "secret: hunter2\n"
        "lookup_options:\n"
        "  secret: { convert_to: Sensitive }\n"
        "big:\n"
    ) + "".join("  - {}\n".format("x" * 20) for _ in range(20000))
    return make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": common_yaml},
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
    rc = main(["-c", str(render_root / "hiera.yaml"), "--render-as", fmt, key])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert out == _RENDER_AS_EXPECTED[key][idx]


def test_default_render_is_yaml(render_root, capsys):
    rc = main(["-c", str(render_root / "hiera.yaml"), "str"])
    assert rc == 0
    assert capsys.readouterr().out == "--- one\n"


def test_render_as_is_case_insensitive(render_root, capsys):
    rc = main(["-c", str(render_root / "hiera.yaml"), "--render-as", "JSON", "str"])
    assert rc == 0
    assert capsys.readouterr().out == '"one"\n'


def test_unknown_render_format_exit_2(render_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(["-c", str(render_root / "hiera.yaml"), "--render-as", "foo", "str"])
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == "Unknown rendering format 'foo'"


def test_json_nonfinite_exit_2(render_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(["-c", str(render_root / "hiera.yaml"), "--render-as", "json", "nan"])
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
            "-c",
            str(render_root / "hiera.yaml"),
            "--default",
            "fallback",
            "--render-as",
            fmt,
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == expected


def test_utf8_on_cp1252_stdout(render_root, monkeypatch):
    import io

    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)
    rc = main(["-c", str(render_root / "hiera.yaml"), "--render-as", "s", "unicode"])
    wrapper.flush()
    assert rc == 0
    assert buf.getvalue() == "café ☃\n".encode("utf-8")


def test_utf8_on_cp1252_pipe(render_root):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyera",
            "-c",
            str(render_root / "hiera.yaml"),
            "--render-as",
            "s",
            "unicode",
        ],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": src},
        timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == "café ☃\n".encode("utf-8")


def test_closed_stdout_exits_2_quietly(render_root, tmp_path):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hyera",
            "-c",
            str(render_root / "hiera.yaml"),
            "--render-as",
            "json",
            "big",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": src},
    )
    try:
        proc.stdout.read(1)
        proc.stdout.close()
        err = proc.stderr.read()
        rc = proc.wait(timeout=60)
    finally:
        proc.stderr.close()
    assert err == b""
    assert rc == 2


@pytest.mark.parametrize("flag", ["-o", "--output"])
def test_output_flag_removed(flag, hiera_root):
    with pytest.raises(SystemExit) as exc:
        main(["app::name", "-c", str(hiera_root / "hiera.yaml"), flag, "json"])
    assert exc.value.code == 2


def test_sensitive_redacted_in_output(values_root, capsys):
    """A ``Sensitive``-converted value is redacted in every output format,
    including the CLI's default (``s``-like) output, and never the wrapped
    secret.
    """
    rc = main(["-c", str(values_root / "hiera.yaml"), "--render-as", "s", "secret"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert out.strip() == "Sensitive [value redacted]"

    rc = main(["-c", str(values_root / "hiera.yaml"), "--render-as", "json", "secret"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert json.loads(out) == "Sensitive [value redacted]"


def test_explicit_merge_first_overrides_lookup_options(mergefirst_root, capsys):
    base_args = [
        "classes",
        "-c",
        str(mergefirst_root / "hiera.yaml"),
        "-s",
        "os_family=RedHat",
        "--render-as",
        "json",
    ]

    assert main(base_args) == 0
    assert json.loads(capsys.readouterr().out) == ["redhat", "base"]

    assert main(base_args + ["--merge", "first"]) == 0
    assert json.loads(capsys.readouterr().out) == ["redhat"]

    assert main(base_args + ["--merge", "unique"]) == 0
    assert json.loads(capsys.readouterr().out) == ["redhat", "base"]


def test_mcp_stdio_serves_lookup(hiera_root):
    # HYERA_MCP=stdio runs the CLI as an MCP server exposing one tool. Both
    # the tool name and the initialize response's serverInfo.name come from
    # duho's root tool-name resolution (Lookup._parsername_ -- see R7a),
    # which is "hyera", not the command class's own name "Lookup".
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "hyera",
                "arguments": {
                    "key": "app::name",
                    "config": str(hiera_root / "hiera.yaml"),
                    "scope": ["environment=production"],
                    "render_as": "s",
                },
            },
        },
    ]
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, "HYERA_MCP": "stdio", "PYTHONPATH": src},
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    replies = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "hyera"
    assert replies[2]["result"]["content"] == [{"type": "text", "text": "myapp\n"}]


def test_mcp_trigger_follows_declared_name_not_argv0(hiera_root, monkeypatch, capsys):
    # The MCP trigger env var name must come from Lookup's own declared
    # `_parsername_`, not from sys.argv[0]'s stem -- otherwise embedding
    # hyera's CLI in another script (or running it as `python -m hyera.cli`)
    # silently changes which env var launches the MCP server, contradicting
    # the documented HYERA_MCP contract.
    monkeypatch.setattr(sys, "argv", ["/x/cli.py"])
    monkeypatch.setenv("HYERA_MCP", "bogus")

    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_mcp_unknown_transport_exits_2(hiera_root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hyera"])
    monkeypatch.setenv("HYERA_MCP", "bogus")

    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_lookup_found(hiera_root, capsys):
    rc = main(
        [
            "app::name",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "-s",
            "environment=production",
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "myapp"


def test_lookup_missing_exit_1(hiera_root):
    rc = main(["nope::key", "-c", str(hiera_root / "hiera.yaml")])
    assert rc == 1


def test_lookup_missing_with_default_exit_0(hiera_root, capsys):
    rc = main(
        [
            "nope::key",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "--default",
            "fallback",
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "fallback"


def test_bad_config_exit_2(tmp_path):
    rc = main(["k", "-c", str(tmp_path / "does-not-exist.yaml")])
    assert rc == 2


def test_codedir_flag(make_tree, capsys):
    root = make_tree(":backends: [yaml]\n:hierarchy: [common]\n", raw=True)
    codedir = root / "code"
    hieradata = codedir / "environments" / "production" / "hieradata"
    hieradata.mkdir(parents=True)
    (hieradata / "common.yaml").write_text("k: v\n", encoding="utf-8")
    rc = main(
        [
            "k",
            "-c",
            str(root / "hiera.yaml"),
            "--codedir",
            str(codedir),
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "v"


def test_bad_lookup_options_merge_exits_2(make_tree, caplog):
    # A lookup_options `merge:` hash with no `strategy` key is a MergeError,
    # not caught anywhere but the CLI's own catch-all -- exit 2, not a crash.
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "common.yaml"}]},
        files={
            "data/common.yaml": "k: v\nlookup_options: {k: {merge: {merge: unique}}}\n"
        },
    )
    rc = main(["k", "-c", str(root / "hiera.yaml")])
    assert rc == 2
    assert "strategy" in _error_records(caplog)[-1].getMessage()


def test_merge_array_alias_extension(hiera_root, capsys):
    # Non-Puppet extension; this test goes with the feature: `--merge array`
    # is our own legacy alias for `unique`.
    rc = main(
        [
            "classes",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "-s",
            "environment=production",
            "--merge",
            "array",
            "--render-as",
            "json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == ["prod", "web", "base"]


def test_unique_merge_json_output(hiera_root, capsys):
    rc = main(
        [
            "classes",
            "-c",
            str(hiera_root / "hiera.yaml"),
            "-s",
            "environment=production",
            "--merge",
            "unique",
            "--render-as",
            "json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == ["prod", "web", "base"]


def test_invalid_scope_exit_2(hiera_root):
    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml"), "-s", "noequals"])
    assert rc == 2


def test_dotted_scope_name_exit_2(hiera_root):
    # A Puppet variable name cannot contain '.', so a dotted --scope name is
    # rejected outright rather than stored as a flat key nothing can read
    # (there is no flat-dotted-context-key fallback to store it under).
    rc = main(
        ["app::name", "-c", str(hiera_root / "hiera.yaml"), "-s", "trusted.certname=x"]
    )
    assert rc == 2


def test_module_entrypoint_smoke(monkeypatch):
    # `python -m hyera` wires through to cli.main.
    monkeypatch.setattr(sys, "argv", ["hyera", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("hyera", run_name="__main__")
    assert exc.value.code == 0  # --help exits 0


def test_config_error_exit_2_one_line(make_tree, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    root = make_tree(
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data\n"
        "hierarchy:\n"
        "  - {name: c, path: common.yaml}\n",
        raw=True,
    )

    with caplog.at_level(logging.ERROR):
        rc = main(["k", "-c", str(root / "hiera.yaml")])

    assert rc == 2
    records = _error_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith("Lookup of key 'k' failed: (")
    assert "hiera.yaml" in message
    assert "\n" not in message
    assert records[0].exc_info is None


def test_data_parse_error_exit_2_names_key_and_file(make_tree, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    root = make_tree(
        {
            "hierarchy": [
                {"name": "c", "path": "common.yaml"},
                {"name": "o", "path": "other.yaml"},
            ],
        },
        files={
            "data/common.yaml": "good: yes\n",
            "data/other.yaml": "k: [unclosed\nz: 2\n",
        },
    )

    with caplog.at_level(logging.ERROR):
        rc = main(["good", "-c", str(root / "hiera.yaml")])

    assert rc == 2
    message = _error_records(caplog)[-1].getMessage()
    assert "Lookup of key 'good' failed: Unable to parse (" in message
    assert "other.yaml" in message


def test_directory_config_exit_2(make_tree, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={"data/one.yaml": "k: v\n"},
    )

    with caplog.at_level(logging.ERROR):
        rc = main(["k", "-c", str(root / "data")])

    assert rc == 2
    assert "Is a directory" in _error_records(caplog)[-1].getMessage()


@pytest.mark.parametrize(
    "exc_type", [RecursionError, TypeError, ValueError, AttributeError]
)
def test_unexpected_exception_exit_2(hiera_root, monkeypatch, caplog, exc_type):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)

    def _raise(self, *a, **kw):
        raise exc_type("boom")

    monkeypatch.setattr(hyera.Hiera, "lookup", _raise)

    with caplog.at_level(logging.ERROR):
        rc = main(["k", "-c", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    records = _error_records(caplog)
    assert len(records) == 1
    assert records[0].getMessage() == "Lookup of key 'k' failed: {}: boom".format(
        exc_type.__name__
    )
    assert records[0].exc_info is None


def test_traceback_only_with_verbose(hiera_root, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)

    def _raise(self, *a, **kw):
        raise RecursionError("boom")

    monkeypatch.setattr(hyera.Hiera, "lookup", _raise)

    with caplog.at_level(logging.ERROR):
        rc = main(["k", "-c", str(hiera_root / "hiera.yaml"), "-v"])

    assert rc == 2
    assert _error_records(caplog)[-1].exc_info is not None


def test_render_error_exit_2(hiera_root, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)

    def _raise_type_error(self, value, **kw):
        raise TypeError("x")

    monkeypatch.setattr("hyera._render.JSONRender.dumps", _raise_type_error)

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "app::name",
                "-c",
                str(hiera_root / "hiera.yaml"),
                "-s",
                "environment=production",
                "--render-as",
                "json",
            ]
        )

    assert rc == 2
    assert "Cannot render the value of key" in _error_records(caplog)[-1].getMessage()


def test_plain_keyerror_is_not_a_miss(hiera_root, monkeypatch):
    def _raise(self, *a, **kw):
        raise KeyError("x")

    monkeypatch.setattr(hyera.Hiera, "lookup", _raise)

    rc = main(["k", "-c", str(hiera_root / "hiera.yaml")])

    assert rc == 2


def test_recursive_data_exits_2_without_traceback(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={"data/one.yaml": "a: \"%{hiera('b')}\"\nb: \"%{hiera('a')}\"\n"},
    )

    proc = subprocess.run(
        [sys.executable, "-m", "hyera", "a", "-c", str(root / "hiera.yaml")],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 2
    assert "Traceback" not in proc.stderr
    assert len(proc.stderr.strip().splitlines()) == 1


def test_hocon_duration_survives_yaml_output(make_tree, capsys):
    pytest.importorskip("pyhocon")
    root = make_tree(
        {
            "defaults": {"data_hash": "hocon_data"},
            "hierarchy": [{"name": "c", "path": "common.conf"}],
        },
        files={"data/common.conf": "dur = 10s\n"},
    )
    rc = main(["dur", "-c", str(root / "hiera.yaml"), "--render-as", "yaml"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "--- 10s"
