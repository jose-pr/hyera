"""CLI behavior and exit codes (unattended-friendly)."""

import contextlib
import io
import json
import logging
import os
import runpy
import shlex
import subprocess
import sys

import pytest
import yaml

duho = pytest.importorskip("duho")

import hyera  # noqa: E402
from hyera.cli import main  # noqa: E402

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(hyera.__file__)))


def _error_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "hyera.cli" and r.levelno == logging.ERROR
    ]


@pytest.fixture
def hiera_root(make_tree):
    """``conftest.hiera_root``, plus a ``facts.yaml`` -- the CLI now
    requires ``--facts`` for every lookup (an empty facts mapping is
    Puppet's own "No facts available" error), so every CLI test needs a
    real facts file even when the data itself never reads a fact.
    Overrides (shadows) the shared ``conftest.py`` fixture of the same
    name for this module only; other test files keep the plain one.
    """
    return make_tree(
        {
            "version": 5,
            "defaults": {"data_hash": "yaml_data", "datadir": "data"},
            "hierarchy": [
                {"name": "Per-environment", "path": "environments/%{environment}.yaml"},
                {"name": "Modules", "globs": ["modules/*.yaml"]},
                {"name": "Common", "path": "common.yaml"},
            ],
        },
        files={
            "data/common.yaml": """\
                ---
                app::name: myapp
                greeting: "hello %{environment}"
                ntp::servers:
                  - a.pool.ntp.org
                classes:
                  - base
                db:
                  host: localhost
                  port: 5432
                alias_target: "%{alias('app::name')}"
                alias_list: "%{alias('ntp::servers')}"
                literal_pct: "100%{literal('%')} done"
                port_msg: "listening on %{hiera('db.port')}"
                """,
            "data/environments/production.yaml": """\
                ---
                ntp::servers:
                  - prod.pool.ntp.org
                classes:
                  - prod
                db:
                  host: db.prod.internal
                lookup_greeting: "%{hiera('app::name')} in prod"
                """,
            "data/modules/web.yaml": """\
                ---
                classes:
                  - web
                """,
        },
        facts={"role": "web"},
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
        facts={"role": "web"},
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


def test_utf8_on_cp1252_stdout(render_root, monkeypatch):
    import io

    buf = io.BytesIO()
    wrapper = io.TextIOWrapper(buf, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", wrapper)
    rc = main(
        [
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "--render-as",
            "s",
            "unicode",
        ]
    )
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
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
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
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
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


@pytest.mark.parametrize(
    "error",
    [
        BrokenPipeError(32, "Broken pipe"),
        OSError(22, "Invalid argument"),
        OSError(28, "No space left on device"),
    ],
    ids=["EPIPE", "EINVAL", "ENOSPC"],
)
def test_stdout_write_failure_exits_2_and_silences_stdout(
    error, monkeypatch, render_root, caplog
):
    # The in-process counterpart to test_closed_stdout_exits_2_quietly
    # above, which needs a real OS pipe closed from the reader side.
    # _silence_stdout is mocked out: its real dup2() would redirect this
    # test process's own stdout to the null device for the rest of the run.
    import hyera.cli as cli

    def refuse(text):
        raise error

    monkeypatch.setattr(cli, "_emit", refuse)
    silenced = []
    monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))
    with caplog.at_level(logging.ERROR):
        rc = cli.main(
            [
                "--hiera_config",
                str(render_root / "hiera.yaml"),
                "--facts",
                str(render_root / "facts.yaml"),
                "--render-as",
                "json",
                "str",
            ]
        )
    assert rc == 2
    assert silenced == [True]
    assert _error_records(caplog) == []


def test_reader_gone_before_the_first_write_exits_2_quietly(render_root):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(render_root / "hiera.yaml"),
            "--facts",
            str(render_root / "facts.yaml"),
            "str",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": _SRC},
    )
    try:
        proc.stdout.close()
        err = proc.stderr.read()
        rc = proc.wait(timeout=60)
    finally:
        proc.stderr.close()
    assert err == b""
    assert rc == 2


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="needs /dev/full")
def test_full_device_stdout_exits_2_quietly(render_root):
    with open("/dev/full", "wb") as full:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyera",
                "--hiera_config",
                str(render_root / "hiera.yaml"),
                "--facts",
                str(render_root / "facts.yaml"),
                "str",
            ],
            stdout=full,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": _SRC},
            timeout=60,
        )
    assert proc.stderr == b""
    assert proc.returncode == 2


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


