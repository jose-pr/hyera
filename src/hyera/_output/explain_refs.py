# Ported from Puppet 8 lib/puppet/pops/lookup/explainer.rb, configured_data_provider.rb,
# hiera_config.rb (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by
# jose-pr. See NOTICE.
"""The provider and location references an explain tree node carries, and the
value dump every node renders through.

Ports ``configured_data_provider.rb``'s provider name, ``ResolvedLocation``
and ``ExplainTreeNode#dump_value`` of ``explainer.rb``.
"""

from __future__ import annotations

import typing as _ty

from pathlib_next import Path

from .._lookup.interpolation import _ruby_inspect


class _ProviderRef(_ty.NamedTuple):
    """A ``data_provider`` qualifier: Puppet's ``configured_data_provider.rb``
    provider object, reduced to what the renderer ever reads from it."""

    name: str
    config_path: _ty.Optional[str] = None
    module_name: _ty.Optional[str] = None


def _provider_ref(provider) -> _ProviderRef:
    """A :class:`~hyera._output.explain_refs._ProviderRef` for one layer's provider
    (``configured_data_provider.rb:33-39``, ``hiera_config.rb:284-286``):
    ``Global``/``Environment Data Provider (hiera configuration version
    N)``, or ``Module "<m>" Data Provider (...)``; a config path (never for
    ``Hiera(None, ...)``'s built-in default) renders POSIX."""
    if provider.place == "Module":
        name = 'Module "{}" Data Provider (hiera configuration version {})'.format(
            provider.module_name, provider.version
        )
    else:
        name = "{} Data Provider (hiera configuration version {})".format(
            provider.place, provider.version
        )
    path = provider.source.path if provider.source is not None else None
    config_path = None if path is None else Path(path).as_posix()
    module_name = provider.module_name if provider.place == "Module" else None
    return _ProviderRef(name, config_path, module_name)


class _LocationRef(_ty.NamedTuple):
    """A ``location`` qualifier: Puppet's ``ResolvedLocation``, reduced the
    same way. ``kind`` is ``"path"`` or ``"uri"``."""

    original_location: str
    location: str
    kind: str = "path"


def _dump_value(parts: list, indent: str, value) -> None:
    """``ExplainTreeNode#dump_value`` (``explainer.rb:125-156``): a Hash or
    Array gets its own multi-line, two-space-per-level layout; anything else
    renders through the one Ruby ``inspect`` port already shared with
    interpolation (:func:`hyera._lookup.interpolation._ruby_inspect`), so the two
    can never disagree on how a scalar prints.
    """
    if isinstance(value, dict):
        if not value:
            parts.append("{}")
            return
        inner = indent + "  "
        parts.append("{")
        first = True
        for k, v in value.items():
            parts.append("\n" if first else ",\n")
            first = False
            parts.append(inner)
            _dump_value(parts, inner, k)
            parts.append(" => ")
            _dump_value(parts, inner, v)
        parts.append("\n")
        parts.append(indent)
        parts.append("}")
    elif isinstance(value, list):
        if not value:
            parts.append("[]")
            return
        inner = indent + "  "
        parts.append("[")
        first = True
        for v in value:
            parts.append("\n" if first else ",\n")
            first = False
            parts.append(inner)
            _dump_value(parts, inner, v)
        parts.append("\n")
        parts.append(indent)
        parts.append("]")
    else:
        parts.append(_ruby_inspect(value))
