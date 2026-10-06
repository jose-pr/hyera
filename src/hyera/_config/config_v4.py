# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The deprecated Hiera 4 hiera.yaml dialect: defaults, validation and levels.

Ports ``HieraConfigV4`` of Puppet's ``pops/lookup/hiera_config.rb``.
"""

from __future__ import annotations

import re
import typing as _ty

from ..exceptions import ConfigError
from .config_source import _ConfigSource, _config_error, _ruby_type_name
from .config_v5 import _msg
from .config_v3 import _V3_NAME_RE_TEMPLATE, _find_line_matching, _v3_string_detail
from .hiera_config import _warn_deprecated
from .level_builder import _build_level

#: ``HieraConfigV4``'s own struct keys (``hiera_config.rb:489-507``), in
#: schema-declaration order.
_V4_TOP_KEYS = ("version", "datadir", "hierarchy")
#: A v4 hierarchy entry's struct keys, in schema-declaration order.
_V4_ENTRY_KEYS = ("backend", "name", "datadir", "path", "paths")
_V4_ENTRY_REQUIRED = ("backend", "name")


def _fill_v4_defaults(data: dict) -> None:
    """Puppet's v4 ``||=`` fill (``HieraConfigV4#validate_config``,
    ``hiera_config.rb:558-559``)."""
    if data.get("datadir") is None or data.get("datadir") is False:
        data["datadir"] = "data"
    if data.get("hierarchy") is None or data.get("hierarchy") is False:
        data["hierarchy"] = [{"name": "common", "backend": "yaml"}]


def _validate_v4(data: dict, source: "_ConfigSource") -> None:
    """Every Puppet v4 schema mismatch, in struct-declaration order
    (``HieraConfigV4::CONFIG_TYPE``, ``hiera_config.rb:489-507``) -- like
    :func:`_validate_v3`, every mismatch is reported, not just the first
    (a nested per-entry mismatch line always precedes a top-level
    "unrecognized key" line, since entries are walked before the top-level
    key scan below). Assumes :func:`_fill_v4_defaults` already ran.
    """
    details: "list" = []

    v = data.get("version")
    if isinstance(v, bool) or not isinstance(v, int):
        details.append(
            _msg(
                ("version",),
                "expects an Integer value, got {}".format(_ruby_type_name(v)),
            )
        )

    detail = _v3_string_detail(data.get("datadir"))
    if detail:
        details.append(_msg(("datadir",), detail))

    hierarchy = data.get("hierarchy")
    if not isinstance(hierarchy, list):
        details.append(
            _msg(
                ("hierarchy",),
                "expects an Array value, got {}".format(_ruby_type_name(hierarchy)),
            )
        )
    else:
        for i, entry in enumerate(hierarchy):
            if not isinstance(entry, dict):
                details.append(
                    _msg(
                        ("hierarchy", i),
                        "expects a Struct value, got {}".format(_ruby_type_name(entry)),
                    )
                )
                continue
            for key in _V4_ENTRY_REQUIRED:
                if key not in entry:
                    details.append(
                        _msg(
                            ("hierarchy", i),
                            "expects a value for key '{}'".format(key),
                        )
                    )
            for key in ("backend", "name", "datadir", "path"):
                if key not in entry:
                    continue
                detail = _v3_string_detail(entry[key])
                if detail:
                    details.append(_msg(("hierarchy", i, key), detail))
            if "paths" in entry:
                pv = entry["paths"]
                if not isinstance(pv, list):
                    details.append(
                        _msg(
                            ("hierarchy", i, "paths"),
                            "expects an Array value, got {}".format(
                                _ruby_type_name(pv)
                            ),
                        )
                    )
                else:
                    for j, item in enumerate(pv):
                        detail = _v3_string_detail(item)
                        if detail:
                            details.append(_msg(("hierarchy", i, "paths", j), detail))
            for key in entry:
                if key not in _V4_ENTRY_KEYS:
                    details.append(
                        _msg(("hierarchy", i), "unrecognized key '{}'".format(key))
                    )

    for k in data:
        if k not in _V4_TOP_KEYS:
            details.append(_msg((), "unrecognized key '{}'".format(k)))

    if not details:
        return
    label = source.label if source else "<dict>"
    message = "The Lookup Configuration at '{}' has wrong type, {}".format(
        label, "\n".join(details)
    )
    raise ConfigError(message, path=source.path if source else None)


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
        # v4's datadir is joined onto the config root literally, never interpolated
        # (`hiera_config.rb:525`, unlike v5's `:664-665`): `datadir_literal` makes the resolver
        # skip it, so a literal '%' survives (`%{literal('%')}` is itself a disallowed method call).
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