def test_explicit_merge_first_overrides_lookup_options(mergefirst_root, capsys):
    base_args = [
        "classes",
        "--hiera_config",
        str(mergefirst_root / "hiera.yaml"),
        "--facts",
        str(mergefirst_root / "facts.yaml"),
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
    # duho's root tool-name resolution (Lookup._parsername_),
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
                    "keys": ["app::name"],
                    "hiera_config": str(hiera_root / "hiera.yaml"),
                    "facts": str(hiera_root / "facts.yaml"),
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

    rc = main(["app::name", "--hiera_config", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_mcp_unknown_transport_exits_2(hiera_root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["hyera"])
    monkeypatch.setenv("HYERA_MCP", "bogus")

    rc = main(["app::name", "--hiera_config", str(hiera_root / "hiera.yaml")])

    assert rc == 2
    assert "unsupported MCP transport" in capsys.readouterr().err


def test_lookup_found(hiera_root, capsys):
    rc = main(
        [
            "app::name",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
            "-s",
            "environment=production",
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "myapp"


def test_lookup_missing_exit_1(hiera_root):
    rc = main(
        [
            "nope::key",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
        ]
    )
    assert rc == 1


def test_lookup_missing_with_default_exit_0(hiera_root, capsys):
    rc = main(
        [
            "nope::key",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
            "--default",
            "fallback",
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == "fallback"


def test_bad_config_exit_2(tmp_path):
    (tmp_path / "facts.yaml").write_text("role: web\n", encoding="utf-8")
    rc = main(
        [
            "k",
            "--hiera_config",
            str(tmp_path / "does-not-exist.yaml"),
            "--facts",
            str(tmp_path / "facts.yaml"),
        ]
    )
    assert rc == 2


def test_codedir_flag(make_tree, capsys):
    root = make_tree(
        ":backends: [yaml]\n:hierarchy: [common]\n",
        raw=True,
        facts={"role": "web"},
    )
    codedir = root / "code"
    hieradata = codedir / "environments" / "production" / "hieradata"
    hieradata.mkdir(parents=True)
    (hieradata / "common.yaml").write_text("k: v\n", encoding="utf-8")
    rc = main(
        [
            "k",
            "--hiera_config",
            str(root / "hiera.yaml"),
            "--facts",
            str(root / "facts.yaml"),
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
        facts={"role": "web"},
    )
    rc = main(
        [
            "k",
            "--hiera_config",
            str(root / "hiera.yaml"),
            "--facts",
            str(root / "facts.yaml"),
        ]
    )
    assert rc == 2
    assert "strategy" in _error_records(caplog)[-1].getMessage()


def test_unique_merge_json_output(hiera_root, capsys):
    rc = main(
        [
            "classes",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
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
    rc = main(
        [
            "app::name",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
            "-s",
            "noequals",
        ]
    )
    assert rc == 2


def test_module_entrypoint_smoke(monkeypatch, hiera_root, capsys):
    # `python -m hyera` wires through to cli.main and actually runs a real
    # lookup (not just `--help`, which never reaches the lookup path at
    # all) -- this run counts toward branch coverage, in-process.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hyera",
            "--render-as",
            "json",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
            "-s",
            "environment=production",
            "app::name",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("hyera", run_name="__main__")
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out) == "myapp"


def test_version_names_hyera(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "hyera {}".format(hyera.__version__)


def test_config_error_exit_2_one_line(make_tree, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    root = make_tree(
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data\n"
        "hierarchy:\n"
        "  - {name: c, path: common.yaml}\n",
        raw=True,
        facts={"role": "web"},
    )

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "k",
                "--hiera_config",
                str(root / "hiera.yaml"),
                "--facts",
                str(root / "facts.yaml"),
            ]
        )

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
        facts={"role": "web"},
    )

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "good",
                "--hiera_config",
                str(root / "hiera.yaml"),
                "--facts",
                str(root / "facts.yaml"),
            ]
        )

    assert rc == 2
    message = _error_records(caplog)[-1].getMessage()
    assert "Lookup of key 'good' failed: Unable to parse (" in message
    assert "other.yaml" in message


def test_directory_config_exit_2(make_tree, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={"data/one.yaml": "k: v\n"},
        facts={"role": "web"},
    )

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "k",
                "--hiera_config",
                str(root / "data"),
                "--facts",
                str(root / "facts.yaml"),
            ]
        )

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
        rc = main(
            [
                "k",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
            ]
        )

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
        rc = main(
            [
                "k",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
                "-v",
            ]
        )

    assert rc == 2
    assert _error_records(caplog)[-1].exc_info is not None


def test_render_error_exit_2(hiera_root, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)

    def _raise_type_error(self, value, **kw):
        raise TypeError("x")

    monkeypatch.setattr("hyera._output.render.JSONRender.dumps", _raise_type_error)

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "app::name",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
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

    rc = main(
        [
            "k",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
        ]
    )

    assert rc == 2


def test_recursive_data_exits_2_without_traceback(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "one", "path": "one.yaml"}]},
        files={"data/one.yaml": "a: \"%{hiera('b')}\"\nb: \"%{hiera('a')}\"\n"},
        facts={"role": "web"},
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyera",
            "a",
            "--hiera_config",
            str(root / "hiera.yaml"),
            "--facts",
            str(root / "facts.yaml"),
        ],
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


# ---------------------------------------------------------------------------
# puppet lookup's flag set: merge validation and deep-merge options, --type,
# scope/facts/node, layers (--environment*/--modulepath*), --strict,
# --explain/--explain-options, and the flags this CLI removed outright.
# ---------------------------------------------------------------------------


@pytest.fixture
def flags_root(make_tree):
    """A two-level hierarchy (``high`` over ``low``) plus a staging
    environment, a ``mymod`` module reachable from two different module
    roots, and three alternate facts files -- everything the flag tests
    below need from one tree."""
    root = make_tree(
        {
            "hierarchy": [
                {"name": "high", "path": "high.yaml"},
                {"name": "low", "path": "low.yaml"},
            ]
        },
        files={
            "data/high.yaml": """\
                h:
                  items: [d, "--b"]
                  rows:
                    - x: 1
                v: "%{role}-%{facts.os.family}|%{environment}|%{server_facts.environment}|[%{trusted.certname}]|%{server_facts.serverversion}"
                u: "[%{nosuch}]"
                str: one
                int0: 0
                sv: "%{n}|%{b}|%{o.x}|%{l}"
                """,
            "data/low.yaml": """\
                h:
                  items: [b, a, c]
                  rows:
                    - y: 2
                """,
            "empty_facts.yaml": "{}\n",
            "partial_facts.yaml": "fqdn: a.example.com\n",
            "environments/staging/hiera.yaml": """\
                version: 5
                defaults: {datadir: data, data_hash: yaml_data}
                hierarchy: [{name: e, path: env.yaml}]
                """,
            "environments/staging/data/env.yaml": "envkey: fromstaging\n",
            "modules/mymod/hiera.yaml": """\
                version: 5
                defaults: {datadir: data, data_hash: yaml_data}
                hierarchy: [{name: m, path: common.yaml}]
                """,
            "modules/mymod/data/common.yaml": "mymod::k: frommodule\n",
            "othermods/mymod/hiera.yaml": """\
                version: 5
                defaults: {datadir: data, data_hash: yaml_data}
                hierarchy: [{name: m, path: common.yaml}]
                """,
            "othermods/mymod/data/common.yaml": "mymod::k: fromother\n",
        },
        facts={"role": "web", "os": {"family": "RedHat"}},
    )
    (root / "empty_environments").mkdir(parents=True, exist_ok=True)
    return root


def _flags_argv(flags_root, *extra):
    return [
        "--hiera_config",
        str(flags_root / "hiera.yaml"),
        "--facts",
        str(flags_root / "facts.yaml"),
        "--node",
        "web01.example.com",
    ] + list(extra)


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


def test_first_found_of_several_keys(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "nope", "str", "int0", "--render-as", "s"))
    assert rc == 0
    assert capsys.readouterr().out == "one\n"


