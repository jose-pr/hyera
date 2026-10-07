# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The deprecated Hiera 4 hiera.yaml dialect: defaults, validation and levels.

Ports ``HieraConfigV4`` of Puppet's ``pops/lookup/hiera_config.rb``.
"""

from __future__ import annotations

import re
import typing as _ty

from .._types.parser import parse_type
from .config_source import (
    _NES,
    _check_config_type,
    _config_error,
    _ConfigSource,
    _optional,
    _struct,
)
from .config_v3 import _V3_NAME_RE_TEMPLATE, _find_line_matching
from .hiera_config import _warn_deprecated
from .level_builder import _build_level

#: ``hiera_config.rb:495-506``.
_V4_CONFIG_TYPE = parse_type(
    _struct(
        "version => Integer[4, 4]",
        _optional("datadir", _NES),
        _optional(
            "hierarchy",
            "Array[{}]".format(
                _struct(
                    "backend => " + _NES,
                    "name => " + _NES,
                    _optional("datadir", _NES),
                    _optional("path", _NES),
                    _optional("paths", "Array[{}]".format(_NES)),
                )
            ),
        ),
    )
)


def _fill_v4_defaults(data: dict) -> None:
    """Puppet's v4 ``||=`` fill (``HieraConfigV4#validate_config``,
    ``hiera_config.rb:558-559``)."""
    if data.get("datadir") is None or data.get("datadir") is False:
        data["datadir"] = "data"
    if data.get("hierarchy") is None or data.get("hierarchy") is False:
        data["hierarchy"] = [{"name": "common", "backend": "yaml"}]


def _validate_v4(data: dict, source: "_ConfigSource") -> None:
    """Every Puppet v4 schema mismatch, in struct-declaration order
    (``HieraConfigV4::CONFIG_TYPE``, ``hiera_config.rb:489-507``): all of them join
    into one :class:`ConfigError`. Assumes :func:`_fill_v4_defaults` already ran.
    """
    _check_config_type(source, _V4_CONFIG_TYPE, data, lines=False)


_V4_NAME_RE_TEMPLATE = r"\s+name:\s+['\"]?{}(?:[^\w]|$)"


def _v4_levels(data: dict, source: "_ConfigSource", backends, scope) -> "list":
    """One :class:`HieraLevel` per v4 hierarchy entry (``HieraConfigV4#
    create_configured_data_providers``, ``hiera_config.rb:509-551``):
    unlike v3, one provider *per entry*, the v5 shape. Assumes
    :func:`_fill_v4_defaults`/:func:`_validate_v4` already ran.
    """
    text = source.text
    config_datadir = data["datadir"]
    levels = []
    seen_at: "dict" = {}
    for i, entry in enumerate(data["hierarchy"]):
        name = entry["name"]
        name_re = _V4_NAME_RE_TEMPLATE.format(re.escape(name))
        if name in seen_at:
            first = seen_at[name]
            second = (
                _find_line_matching(text, name_re, start_line=first + 1)
                if first
                else None
            )
            message = "Hierarchy name '{}' defined more than once.".format(name)
            if second:
                raise _config_error(
                    source,
                    message + " First defined at (line: {})".format(first),
                    line=second,
                )
            raise _config_error(source, message, line=first)
        line = _find_line_matching(text, name_re)
        seen_at[name] = line

        backend = entry["backend"]
        if backend in ("yaml", "json"):
            kind, function, extension = (
                "data_hash",
                "{}_data".format(backend),
                ("." + backend),
            )
        elif backend == "hocon":
            kind, function, extension = "data_hash", "hocon_data", ".conf"
        else:
            backend_re = _V3_NAME_RE_TEMPLATE.format(re.escape(backend))
            raise _config_error(
                source,
                "No data provider is registered for backend '{}'".format(backend),
                line=_find_line_matching(text, backend_re, start_line=line or 1),
            )

        if "paths" in entry:
            locations = list(entry["paths"])
        else:
            locations = [entry.get("path") or name]
        # v4's datadir is joined onto the config root literally (`hiera_config.rb:525`,
        # unlike v5's `:664-665`): `datadir_literal` skips interpolation so a literal
        # '%' survives (`%{literal('%')}` is a disallowed call).
        datadir = entry.get("datadir", config_datadir)

        conf = {"name": name, "paths": locations, "datadir": datadir}
        levels.append(
            _build_level(
                conf,
                kind,
                function,
                backends,
                source,
                scope,
                extension=extension,
                datadir_base=None,
                datadir_literal=True,
                lenient_locations=False,
            )
        )
    return levels


def _read_v4(
    data: dict, source: "_ConfigSource", scope, backends
) -> "_ty.Tuple[list, list]":
    """Read a Hiera version 4 base config (``HieraConfigV4``,
    ``hiera_config.rb:488-566``): the deprecation warning, the ``||=``
    fill, full schema validation, then the per-entry provider build.
    Version 4 has no ``default_hierarchy`` concept either.
    """
    _warn_deprecated(source, 4, scope)
    _fill_v4_defaults(data)
    _validate_v4(data, source)
    return _v4_levels(data, source, backends, scope), []
