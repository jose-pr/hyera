"""CLI behavior and exit codes (unattended-friendly)."""

import pytest

duho = pytest.importorskip("duho")

from hiera.cli import main  # noqa: E402


def test_lookup_found(hiera_root, capsys):
    rc = main([str("app::name"), "-c", str(hiera_root / "hiera.yaml"),
               "-s", "environment=production"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "myapp"


def test_lookup_missing_exit_1(hiera_root, capsys):
    rc = main(["nope::key", "-c", str(hiera_root / "hiera.yaml")])
    assert rc == 1


def test_lookup_missing_with_default_exit_0(hiera_root, capsys):
    rc = main(["nope::key", "-c", str(hiera_root / "hiera.yaml"),
               "--default", "fallback"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "fallback"


def test_bad_config_exit_2(tmp_path):
    rc = main(["k", "-c", str(tmp_path / "does-not-exist.yaml")])
    assert rc == 2


def test_array_merge_json_output(hiera_root, capsys):
    rc = main(["classes", "-c", str(hiera_root / "hiera.yaml"),
               "-s", "environment=production", "--merge", "array", "-o", "json"])
    assert rc == 0
    import json
    assert json.loads(capsys.readouterr().out) == ["prod", "web", "base"]


def test_invalid_scope_exit_2(hiera_root):
    rc = main(["app::name", "-c", str(hiera_root / "hiera.yaml"), "-s", "noequals"])
    assert rc == 2


def test_module_entrypoint_smoke():
    # `python -m hiera` wires through to cli.main.
    import runpy
    import sys

    argv = sys.argv
    sys.argv = ["hiera", "--help"]
    try:
        with pytest.raises(SystemExit) as exc:
            runpy.run_module("hiera", run_name="__main__")
        assert exc.value.code == 0  # --help exits 0
    finally:
        sys.argv = argv
