"""Writing the result to stdout."""

import io as _io
import os as _os
import sys as _sys


def _emit(text: str) -> None:
    """Write text to stdout the way Ruby's puts does: a trailing
    newline is appended only if text does not already end with one.

    Always UTF-8 bytes with LF line endings, regardless of the console or
    locale encoding: written to sys.stdout.buffer when one exists (a
    real console or pipe), else (a StringIO under
    contextlib.redirect_stdout, as the conformance harness uses)
    sys.stdout.write directly. Never sys.stdout.reconfigure --
    that would change the caller's own stream when main() runs
    in-process.
    """
    if not text.endswith("\n"):
        text += "\n"
    buffer = getattr(_sys.stdout, "buffer", None)
    if buffer is not None:
        _sys.stdout.flush()
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        _sys.stdout.write(text)
        _sys.stdout.flush()


def _silence_stdout() -> None:
    """Redirect the stdout file descriptor to the null device.

    Called after a BrokenPipeError: the reader is already gone,
    so nothing further should try to write to (or complain about) the
    broken pipe, including whatever the interpreter does with stdout at
    exit. Best-effort: a stream with no real file descriptor (a
    StringIO) just leaves this a no-op.
    """
    try:
        _os.dup2(_os.open(_os.devnull, _os.O_WRONLY), _sys.stdout.fileno())
    except (OSError, ValueError, _io.UnsupportedOperation):
        pass
