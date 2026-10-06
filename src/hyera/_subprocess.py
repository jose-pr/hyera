"""The one place hyera runs an external program (``sops``, ``facter``)."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import typing as _ty

from .exceptions import BackendError, BackendTimeoutError

#: Characters of the child's standard error kept in an error message.
_STDERR_TAIL = 2000

#: Seconds to wait for the pipes to drain after the process group is killed.
_REAP_TIMEOUT = 5

_WINDOWS = sys.platform == "win32"


def _refuse_batch(program: str, exe: str) -> None:
    """Raise if *exe* is a ``.bat``/``.cmd`` shim, in any letter case."""
    if os.path.splitext(exe)[1].lower() in (".bat", ".cmd"):
        raise BackendError(
            "refusing to run {} batch shim {}: cmd.exe re-parses its own "
            "argument line, which is unsafe for a data-derived path".format(
                program, exe
            )
        )


def _resolve(program: str, refuse_batch: bool) -> str:
    """The absolute path of *program* on ``PATH``, or a :class:`BackendError`."""
    exe = shutil.which(program)
    if exe is None:
        raise BackendError("{} executable not found on PATH".format(program))
    # Checked before the relative test: a Windows-style path is never
    # absolute on POSIX, and the batch refusal must hold whatever it looks
    # like.
    if refuse_batch:
        _refuse_batch(program, exe)
    if not (os.path.isabs(exe) or exe.startswith(("/", "\\"))):
        # shutil.which on Windows with Python 3.9 to 3.11 still searches the current directory and can return a
        # path relative to it, even with NoDefaultCurrentDirectoryInExePath set. A leading separator without a
        # drive is rooted, not cwd-relative, and is accepted.
        raise BackendError(
            "refusing to run {} resolved to a relative path {!r} (from the "
            "current directory or a relative PATH entry); put an absolute "
            "{} on PATH instead".format(program, exe, program)
        )
    exe = os.path.abspath(exe)
    # abspath normalizes forms such as ``sops.bat.`` into ``.bat``.
    if refuse_batch:
        _refuse_batch(program, exe)
    return exe


def _job_for(proc: "subprocess.Popen[bytes]") -> _ty.Any:
    """On Windows, a Job Object holding *proc*, set to kill its members when
    closed; ``None`` elsewhere or when any Win32 call fails. Unlike
    ``taskkill /T``, which walks the tree as it is at that instant, a job also
    holds processes spawned while it is being killed.
    """
    if not _WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        ) and kernel32.AssignProcessToJobObject(
            job, int(proc._handle)
        )  # type: ignore[attr-defined]
        if not ok:
            kernel32.CloseHandle(ctypes.c_void_p(job))
            return None
        return (kernel32, job)
    except Exception:
        return None


def _job_call(job: _ty.Any, name: str) -> None:
    """Terminate (``TerminateJobObject``) or close (``CloseHandle``) *job*."""
    if job is None:
        return
    import ctypes

    kernel32, handle = job
    try:
        if name == "terminate":
            kernel32.TerminateJobObject(ctypes.c_void_p(handle), 1)
        else:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        pass


def _kill_tree(proc: "subprocess.Popen[bytes]", job: _ty.Any = None) -> None:
    """Kill *proc* and every process it started."""
    _job_call(job, "terminate")
    if _WINDOWS:
        taskkill = os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe"
        )
        try:
            subprocess.run(
                [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_REAP_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _kill_and_reap(proc: "subprocess.Popen[bytes]", job: _ty.Any = None) -> None:
    """Kill *proc*'s tree, wait for it a bounded time and close its pipes."""
    _kill_tree(proc, job)
    try:
        proc.communicate(timeout=_REAP_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def run(
    program: str,
    args: _ty.Sequence[str],
    *,
    timeout: float,
    refuse_batch: bool = False,
    context: str = "",
) -> bytes:
    """Run *program* with *args* and return its standard output.

    *program* is resolved once with :func:`shutil.which` and run by its
    absolute path, never through a shell. Its standard input is the null
    device and it runs in its own process group, which is killed on timeout.
    A relative resolution is refused; a ``.bat``/``.cmd`` shim too when
    *refuse_batch* is set. Standard output is never part of an error.

    :param program: the bare name to look up on ``PATH``.
    :param args: the arguments after the program.
    :param timeout: seconds to wait before killing the process group.
    :param refuse_batch: refuse a batch-file shim.
    :param context: a phrase appended to every message (``decrypting <path>``).
    :returns: the program's standard output.
    :raises BackendTimeoutError: the program did not finish in time.
    :raises BackendError: the program is missing, refused, cannot start, or
        exits non-zero (its last 2,000 characters of standard error are quoted).
    """
    suffix = " " + context if context else ""
    try:
        exe = _resolve(program, refuse_batch)
    except BackendError as e:
        if context:
            e.args = ("{} while{}".format(e.args[0], suffix),)
        raise

    kwargs: _ty.Dict[str, _ty.Any] = {}
    if _WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    except OSError as e:
        raise BackendError("Failed to run {}{}: {}".format(program, suffix, e)) from e

    job = _job_for(proc)
    try:
        return _wait(proc, job, program, timeout, suffix)
    finally:
        _job_call(job, "close")


def _wait(
    proc: "subprocess.Popen[bytes]",
    job: _ty.Any,
    program: str,
    timeout: float,
    suffix: str,
) -> bytes:
    """Wait for *proc*; kill its tree on timeout or any other exception."""
    # The timeout error is raised after the handler: a TimeoutExpired carries the child's partial stdout, which
    # raising inside the handler would keep reachable through __context__. Any other exception kills the child
    # (in its own process group, so it would outlive the caller) and propagates unchanged.
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except BaseException:
        _kill_and_reap(proc, job)
        raise
    if timed_out:
        _kill_and_reap(proc, job)
        raise BackendTimeoutError(
            "{} timed out after {}s{}".format(program, timeout, suffix)
        ) from None

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", "replace").strip()[-_STDERR_TAIL:]
        raise BackendError(
            "{} failed (exit {}){}: {}".format(
                program, proc.returncode, suffix, detail or "<no stderr>"
            )
        )
    return stdout
