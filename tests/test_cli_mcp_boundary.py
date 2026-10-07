"""The operator's boundary around the MCP tool: ``HYERA_MCP_ROOT`` and
``HYERA_MCP_BACKENDS``, driven through the real stdio server."""

import json
import os
import subprocess
import sys

import pytest

duho = pytest.importorskip("duho")

from hyera.cli import main  # noqa: E402
from cli_support import _SRC  # noqa: E402

_MARK = "SECRETMARK-not-for-the-caller"
_INIT = [
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
]


def _link(link, target):
    try:
        if os.name == "nt":
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
            )
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
    except (OSError, subprocess.CalledProcessError) as e:
        pytest.skip("cannot create a directory link here: {}".format(e))


@pytest.fixture
def world(tmp_path, make_tree):
    """``tmp/root`` holds a hierarchy, facts and a data tree; ``tmp/outside``
    holds a facts file, a hiera.yaml and a data file the caller must not reach."""
    root = make_tree(
        {"hierarchy": [{"name": "p", "path": "%{facts.x}.yaml"}]},
        files={
            "data/common.yaml": "app: myapp\n",
            "other/secret.yaml": "leak: {}\n".format(_MARK),
        },
        facts={"x": "common"},
        root="root",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "facts.yaml").write_text("x: {}\n".format(_MARK), encoding="utf-8")
    (outside / "hiera.yaml").write_text(
        "version: 5\nhierarchy: []\n# {}\n".format(_MARK), encoding="utf-8"
    )
    return root, outside


def _call(arguments, env=None, cwd=None, messages=None):
    messages = _INIT + [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "hyera", "arguments": arguments},
        }
    ]
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, "HYERA_MCP": "stdio", "PYTHONPATH": _SRC, **(env or {})},
        cwd=cwd,
        timeout=120,
    )
    replies = {}
    for line in proc.stdout.splitlines():
        reply = json.loads(line)
        replies[reply.get("id")] = reply
    return proc, replies.get(2, {})


def _args(root, **extra):
    base = {
        "keys": ["app"],
        "hiera_config": str(root / "hiera.yaml"),
        "facts": str(root / "facts.yaml"),
        "render_as": "s",
    }
    base.update(extra)
    return base


def _text(reply):
    result = reply.get("result", {})
    return "".join(c.get("text", "") for c in result.get("content", []))


def _failed(reply):
    return "error" in reply or reply.get("result", {}).get("isError") is True


def test_a_call_inside_the_root_works(world):
    root, _ = world
    proc, reply = _call(_args(root), {"HYERA_MCP_ROOT": str(root)})
    assert proc.returncode == 0, proc.stderr
    assert _text(reply) == "myapp\n"


_PATH_FIELDS = [
    "facts",
    "hiera_config",
    "environmentpath",
    "modulepath",
    "basemodulepath",
    "codedir",
]


def _outside_value(field, how, root, outside):
    """A value for ``field`` that names something in ``outside``: the facts
    file, the config file, or the directory itself for a directory setting."""
    name = {"facts": "facts.yaml", "hiera_config": "hiera.yaml"}.get(field, "")
    if how == "absolute":
        base = str(outside)
    elif how == "dotdot":
        base = os.path.join(str(root), "..", "outside")
    else:
        link = root / "lnk"
        if not link.exists():
            _link(link, outside)
        base = str(link)
    return os.path.join(base, name) if name else base


@pytest.mark.parametrize("how", ["absolute", "dotdot", "link"])
@pytest.mark.parametrize("field", _PATH_FIELDS)
def test_a_path_argument_outside_the_root_is_refused(world, field, how):
    root, outside = world
    value = _outside_value(field, how, root, outside)
    proc, reply = _call(_args(root, **{field: value}), {"HYERA_MCP_ROOT": str(root)})
    assert _failed(reply), (proc.stdout, proc.stderr)
    shown = proc.stdout + proc.stderr
    assert _MARK not in shown
    flag = "--" + field
    assert flag in shown
    assert "HYERA_MCP_ROOT" in shown