def test_default_with_several_keys(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "nope", "nope2", "--default", "x"))
    assert rc == 0
    assert capsys.readouterr().out == "--- x\n"


def test_default_with_unique_merge(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "--merge", "unique", "--default", "x", "nope"))
    assert rc == 0
    assert capsys.readouterr().out == "--- x\n"


def test_type_asserts_found_value(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "str", "--type", "Integer"))
    assert rc == 2
    assert (
        "Found value has wrong type, expects an Integer value, got String"
        in _error_records(caplog)[-1].getMessage()
    )


def test_type_asserts_default(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            _flags_argv(flags_root, "nope", "--type", "Integer", "--default", "3")
        )
    assert rc == 2
    assert (
        "Default value has wrong type, expects an Integer value, got String"
        in _error_records(caplog)[-1].getMessage()
    )


def test_type_syntax_error_before_lookup(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "nope", "--type", "Integer["))
    assert rc == 2  # not 1: a syntax error is never a plain miss
    assert "Syntax error at end of input" in _error_records(caplog)[-1].getMessage()


def test_no_keys(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root))
    assert rc == 2
    assert _error_records(caplog)[-1].getMessage() == "No keys were given to lookup."


def test_scope_variables(flags_root, capsys):
    rc = main(_flags_argv(flags_root, "v", "--render-as", "s"))
    assert rc == 0
    assert capsys.readouterr().out == "web-RedHat|production|production|[]|8.10.0\n"


