"""Command-line interface for hyera, built on duho.

``hyera KEY --config hiera.yaml --scope environment=production``

Designed for unattended use: no interactive prompts, deterministic output,
and meaningful exit codes: ``0`` found (or ``--default`` printed), ``1``
key not found, ``2`` any other error (one stderr line; ``-v`` or
``DUHO_TRACEBACK=1`` adds the traceback). Output is rendered the way
``puppet lookup --render-as s|json|yaml`` does (:mod:`hyera._render`),
written as UTF-8 bytes with LF line endings whatever the console/locale
encoding.
"""

import io as _io
import logging as _logging
import os as _os
import sys as _sys
import typing as _ty

try:
    import duho
    import duho.logging as _duho_logging
except ModuleNotFoundError as _e:
    if _e.name != "duho":
        raise
    duho = None

from . import __version__
from .exceptions import HieraError, KeyNotFoundError, _one_line
from .core import Hiera
from ._scope import Scope
from .backends import Backend

_LOGGER = _logging.getLogger(__name__)

#: CLI merge choice -> spec strategy name (array/set are legacy aliases).
_MERGE_ALIASES = {"array": "unique", "set": "unique"}

#: Printed (to stderr) when the ``cli`` extra (duho) is not installed.
# Double-quoted, not single-quoted: `pip install 'hyera[cli]'` fails when
# pasted into cmd.exe, where single quotes are literal (pip then sees the
# argv "'hyera[cli]'" verbatim and rejects it); double quotes work in
# cmd.exe, PowerShell and POSIX shells alike.
_NO_CLI_EXTRA_HINT = (
    "hyera: the command-line interface needs the cli extra: " 'pip install "hyera[cli]"'
)


class _ScopeError(ValueError):
    """A ``--scope`` entry was not in ``key=value`` form, or its name is invalid."""

    def __init__(self, item: str, reason: str = "expected key=value"):
        super().__init__("invalid --scope {!r} ({})".format(item, reason))


def _parse_scope(items: "_ty.Iterable[str]") -> dict:
    """Parse ``key=value`` scope entries into a context dict.

    A dotted name is rejected outright rather than stored as a flat key
    nothing can read: Puppet variable names cannot contain ``.``, and
    there is no flat-dotted-key context fallback that would have made one
    work by accident.
    """
    context: dict = {}
    for item in items or ():
        if not item:
            continue
        if "=" not in item:
            raise _ScopeError(item)
        k, v = item.split("=", 1)
        if "." in k:
            raise _ScopeError(item, "a variable name cannot contain '.'")
        context[k] = v
    return context


def _describe(e) -> str:
    """One-line description of an exception for the CLI's failure log."""
    if isinstance(e, (HieraError, OSError)):
        return _one_line(e)
    return "{}: {}".format(type(e).__name__, _one_line(e))


def _emit(text: str) -> None:
    """Write ``text`` to stdout the way Ruby's ``puts`` does: a trailing
    newline is appended only if ``text`` does not already end with one.

    Always UTF-8 bytes with LF line endings, regardless of the console or
    locale encoding: written to ``sys.stdout.buffer`` when one exists (a
    real console or pipe), else (a ``StringIO`` under
    ``contextlib.redirect_stdout``, as the conformance harness uses)
    ``sys.stdout.write`` directly. Never ``sys.stdout.reconfigure`` --
    that would change the caller's own stream when ``main()`` runs
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

    Called after a :class:`BrokenPipeError`: the reader is already gone,
    so nothing further should try to write to (or complain about) the
    broken pipe, including whatever the interpreter does with stdout at
    exit. Best-effort: a stream with no real file descriptor (a
    ``StringIO``) just leaves this a no-op.
    """
    try:
        _os.dup2(_os.open(_os.devnull, _os.O_WRONLY), _sys.stdout.fileno())
    except (OSError, ValueError, _io.UnsupportedOperation):
        pass


