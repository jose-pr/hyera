"""CLI lookups: found and missing keys, defaults, environments and exit codes."""

import json
import logging
import os
import runpy
import subprocess
import sys

import pytest

duho = pytest.importorskip("duho")

import hyera  # noqa: E402
from hyera.cli import main  # noqa: E402
from cli_support import (  # noqa: F401
    _error_records,
    _flags_argv,
    flags_root,
    hiera_root,
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