def test_environment_flag_sets_scope(flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root,
            "--environment",
            "staging",
            "--environmentpath",
            str(flags_root / "environments"),
            "v",
            "--render-as",
            "s",
        )
    )
    assert rc == 0
    assert capsys.readouterr().out == "web-RedHat|staging|staging|[]|8.10.0\n"


def test_environment_layer(flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root,
            "--environment",
            "staging",
            "--environmentpath",
            str(flags_root / "environments"),
            "envkey",
            "--render-as",
            "s",
        )
    )
    assert rc == 0
    assert capsys.readouterr().out == "fromstaging\n"


def test_missing_environment_exit_2(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            _flags_argv(
                flags_root,
                "--environment",
                "nosuch",
                "--environmentpath",
                str(flags_root / "environments"),
                "str",
            )
        )
    assert rc == 2
    message = _error_records(caplog)[-1].getMessage()
    assert "Could not find a directory environment named 'nosuch'" in message
    assert "Does the directory exist?" in message


def test_missing_production_environment_is_skipped(flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root,
            "--environmentpath",
            str(flags_root / "empty_environments"),
            "str",
            "--render-as",
            "s",
        )
    )
    assert rc == 0
    assert capsys.readouterr().out == "one\n"


def test_module_layer_from_basemodulepath(flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root,
            "--basemodulepath",
            str(flags_root / "modules"),
            "mymod::k",
            "--render-as",
            "s",
        )
    )
    assert rc == 0
    assert capsys.readouterr().out == "frommodule\n"


def test_modulepath_overrides_basemodulepath(flags_root, capsys):
    rc = main(
        _flags_argv(
            flags_root,
            "--basemodulepath",
            str(flags_root / "modules"),
            "--modulepath",
            str(flags_root / "othermods"),
            "mymod::k",
            "--render-as",
            "s",
        )
    )
    assert rc == 0
    assert capsys.readouterr().out == "fromother\n"


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


def test_emit_falls_back_to_plain_write_without_a_buffer_attr():
    # _emit's own fallback for a stdout with no `.buffer` (a StringIO
    # under contextlib.redirect_stdout, as the conformance harness uses,
    # or a genuine one here) -- real sys.stdout (even under pytest's
    # capsys) always has one, so no in-process CLI test exercises this any
    # other way; the subprocess-based BrokenPipeError test above covers
    # the real-console path, and test_broken_pipe_while_emitting_exits_2_
    # and_silences_stdout covers the same except-branch in-process (with
    # _emit mocked) so it is actually measured.
    from hyera.cli import _emit, _silence_stdout

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _emit("hello")
    assert buf.getvalue() == "hello\n"

    # _silence_stdout's own best-effort no-op: a StringIO has no real file
    # descriptor, so `.fileno()` raises `io.UnsupportedOperation`.
    with contextlib.redirect_stdout(io.StringIO()):
        _silence_stdout()  # must not raise


