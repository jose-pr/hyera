"""The hyera side of a comparison: each job through the API and the CLI.

Both channels run in this process. The API is built the way the conformance
replay builds it (a ``Scope`` from the facts file, the scenario's
``environments`` and ``modules`` directories, ``codedir`` naming a directory that
never exists); the CLI is ``hyera.cli.main`` with the query's own argument tail.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import logging
import os
import traceback
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from _golden import NODE, SENSITIVE_JSON

#: The server version a scope reports, as the reference's.
SERVER_VERSION = "8.10.0"
#: Flags the API channel can express; any other query runs through the CLI only.
_API_FLAGS = ("--strict", "--environment")


def _flag(args: List[str], name: str) -> Optional[str]:
    value = None
    i = 0
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        i += 1
    return value


def api_expressible(query: dict) -> bool:
    """Whether the query uses only flags the API channel understands."""
    args = query.get("args") or []
    i = 0
    while i < len(args):
        if args[i] in _API_FLAGS and i + 1 < len(args):
            i += 2
            continue
        return False
    return True


@contextlib.contextmanager
def _chdir(path: Path) -> Iterator[None]:
    previous = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(previous)


def _key_text(key) -> str:
    """A hash key as Ruby's ``to_s`` spells it, which is how Puppet's JSON
    renderer writes a key that is not a string."""
    if isinstance(key, str):
        return key
    if key is None:
        return ""
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, float):
        if key != key:
            return "NaN"
        if key in (float("inf"), float("-inf")):
            return "Infinity" if key > 0 else "-Infinity"
    return str(key)


def _project(value):
    from hyera import Sensitive

    if isinstance(value, Sensitive):
        return SENSITIVE_JSON
    if isinstance(value, dict):
        return {_key_text(k): _project(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_project(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"__unrenderable__": repr(value)}


def _jsonable(value):
    """A value as Puppet's JSON renderer shows it.

    :raises ValueError: for NaN and the infinities, which the renderer refuses.
    """
    projected = _project(value)
    json.dumps(projected, allow_nan=False)
    return projected


class _Rejected(Exception):
    """The invocation is one ``puppet lookup`` refuses before it looks anything up."""


def _build_scope(case_dir: Path, args: List[str]):
    """The scope ``puppet lookup`` builds from the facts file and the flags.

    A scope the constructor rejects (a fact named like a reserved variable, an
    unknown ``--strict`` value) and an empty facts file are failures of the
    command, not crashes of the library, so they are reported as errors.
    """
    from hyera import Scope, load_facts

    facts = load_facts(case_dir / "facts.yaml")
    if not facts:
        raise _Rejected("No facts available for target node: " + NODE)
    environment = _flag(args, "--environment")
    try:
        return Scope(
            facts=facts,
            environment="production" if environment is None else environment,
            server_facts={"serverversion": SERVER_VERSION},
            strict=_flag(args, "--strict") or "warning",
            node_name=NODE,
        )
    except (TypeError, ValueError) as e:
        raise _Rejected(str(e)) from e


def _crash(e: BaseException) -> dict:
    """The result for an exception that is not an outcome of the lookup."""
    return {
        "status": "crash",
        "exc": type(e).__name__,
        "message": str(e)[:300],
        "trace": traceback.format_exc()[-1200:],
    }


def run_api(job: dict, case_dir: Path) -> dict:
    """One query through ``Hiera.lookup``.

    :param job: a job from :func:`differential.generate.make_jobs`.
    :param case_dir: the scenario directory.
    :returns: ``{"status": "found", "value": ...}``, ``{"status": "not_found"}``,
        ``{"status": "error", "message": ...}``, ``{"status": "crash", ...}`` when
        something that is not a ``HieraError`` escaped, or ``{"status": "skipped"}``.
    """
    from hyera import Hiera, HieraError, KeyNotFoundError

    query = job["query"]
    if not api_expressible(query):
        return {"status": "skipped"}
    args = query.get("args") or []
    # an empty --type is no type at all, as it is for the command
    kwargs = {"value_type": query.get("type") or None, "merge": query.get("merge")}
    if query.get("default") is not None:
        kwargs["default_value"] = query["default"]
    try:
        with _chdir(case_dir):
            scope = _build_scope(case_dir, args)
            hiera = Hiera(
                str(case_dir / "hiera.yaml"),
                scope=scope,
                codedir=str(case_dir / "_no_codedir"),
                environmentpath=[case_dir / "environments"],
                basemodulepath=[case_dir / "modules"],
            )
            value = hiera.lookup(query["key"], **kwargs)
    except KeyNotFoundError as e:
        return {"status": "not_found", "message": str(e)}
    except _Rejected as e:
        return {"status": "error", "message": str(e), "exc": "Rejected"}
    except HieraError as e:
        return {"status": "error", "message": str(e), "exc": type(e).__name__}
    except (TypeError, ValueError) as e:
        # a --type or --merge value the library refuses as a bad argument is the
        # command's error, as it is for `puppet lookup`; anything else is a crash
        if kwargs["value_type"] is None and kwargs["merge"] is None:
            return _crash(e)
        return {"status": "error", "message": str(e), "exc": type(e).__name__}
    except BaseException as e:  # noqa: BLE001 - a non-HieraError is itself a result
        return _crash(e)
    try:
        return {"status": "found", "value": _jsonable(value)}
    except ValueError as e:
        # Puppet's JSON renderer refuses NaN and the infinities
        return {"status": "error", "message": str(e), "exc": "ValueError"}


class _Errors(logging.Handler):
    """Collects what the CLI logs at ERROR level, where it reports a failure."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def run_cli(job: dict, case_dir: Path) -> dict:
    """One query through ``hyera.cli.main``.

    :returns: ``{"rc", "stdout", "errors"}``; ``rc`` is ``-2`` when argument
        parsing exited the process, with the exit code under ``usage``.
    """
    from hyera.cli import main

    argv = [
        "--hiera_config",
        str(case_dir / "hiera.yaml"),
        "--facts",
        str(case_dir / "facts.yaml"),
        "--node",
        NODE,
    ]
    # the reference always has both, whether or not the directories exist
    argv += ["--environmentpath", str(case_dir / "environments")]
    argv += ["--basemodulepath", str(case_dir / "modules")]
    argv += ["--codedir", str(case_dir / "_no_codedir")]
    argv += ["--render-as", job.get("render") or "json"] + list(job["argv"])
    out = io.StringIO()
    errors = _Errors()
    logger = logging.getLogger("hyera")
    logger.addHandler(errors)
    try:
        with _chdir(case_dir), contextlib.redirect_stdout(out):
            with contextlib.redirect_stderr(io.StringIO()):
                rc = main(argv)
    except SystemExit as e:
        return {"rc": -2, "usage": e.code, "stdout": out.getvalue(), "errors": []}
    finally:
        logger.removeHandler(errors)
    return {"rc": rc, "stdout": out.getvalue(), "errors": errors.messages}


def run_job(job: dict, root: Path) -> dict:
    """Both channels for one job (the render variants run the CLI only)."""
    case_dir = Path(root) / job["dir"]
    result: dict = {"id": job["id"]}
    if not job.get("render"):
        result["api"] = run_api(job, case_dir)
    result["cli"] = run_cli(job, case_dir)
    return result


def _run_chunk(args) -> List[dict]:
    root, jobs = args
    return [run_job(job, Path(root)) for job in jobs]


def run_jobs(jobs: List[dict], root: Path, workers: int = 1) -> List[dict]:
    """Run every job, in order.

    :param jobs: the job list.
    :param root: the directory the jobs' ``dir`` entries are relative to.
    :param workers: processes to use; 1 runs in this process.
    """
    if workers <= 1 or len(jobs) < 2 * workers:
        return _run_chunk((str(root), jobs))
    size = max(1, len(jobs) // (workers * 4))
    chunks: Dict[int, list] = {}
    for start in range(0, len(jobs), size):
        chunks[start] = jobs[start : start + size]
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(_run_chunk, [(str(root), c) for c in chunks.values()]))
    return [item for part in parts for item in part]