def test_a_path_list_with_one_outside_entry_is_refused(world):
    root, outside = world
    value = os.pathsep.join([str(root / "data"), str(outside)])
    proc, reply = _call(_args(root, modulepath=value), {"HYERA_MCP_ROOT": str(root)})
    assert _failed(reply)
    assert "--modulepath" in proc.stdout + proc.stderr


def test_without_a_config_the_working_directory_must_be_inside(world):
    root, outside = world
    arguments = _args(root)
    del arguments["hiera_config"]
    proc, reply = _call(arguments, {"HYERA_MCP_ROOT": str(root)}, cwd=str(outside))
    assert _failed(reply)
    assert "working directory" in proc.stdout + proc.stderr


def test_a_fact_in_a_path_cannot_leave_the_datadir(world):
    root, _ = world
    facts = root / "facts.yaml"
    facts.write_text("x: ../other/secret\n", encoding="utf-8")
    arguments = _args(root, keys=["leak"])
    unbounded, open_reply = _call(arguments)
    assert _MARK in _text(open_reply)
    proc, reply = _call(arguments, {"HYERA_MCP_ROOT": str(root)})
    assert _MARK not in proc.stdout + proc.stderr


def test_no_facts_file_is_refused(world):
    root, _ = world
    arguments = _args(root)
    del arguments["facts"]
    proc, reply = _call(arguments, {"HYERA_MCP_ROOT": str(root)})
    assert _failed(reply)
    assert "No facts available" in proc.stdout + proc.stderr


def _backend_tree(make_tree, function):
    return make_tree(
        {
            "hierarchy": [{"name": "p", "path": "common.yaml", "data_hash": function}],
        },
        files={"data/common.yaml": "app: myapp\n"},
        facts={"x": "common"},
        root="backends-" + function,
    )


def test_an_allowed_function_works(make_tree):
    root = _backend_tree(make_tree, "yaml_data")
    proc, reply = _call(_args(root), {"HYERA_MCP_BACKENDS": "yaml_data"})
    assert _text(reply) == "myapp\n", proc.stderr


@pytest.mark.parametrize("function", ["json_data", "sops_data"])
def test_a_function_outside_the_allow_list_is_refused(make_tree, function):
    root = _backend_tree(make_tree, function)
    proc, reply = _call(_args(root), {"HYERA_MCP_BACKENDS": "yaml_data"})
    assert _failed(reply)
    assert function in proc.stdout + proc.stderr


def test_the_allow_list_accepts_several_names(make_tree):
    root = _backend_tree(make_tree, "json_data")
    (root / "data" / "common.yaml").write_text('{"app": "j"}', encoding="utf-8")
    proc, reply = _call(_args(root), {"HYERA_MCP_BACKENDS": "yaml_data, json_data"})
    assert _text(reply) == "j\n", proc.stderr


@pytest.mark.parametrize(
    "env, needle",
    [
        ({"HYERA_MCP_ROOT": "no-such-directory-here"}, "HYERA_MCP_ROOT"),
        ({"HYERA_MCP_BACKENDS": "yaml_data,nope_data"}, "HYERA_MCP_BACKENDS"),
        ({"HYERA_MCP_BACKENDS": "yaml_data,"}, "HYERA_MCP_BACKENDS"),
    ],
)
def test_a_bad_variable_stops_the_server_at_start(world, env, needle):
    root, _ = world
    proc = subprocess.run(
        [sys.executable, "-m", "hyera"],
        input="",
        capture_output=True,
        text=True,
        env={**os.environ, "HYERA_MCP": "stdio", "PYTHONPATH": _SRC, **env},
        timeout=60,
    )
    assert proc.returncode == 2
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and needle in lines[0]
    assert proc.stdout == ""


def test_the_variables_are_ignored_outside_the_mcp_server(world, monkeypatch, capsys):
    root, _ = world
    monkeypatch.delenv("HYERA_MCP", raising=False)
    monkeypatch.setenv("HYERA_MCP_ROOT", "no-such-directory-here")
    monkeypatch.setenv("HYERA_MCP_BACKENDS", "nope_data")
    rc = main(
        [
            "app",
            "--hiera_config",
            str(root / "hiera.yaml"),
            "--facts",
            str(root / "facts.yaml"),
            "--render-as",
            "s",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "myapp\n"
