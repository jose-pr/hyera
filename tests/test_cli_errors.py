"""CLI failures: error messages, exit codes and tracebacks."""

import logging
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


def test_main_without_duho_prints_hint_and_exits_2(monkeypatch, capsys):
    # duho is None only when the "cli" extra's import failed at module load, which
    # cannot be reproduced afterwards; the downstream check is exercised directly.
    import hyera.cli as cli

    monkeypatch.setattr(cli, "duho", None)
    rc = cli.main(["k"])
    assert rc == 2
    assert 'pip install "hyera[cli]"' in capsys.readouterr().err


def test_cli_module_reimported_without_duho_installed(monkeypatch, capsys):
    # Faking sys.modules["duho"] = None makes `import duho` raise
    # ModuleNotFoundError(name="duho"); dropping hyera.cli from sys.modules reruns its
    # module body, import and `if duho is not None:` guard included. Reloading is safe:
    # the module only defines names. monkeypatch.undo() runs before the final reimport
    # so the restored hyera.cli sees the real duho.
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
    # A duho that is present but broken (here without `duho.logging`) must not be taken
    # for a missing cli extra: only ModuleNotFoundError naming "duho" itself means that.
    # A stub module with no `__path__` reproduces it, once the cached real
    # `duho.logging` entry is removed from sys.modules.
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
