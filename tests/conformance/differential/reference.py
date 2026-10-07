"""Running the reference Puppet over a batch of jobs.

One Ruby process takes the whole batch (``drive_puppet.rb`` forks one child per
query), so the cost of loading Puppet is paid once. The runner is the
recorder's: ``local`` runs ``ruby`` here, ``wsl`` or ``wsl:<distro>`` runs it in
WSL.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from record import _command, _iso_root, _versions

DRIVER = Path(__file__).resolve().parent / "drive_puppet.rb"


class ReferenceError(RuntimeError):
    """The reference could not be run or returned nothing usable."""


def to_runner_path(runner: str, path: Path) -> str:
    """``path`` as the machine that runs Puppet names it."""
    text = str(path)
    if not runner.startswith("wsl"):
        return text
    posix = text.replace("\\", "/")
    if len(posix) > 1 and posix[1] == ":":
        return "/mnt/" + posix[0].lower() + posix[2:]
    return posix


def run_puppet(
    runner: str,
    jobs: List[dict],
    work: Path,
    workers: int = 8,
    progress: Optional[Callable[[str], None]] = None,
) -> List[dict]:
    """Run ``jobs`` through Puppet and return one raw record per job, in order.

    :param runner: ``local``, ``wsl`` or ``wsl:<distro>``.
    :param jobs: dicts with ``id``, ``dir`` (a path that exists on this machine)
        and ``argv``; ``render`` is optional.
    :param work: a scratch directory the job and result files are written to.
    :param workers: queries run at once inside the Ruby process.
    :param progress: called with each progress line the driver prints.
    :raises ReferenceError: when the driver fails or returns a different count.
    """
    work.mkdir(parents=True, exist_ok=True)
    remote = []
    for job in jobs:
        item = dict(job)
        item["dir"] = to_runner_path(runner, Path(job["dir"]))
        remote.append(item)
    jobs_file = work / "puppet-jobs.json"
    out_file = work / "puppet-results.json"
    jobs_file.write_bytes(json.dumps(remote, ensure_ascii=False).encode("utf-8"))
    if out_file.exists():
        out_file.unlink()
    args = [
        to_runner_path(runner, DRIVER),
        to_runner_path(runner, jobs_file),
        to_runner_path(runner, out_file),
        str(workers),
    ]
    cmd, cwd = _command(runner, work, args, program="ruby")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        raise ReferenceError("cannot start the reference: {}".format(e))
    assert proc.stderr is not None
    tail: List[str] = []
    for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        tail = (tail + [line])[-20:]
        if progress and "/" in line and line.split("/")[0].isdigit():
            progress(line)
    code = proc.wait()
    if code != 0 or not out_file.exists():
        raise ReferenceError(
            "the reference driver exited {}:\n{}".format(code, "\n".join(tail))
        )
    results = json.loads(out_file.read_text(encoding="utf-8"))
    if len(results) != len(jobs) or any(
        r is None or r["id"] != j["id"] for r, j in zip(results, jobs)
    ):
        raise ReferenceError(
            "the reference returned results that do not match the jobs"
        )
    return results


def reference_versions(runner: str) -> dict:
    """Puppet's and Ruby's versions and the gem versions, and the recording
    host's identities (kept only to refuse a leaking record).

    :returns: ``{"versions": {...}, "identities": (...)}``.
    """
    versions, identities = _versions(runner, _iso_root(runner))
    return {"versions": versions, "identities": identities}


def echo(line: str) -> None:
    sys.stderr.write(line + "\n")