def test_main_without_duho_prints_hint_and_exits_2(monkeypatch, capsys):
    # duho is a module-level name, None only when the "cli" extra's own
    # import failed at module load time (not reproducible by breaking the
    # import after the fact) -- the downstream check this guards is
    # exercised directly instead.
    import hyera.cli as cli

    monkeypatch.setattr(cli, "duho", None)
    rc = cli.main(["k"])
    assert rc == 2
    assert 'pip install "hyera[cli]"' in capsys.readouterr().err


def test_cli_module_reimported_without_duho_installed(monkeypatch, capsys):
    # The module-level `import duho` / `except ModuleNotFoundError` (only
    # this test forces a fresh import of hyera.cli itself) and the
    # `if duho is not None:` guard around the whole `Lookup` class body:
    # setting sys.modules["duho"] = None makes the next `import duho`
    # raise ModuleNotFoundError(name="duho") exactly as a genuinely
    # missing package would, and dropping hyera.cli from sys.modules
    # forces cli.py's module body -- including that import and the class
    # guard -- to run again. hyera.cli has no import-time side effect
    # beyond defining names (backend/format registration lives in
    # hyera.backends, untouched here), so reloading it is safe.
    #
    # monkeypatch.undo() runs explicitly (rather than waiting for the
    # fixture's own automatic teardown) before the final restoring
    # reimport below: sys.modules["duho"] must already be back to the
    # real module at that point, or the restored hyera.cli would itself
    # be reloaded with duho still faked absent.
    import sys as _sys_mod

    import hyera.cli

    monkeypatch.setitem(_sys_mod.modules, "duho", None)
    monkeypatch.delitem(_sys_mod.modules, "hyera.cli")
    try:
        reimported = __import__("hyera.cli", fromlist=["cli"])
        assert reimported.duho is None
        assert not hasattr(reimported, "Lookup")
        assert "Lookup" not in reimported.__all__
        rc = reimported.main(["k"])
        assert rc == 2
        assert 'pip install "hyera[cli]"' in capsys.readouterr().err
    finally:
        monkeypatch.undo()
        _sys_mod.modules.pop("hyera.cli", None)
        __import__("hyera.cli", fromlist=["cli"])


def test_cli_reraises_a_duho_internal_import_error(monkeypatch):
    # The `if _e.name != "duho": raise` half of the same except clause:
    # a `duho` present but broken in some other way (here, missing its
    # own `logging` submodule) must not be swallowed as "the cli extra
    # isn't installed" -- only a ModuleNotFoundError naming "duho" itself
    # means that. A bare ModuleType stub named "duho" with no `__path__`
    # makes `import duho` succeed (it's already in sys.modules) but
    # `import duho.logging` fail with ModuleNotFoundError(name=
    # "duho.logging"), reproducing exactly that shape without needing a
    # genuinely broken duho installation -- but only once the real
    # `duho.logging`'s own already-cached sys.modules entry is also
    # removed, or `import duho.logging` would just return that cached
    # submodule without ever consulting the (now-stubbed) `duho` package.
    import sys as _sys_mod
    import types

    import hyera.cli

    monkeypatch.setitem(_sys_mod.modules, "duho", types.ModuleType("duho"))
    monkeypatch.delitem(_sys_mod.modules, "duho.logging", raising=False)
    monkeypatch.delitem(_sys_mod.modules, "hyera.cli")
    try:
        with pytest.raises(ModuleNotFoundError, match="duho.logging"):
            __import__("hyera.cli", fromlist=["cli"])
    finally:
        monkeypatch.undo()
        _sys_mod.modules.pop("hyera.cli", None)
        __import__("hyera.cli", fromlist=["cli"])


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


def test_empty_facts_file_exit_2(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "--hiera_config",
                str(flags_root / "hiera.yaml"),
                "--facts",
                str(flags_root / "empty_facts.yaml"),
                "--node",
                "web01.example.com",
                "str",
            ]
        )
    assert rc == 2
    assert (
        _error_records(caplog)[-1].getMessage()
        == "No facts available for target node: web01.example.com"
    )


@pytest.mark.parametrize("target", ["nosuch.yaml", "."])
def test_unreadable_facts_path_exits_2_with_one_line(target, flags_root, caplog, capfd):
    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "--hiera_config",
                str(flags_root / "hiera.yaml"),
                "--facts",
                str(flags_root / target),
                "str",
            ]
        )
    captured = capfd.readouterr()
    assert rc == 2
    assert "Traceback" not in captured.err
    assert len(_error_records(caplog)) == 1
    assert "\n" not in _error_records(caplog)[0].getMessage()