if duho is not None:

    class Lookup(duho.LoggingArgs, duho.Cli):
        """Look up a key in a hiera hierarchy and print the resolved value."""

        _version_ = __version__
        _mcp_ = True
        _parsername_ = "hyera"

        key: "duho.Arg[str, duho.NS(flags=['key'], metavar='KEY', help='hiera key to look up')]"
        config: "duho.Arg[str, duho.NS(flags=['--config', '-c'], help='path to the hiera base config')]" = ("hiera.yaml")
        scope: (
            "duho.Arg[_ty.List[str], duho.NS(flags=['--scope', '-s']), duho.Append()]"
        ) = None
        """Context variable ``key=value`` (repeatable)."""
        merge: (
            "duho.Arg[_ty.Optional[str], "
            "duho.Choice('first', 'unique', 'hash', 'deep', 'array', 'set')]"
        ) = None
        """Merge strategy. Omitted: the data's ``lookup_options`` decide,
        else first found. An explicit value, ``first`` included, overrides
        ``lookup_options``. ``array``/``set`` are legacy aliases for
        ``unique``."""
        deep: bool = False
        """Promote ``--merge hash`` to a deep merge (legacy convenience)."""
        knockout_prefix: (
            "duho.Arg[_ty.Optional[str], duho.NS(flags=['--knockout-prefix'])]"
        ) = None
        """Deep-merge knockout prefix (marks keys/values to remove)."""
        render_as: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--render-as'], metavar='FORMAT')]" = (None)
        """Output format: s, json or yaml (default yaml)."""
        default: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--default'])]" = None
        """Value to print when the key is missing (otherwise exit 1)."""
        codedir: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--codedir'])]" = None
        """Puppet's $codedir (Puppet's own AIO default per platform if
        omitted); consulted only by a version 3 hiera.yaml's default
        per-backend datadir."""

        def _merge_spec(self):
            if self.merge is None:
                return None
            strategy = _MERGE_ALIASES.get(self.merge, self.merge)
            if strategy == "hash" and self.deep:
                strategy = "deep"
            if strategy == "deep" and self.knockout_prefix:
                return {"strategy": "deep", "knockout_prefix": self.knockout_prefix}
            return strategy

        def _fail(self, text) -> int:
            """Log one ERROR-level line and return exit code 2.

            The traceback is attached only when explicitly asked for
            (``-v`` or ``DUHO_TRACEBACK=1``); an unattended caller gets a
            single line, not a stack.
            """
            kw = {}
            if self.verbose > 0 or _duho_logging.traceback_enabled():
                kw = {"exc_info": True}
            _LOGGER.error("%s", text, **kw)
            return 2

        def __call__(self) -> int:
            fmt = (self.render_as or "yaml").lower()
            if Backend.find(fmt, kind="render") is None:
                return self._fail("Unknown rendering format '{}'".format(fmt))

            try:
                context = _parse_scope(self.scope)
            except _ScopeError as e:
                _LOGGER.error("%s", e)
                return 2

            merge = self._merge_spec()
            try:
                hiera = Hiera(
                    self.config, scope=Scope(variables=context), codedir=self.codedir
                )
                value = hiera.lookup(self.key, merge=merge)
            except KeyNotFoundError:
                if self.default is not None:
                    value = self.default
                else:
                    _LOGGER.error("key not found: %s", self.key)
                    return 1
            except Exception as e:  # HieraError, OSError, anything unexpected
                return self._fail(
                    "Lookup of key '{}' failed: {}".format(self.key, _describe(e))
                )

            try:
                text = Backend.new(fmt, kind="render").dumps(value)
                _emit(text)
            except BrokenPipeError:
                _silence_stdout()
                return 2
            except Exception as e:
                return self._fail(
                    "Cannot render the value of key '{}': {}".format(
                        self.key, _describe(e)
                    )
                )
            return 0


def main(argv=None) -> int:
    # duho.main sets up stderr logging (honoring -v/-q/--loglevel) and
    # dispatches to Lookup.__call__, whose int return becomes the exit code.
    if duho is None:
        print(_NO_CLI_EXTRA_HINT, file=_sys.stderr)
        return 2
    return duho.main(Lookup, argv)


if __name__ == "__main__":
    _sys.exit(main())
