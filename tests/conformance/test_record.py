"""``record.py``'s own command-building logic. Needs no Puppet and no WSL --
these are pure-function checks of the argv ``_command`` builds, not an
actual run of it.
"""

from pathlib import Path

from record import _command


def test_wsl_runner_uses_dash_e_not_dash_dash():
    # wsl.exe's `--` form relays the trailing argv through a shell on the
    # Linux side that strips a lone or wrapping single quote from an
    # argument before the command ever sees it; `-e` does not. Verified
    # directly against the WSL distro this project records against:
    # `wsl.exe -d <distro> -- ruby -e 'puts ARGV.inspect' "'a.b'"` delivers
    # `["a.b"]` (quotes gone), the `-e` form delivers `["'a.b'"]` (quotes
    # intact, the TRUE argument).
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