def test_facts_file_error_exit_2(flags_root, caplog):
    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "--hiera_config",
                str(flags_root / "hiera.yaml"),
                "--facts",
                str(flags_root / "partial_facts.yaml"),
                "--node",
                "web01.example.com",
                "str",
            ]
        )
    assert rc == 2
    assert "they must all be overridden" in _error_records(caplog)[-1].getMessage()


def test_hiera_config_defaults_to_cwd_file(flags_root, monkeypatch, capsys):
    monkeypatch.chdir(flags_root)
    rc = main(
        [
            "--facts",
            str(flags_root / "facts.yaml"),
            "--node",
            "n",
            "str",
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "one\n"


def test_hiera_config_default_falls_back_to_puppet_default(
    make_tree, monkeypatch, capsys
):
    root = make_tree(
        {"hierarchy": [{"name": "common", "path": "common.yaml"}]},
        files={"data/common.yaml": "k: v\n"},
        facts={"role": "web"},
    )
    monkeypatch.chdir(root)
    rc = main(["--facts", "facts.yaml", "--node", "n", "k", "--render-as", "s"])
    assert rc == 0
    assert capsys.readouterr().out == "v\n"


def test_hiera_config_default_with_no_hiera_yaml_uses_builtin_default(
    tmp_path, monkeypatch
):
    # Distinct from the case above (whose tmp tree has its own real
    # hiera.yaml, found by the "default_path exists" branch): with no
    # --hiera_config and no hiera.yaml in the cwd at all, base_path falls
    # back to the cwd itself for Hiera's own Puppet-default config, rather
    # than crashing on a missing file.
    (tmp_path / "facts.yaml").write_text("role: web\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    rc = main(["--facts", "facts.yaml", "--node", "n", "nosuchkey", "--render-as", "s"])
    assert rc == 1


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


# ---------------------------------------------------------------------------
# Puppet's own log levels, a silent miss, and plain-text help.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _reset_hyera_logger():
    yield
    logging.getLogger("hyera").setLevel(logging.NOTSET)


def _hyera_records_at_or_above(caplog, level):
    return [
        r for r in caplog.records if r.name.startswith("hyera") and r.levelno >= level
    ]


def test_plain_miss_prints_nothing(hiera_root, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        rc = main(
            [
                "nope::key",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
            ]
        )
    assert rc == 1
    assert capsys.readouterr().out == ""
    assert not _hyera_records_at_or_above(caplog, logging.WARNING)


def test_plain_miss_subprocess_is_silent(hiera_root):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
            "nope::key",
        ],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": src},
        timeout=60,
    )
    assert proc.returncode == 1
    assert proc.stdout == b""
    assert proc.stderr == b""


@pytest.mark.parametrize(
    "flags,expected_level",
    [
        ([], logging.WARNING),
        (["-v"], logging.INFO),
        (["-v", "-v"], logging.DEBUG),
        (["-d"], logging.DEBUG),
        (["-q"], logging.ERROR),
        (["-q", "-q"], logging.CRITICAL),
    ],
    ids=["none", "-v", "-vv", "-d", "-q", "-qq"],
)
def test_verbosity_levels(flags, expected_level, hiera_root, _reset_hyera_logger):
    rc = main(
        [
            "nope::key",
            "--hiera_config",
            str(hiera_root / "hiera.yaml"),
            "--facts",
            str(hiera_root / "facts.yaml"),
        ]
        + flags
    )
    assert rc == 1
    assert logging.getLogger("hyera").level == expected_level


def test_debug_logs_the_miss(hiera_root, caplog):
    with caplog.at_level(logging.DEBUG):
        rc = main(
            [
                "nope::key",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
                "-d",
            ]
        )
    assert rc == 1
    assert any(
        "did not find a value for the name 'nope::key'" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("extra", [[], ["-q"]], ids=["default", "-q"])
def test_warnings_follow_quiet(extra, flags_root, caplog):
    with caplog.at_level(logging.DEBUG):
        rc = main(_flags_argv(flags_root, "--strict", "warning", "u", *extra))
    assert rc == 0
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Undefined variable 'nosuch'" in r.getMessage()
    ]
    if extra:
        assert not warnings
    else:
        assert warnings


def test_traceback_with_debug(hiera_root, monkeypatch, caplog):
    monkeypatch.delenv("DUHO_TRACEBACK", raising=False)

    def _raise(self, *a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(hyera.Hiera, "lookup", _raise)

    with caplog.at_level(logging.ERROR):
        rc = main(
            [
                "k",
                "--hiera_config",
                str(hiera_root / "hiera.yaml"),
                "--facts",
                str(hiera_root / "facts.yaml"),
                "-d",
            ]
        )

    assert rc == 2
    assert _error_records(caplog)[-1].exc_info is not None


def test_help_is_plain_text(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: hyera")
    assert "lookup_options" in out
    assert "``" not in out


def test_python_m_hyera_lookup(flags_root):
    src = os.path.join(os.path.dirname(__file__), os.pardir, "src")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyera",
            "--hiera_config",
            str(flags_root / "hiera.yaml"),
            "--facts",
            str(flags_root / "facts.yaml"),
            "--node",
            "web01.example.com",
            "--render-as",
            "s",
            "str",
        ],
        capture_output=True,
        env={**os.environ, "PYTHONPATH": src},
        timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout == b"one\n"


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


def test_verbose_usage_failure_logs_one_line_without_a_traceback(flags_root, capfd):
    rc = main(_flags_argv(flags_root, "-v"))
    err = capfd.readouterr().err
    assert rc == 2
    assert "NoneType" not in err
    assert "Traceback" not in err


def test_backend_timeout_is_an_ordinary_error_exit_2(
    flags_root, monkeypatch, caplog, capfd
):
    def hang(*args, **kwargs):
        raise hyera.BackendTimeoutError("the data hook timed out")

    monkeypatch.setattr(hyera.Hiera, "lookup", hang)
    with caplog.at_level(logging.ERROR):
        rc = main(_flags_argv(flags_root, "h"))
    assert rc == 2
    assert len(_error_records(caplog)) == 1
    assert "timed out" in _error_records(caplog)[0].getMessage()
    assert "Traceback" not in capfd.readouterr().err


def test_keyboard_interrupt_exits_130_without_a_traceback(
    flags_root, monkeypatch, capfd
):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(hyera.Hiera, "lookup", interrupt)
    rc = main(_flags_argv(flags_root, "h"))
    captured = capfd.readouterr()
    assert rc == 130
    assert captured.err == ""
    assert captured.out == ""


def test_main_leaves_the_stdio_encodings_alone(flags_root):
    code = (
        "import sys\n"
        "before = (sys.stdout.encoding, sys.stderr.encoding)\n"
        "from hyera.cli import main\n"
        "rc = main(sys.argv[1:])\n"
        "after = (sys.stdout.encoding, sys.stderr.encoding)\n"
        "sys.stderr.write(repr(before) + ('SAME' if before == after else 'CHANGED'))\n"
        "sys.exit(rc)\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C", PYTHONPATH=_SRC)
    proc = subprocess.run(
        [sys.executable, "-c", code] + _flags_argv(flags_root, "str"),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    if "utf" in proc.stderr.lower().split("same")[0].split("changed")[0]:
        pytest.skip("the interpreter's piped stdio is already UTF-8")
    assert proc.stderr.endswith("SAME")


def _agent_help(*extra):
    proc = subprocess.run(
        [sys.executable, "-m", "hyera", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "AGENT_HELP": "1", "PYTHONPATH": _SRC},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_agent_help_declares_the_real_exit_codes():
    codes = _agent_help()["exit_codes"]
    assert codes["1"] == "No value found for the key"
    assert codes["2"].startswith("Any other error")
    assert "130" in codes


def test_agent_help_examples_run(monkeypatch, capsys):
    root = os.path.dirname(_SRC)
    monkeypatch.chdir(root)
    examples = _agent_help()["examples"]
    assert len(examples) >= 3
    for example in examples:
        argv = shlex.split(example["command"])
        assert argv[0] == "hyera"
        assert main(argv[1:]) == 0, example["command"]
        assert capsys.readouterr().out.strip() != ""


def test_agent_help_field_help_is_one_line_and_facts_is_required():
    options = {o["dest"]: o["help"] for o in _agent_help()["options"] if "dest" in o}
    assert "REQUIRED" in options["facts"]
    for dest, text in options.items():
        assert "\n" not in text, dest
