"""Command-line interface for pyera, built on duho.

``pyera KEY --config hiera.yaml --scope environment=production``

Designed for unattended use: no interactive prompts, deterministic output,
and meaningful exit codes (0 found, 1 missing, 2 usage/config error).
"""

import json as _json
import logging as _logging
import sys as _sys
import typing as _ty

try:
    import duho
except ModuleNotFoundError as _e:
    if _e.name != "duho":
        raise
    duho = None

from . import __version__
from .exceptions import HieraError
from .core import Hiera, Sensitive

_LOGGER = _logging.getLogger("pyera")

#: CLI merge choice -> spec strategy name (array/set are legacy aliases).
_MERGE_ALIASES = {"array": "unique", "set": "unique"}

#: Printed (to stderr) when the ``cli`` extra (duho) is not installed.
_NO_CLI_EXTRA_HINT = (
    "pyera: the command-line interface needs the cli extra: " "pip install 'pyera[cli]'"
)


class _ScopeError(ValueError):
    """A ``--scope`` entry was not in ``key=value`` form."""

    def __init__(self, item: str):
        super().__init__("invalid --scope {!r} (expected key=value)".format(item))


def _parse_scope(items: "_ty.Iterable[str]") -> dict:
    """Parse ``key=value`` scope entries into a context dict."""
    context: dict = {}
    for item in items or ():
        if not item:
            continue
        if "=" not in item:
            raise _ScopeError(item)
        k, v = item.split("=", 1)
        context[k] = v
    return context


def _plain(value):
    """Convert to plain, YAML/JSON-safe types, recursively.

    A :class:`~pyera.core.Sensitive` becomes its redacted text (the same
    text raw/json output already show); any ``dict`` (a ``LookupDict``
    included) becomes a plain ``dict``; a ``list``/``tuple`` becomes a
    plain ``list``. Everything else passes through unchanged.
    """
    if isinstance(value, Sensitive):
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


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

        def __call__(self) -> int:
            try:
                context = _parse_scope(self.scope)
            except _ScopeError as e:
                _LOGGER.error("%s", e)
                return 2
            try:
                hiera = Hiera(self.config, context=context)
            except HieraError as e:
                _LOGGER.error("%s", e)
                return 2
            except OSError as e:
                _LOGGER.error("could not open config %s: %s", self.config, e)
                return 2

            merge = self._merge_spec()
            try:
                value = hiera.get(self.key, merge=merge, throw=True)
            except KeyError:
                if self.default is not None:
                    print(_dump(self.default, self.output))
                    return 0
                _LOGGER.error("key not found: %s", self.key)
                return 1
            except HieraError as e:
                _LOGGER.error("%s", e)
                return 2

            print(_dump(value, self.output))
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
