"""Command-line interface for hiera, built on duho.

``hiera KEY --config hiera.yaml --scope environment=production``

Designed for unattended use: no interactive prompts, deterministic output,
and meaningful exit codes (0 found, 1 missing, 2 usage/config error).
"""

import json as _json
import logging as _logging
import sys as _sys
import typing as _ty

import duho

from . import __version__
from .exceptions import HieraError
from .core import Hiera

_LOGGER = _logging.getLogger("hiera")

#: CLI merge choice -> spec strategy name (array/set are legacy aliases).
_MERGE_ALIASES = {"array": "unique", "set": "unique"}


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


def _dump(value, fmt: str) -> str:
    if fmt == "json":
        return _json.dumps(value, default=str, indent=2, sort_keys=True)
    if fmt == "yaml":
        import yaml

        return yaml.safe_dump(value, default_flow_style=False).rstrip("\n")
    # raw
    if isinstance(value, (dict, list)):
        return _json.dumps(value, default=str)
    return str(value)


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
        "duho.Arg[str, duho.Choice('first', 'unique', 'hash', 'deep', 'array', 'set')]"
    ) = "first"
    """Merge strategy across the hierarchy (default: first match wins).
    ``array``/``set`` are legacy aliases for ``unique``."""
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
        strategy = _MERGE_ALIASES.get(self.merge, self.merge)
        if strategy == "hash" and self.deep:
            strategy = "deep"
        if strategy == "first":
            return None
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
    return duho.main(Lookup, argv)


if __name__ == "__main__":
    _sys.exit(main())
