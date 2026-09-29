"""Command-line interface for hyera, built on duho.

``hyera KEY --config hiera.yaml --scope environment=production``

Designed for unattended use: no interactive prompts, deterministic output,
and meaningful exit codes: ``0`` found (or ``--default`` printed), ``1``
key not found, ``2`` any other error (one stderr line; ``-v`` or
``DUHO_TRACEBACK=1`` adds the traceback).
"""

import json as _json
import logging as _logging
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
from ._types import Sensitive

_LOGGER = _logging.getLogger("hyera")

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


def _plain(value):
    """Convert to plain, YAML/JSON-safe types, recursively.

    A :class:`~hyera.core.Sensitive` becomes its redacted text (the same
    text raw/json output already show); a ``dict`` becomes a plain
    ``dict``; a ``list``/``tuple`` becomes a plain ``list``. Everything
    else passes through unchanged.
    """
    if isinstance(value, Sensitive):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _describe(e) -> str:
    """One-line description of an exception for the CLI's failure log."""
    if isinstance(e, (HieraError, OSError)):
        return _one_line(e)
    return "{}: {}".format(type(e).__name__, _one_line(e))


def _dump(value, fmt: str) -> str:
    value = _plain(value)
    if fmt == "json":
        return _json.dumps(value, default=str, indent=2, sort_keys=True)
    if fmt == "yaml":
        import yaml

        return yaml.safe_dump(value, default_flow_style=False).rstrip("\n")
    # raw
    if isinstance(value, (dict, list)):
        return _json.dumps(value, default=str)
    return str(value)


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
        output: "duho.Arg[str, duho.NS(flags=['--output', '-o']), duho.Choice('raw', 'json', 'yaml')]" = ("raw")
        """Output format for the resolved value."""
        default: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--default'])]" = None
        """Value to print when the key is missing (otherwise exit 1)."""

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
            try:
                context = _parse_scope(self.scope)
            except _ScopeError as e:
                _LOGGER.error("%s", e)
                return 2

            merge = self._merge_spec()
            try:
                hiera = Hiera(self.config, scope=Scope(variables=context))
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
                print(_dump(value, self.output))
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
