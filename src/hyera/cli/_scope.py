"""Scope, facts and path-list handling for the command line."""

from __future__ import annotations

import os as _os
import socket as _socket
import typing as _ty

from ..backends._base import Backend
from .._scope.facts import load_facts
from .._scope.scope import Scope

#: The puppet lookup release this CLI's flags and $server_facts
#: mirror. Not read from anywhere else -- there is no local Puppet to ask.
_PUPPET_VERSION = "8.10.0"


class _UsageError(ValueError):
    """Puppet's Could not run: ... text: reported bare, no key prefix."""


class _ScopeError(_UsageError):
    """A --scope entry was not in key=value form, or its name is invalid."""

    def __init__(self, item: str, reason: str = "expected key=value"):
        super().__init__("invalid --scope {!r} ({})".format(item, reason))


def _parse_scope_value(text: str):
    """A --scope value is YAML; an empty value is None (not the
    yaml_data loader's own empty-document False)."""
    if text == "":
        return None
    return Backend.new("yaml", kind="format").loads(text)


def _parse_scope(items: "_ty.Iterable[str]") -> dict:
    """Parse NAME=VALUE scope entries into a node-parameters dict.

    NAME may be dotted (os.family=RedHat) to build a nested hash;
    later items win, and a segment landing under an already-scalar value
    raises. An empty NAME, or an item with no = at all, raises
    _ScopeError.
    """
    result: dict = {}
    for item in items or ():
        if not item:
            continue
        if "=" not in item:
            raise _ScopeError(item)
        name, _eq, value = item.partition("=")
        if not name:
            raise _ScopeError(item)
        parsed = _parse_scope_value(value)
        segments = name.split(".")
        target = result
        prefix: list = []
        for seg in segments[:-1]:
            prefix.append(seg)
            existing = target.get(seg)
            if existing is None:
                existing = {}
                target[seg] = existing
            elif not isinstance(existing, dict):
                raise _ScopeError(item, "{!r} is not a hash".format(".".join(prefix)))
            target = existing
        target[segments[-1]] = parsed
    return result


def _paths(value):
    """Split a Puppet path-list setting (os.pathsep-joined) into
    absolute paths; None stays None (the flag was not given)."""
    if value is None:
        return None
    return [_os.path.abspath(p) for p in value.split(_os.pathsep) if p]


def _build_scope(variables, facts_path, node, environment, strict) -> Scope:
    """The lookup scope: --scope variables over the facts file's facts.

    Raises _UsageError when no facts were given or the file holds none
    (Puppet's own "No facts available" case).
    """
    facts = load_facts(facts_path) if facts_path is not None else {}
    if not facts:
        # socket.getfqdn() is a live reverse-DNS lookup and can be slow
        # (60s+ hangs on macOS CI runners): only this message needs it.
        node_name = node or _socket.getfqdn().lower()
        raise _UsageError("No facts available for target node: {}".format(node_name))
    return Scope(
        variables=variables,
        facts=facts,
        server_facts={"serverversion": _PUPPET_VERSION},
        environment=environment,
        strict=strict or "warning",
        node_name=node,
    )
