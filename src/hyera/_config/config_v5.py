# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb,
# lib/puppet/pops/issues.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Hiera 5 hiera.yaml schema validation.

Ports the ``validate_config`` checks of Puppet's ``pops/lookup/hiera_config.rb``
with the messages ``issues.rb`` defines: the type of the whole document is checked
by the package's own type describer, the rest by hand.
"""

from __future__ import annotations

import typing as _ty

from .._types.parser import parse_type
from ..backends import has_hocon
from .config_source import (
    _check_config_type,
    _config_error,
    _config_line,
    _ConfigSource,
)

#: ``hiera_config.rb:71-73``.
_FUNCTION_KEYS = ("data_hash", "lookup_key", "data_dig", "hiera3_backend")
_ALL_FUNCTION_KEYS = _FUNCTION_KEYS + ("v4_data_hash",)
_LOCATION_KEYS = ("path", "paths", "glob", "globs", "uri", "uris", "mapped_paths")
#: ``hiera_config.rb:726``.
_RESERVED_OPTION_KEYS = ("path", "uri")


def _where(path: "_ty.Tuple") -> str:
    return " ".join(
        "index {}".format(step) if isinstance(step, int) else "entry '{}'".format(step)
        for step in path
    )


def _msg(where: "_ty.Tuple", tail: str) -> str:
    prefix = _where(where)
    return "{} {}".format(prefix, tail) if prefix else tail


def _struct(*members: str) -> str:
    return "Struct[{" + ", ".join(members) + "}]"


def _optional(key: str, type_: str) -> str:
    return "Optional[{}] => {}".format(key, type_)


#: ``hiera_config.rb:574``, the ``nes_t`` of every string the schema names.
_NES = "String[1]"
#: ``hiera_config.rb:580``, kept verbatim including its own ``(:?`` (an optional
#: literal ``:``, not a non-capturing group).
_OPTION_NAME = r"Pattern[/\A[A-Za-z](:?[0-9A-Za-z_-]*[0-9A-Za-z])?\z/]"
_OPTIONS = "Hash[{}, Data]".format(_OPTION_NAME)

#: ``hiera_config.rb:582-601``.
_HIERARCHY = "Array[{}]".format(
    _struct(
        "name => " + _NES,
        _optional("options", _OPTIONS),
        _optional("data_hash", _NES),
        _optional("lookup_key", _NES),
        _optional("hiera3_backend", _NES),
        _optional("v4_data_hash", _NES),
        _optional("data_dig", _NES),
        _optional("path", _NES),
        _optional("paths", "Array[{}, 1]".format(_NES)),
        _optional("glob", _NES),
        _optional("globs", "Array[{}, 1]".format(_NES)),
        _optional("uri", _NES),
        _optional("uris", "Array[{}, 1]".format(_NES)),
        _optional("mapped_paths", "Array[{}, 3, 3]".format(_NES)),
        _optional("datadir", _NES),
    )
)

#: ``hiera_config.rb:603-618``.
_CONFIG_TYPE = parse_type(
    _struct(
        "version => Integer[5, 5]",
        _optional(
            "defaults",
            _struct(
                _optional("data_hash", _NES),
                _optional("lookup_key", _NES),
                _optional("data_dig", _NES),
                _optional("datadir", _NES),
                _optional("options", _OPTIONS),
            ),
        ),
        _optional("hierarchy", _HIERARCHY),
        _optional("plan_hierarchy", _HIERARCHY),
        _optional("default_hierarchy", _HIERARCHY),
    )
)


def _validate_defaults_issues(defaults: dict, source: "_ConfigSource") -> None:
    """``validate_defaults`` (``hiera_config.rb:802-816``).

    ``hiera3_backend`` is one of ``_FUNCTION_KEYS`` but not a key of the
    ``defaults`` struct, so ``defaults`` can never actually carry it by the
    time this runs -- the type check already rejected it as an unrecognized
    key. This mirrors Puppet's own ``validate_defaults`` exactly: its
    ``FUNCTION_KEYS`` list (and so this error's own message text) names
    ``hiera3_backend`` too, even though Puppet's own ``defaults`` struct type
    excludes it the same way (``hiera_config.rb``'s ``@@CONFIG_TYPE``;
    conformance case ``config-defaults-hiera3-backend-key``) -- kept as
    written, rather than narrowed, to stay a literal port.
    """
    if sum(1 for k in _FUNCTION_KEYS if k in defaults) > 1:
        raise _config_error(
            source,
            "Only one of data_hash, lookup_key, data_dig, or hiera3_backend can "
            "be defined in defaults",
        )
    options = defaults.get("options")
    if isinstance(options, dict):
        for reserved in _RESERVED_OPTION_KEYS:
            if reserved in options:
                raise _config_error(
                    source,
                    "Option key '{}' used in defaults is reserved by Puppet".format(
                        reserved
                    ),
                )


def _validate_hierarchy_issues(
    entries: list,
    area: str,
    defaults: dict,
    source: "_ConfigSource",
    *,
    layer: str = "global",
) -> None:
    """``validate_hierarchy`` (``hiera_config.rb:764-800``), for ``hierarchy``,
    ``plan_hierarchy`` or ``default_hierarchy`` in turn."""
    defaults_has_function = any(k in defaults for k in _FUNCTION_KEYS)
    for i, entry in enumerate(entries):
        name = entry.get("name")
        functions_present = [k for k in _ALL_FUNCTION_KEYS if k in entry]
        if not functions_present:
            if not defaults_has_function:
                raise _config_error(
                    source,
                    "One of data_hash, lookup_key, data_dig, or hiera3_backend "
                    "must be defined in hierarchy '{}'".format(name),
                )
        elif len(functions_present) > 1:
            raise _config_error(
                source,
                "Only one of data_hash, lookup_key, data_dig, or hiera3_backend "
                "can be defined in hierarchy '{}'".format(name),
            )
        if "hiera3_backend" in entry:
            if layer != "global":
                # issues.rb:833-835 -- checked before the replaced-by-
                # data_hash rule below, and applies regardless of whether
                # this backend name has a data_hash replacement.
                raise _config_error(
                    source,
                    "'hiera3_backend' is only allowed in the global layer",
                    line=_config_line(
                        source.text, (area, i, "hiera3_backend"), key=True
                    ),
                )
            backend_name = entry["hiera3_backend"]
            replaceable = backend_name in ("json", "yaml") or (
                backend_name == "hocon" and has_hocon()
            )
            if replaceable:
                raise _config_error(
                    source,
                    'Use "data_hash: {0}_data" instead of "hiera3_backend: {0}"'.format(
                        backend_name
                    ),
                    line=_config_line(
                        source.text, (area, i, "hiera3_backend"), key=True
                    ),
                )
        location_present = [k for k in _LOCATION_KEYS if k in entry]
        if len(location_present) > 1:
            raise _config_error(
                source,
                "Only one of path, paths, glob, globs, uri, uris, or "
                "mapped_paths can be defined in hierarchy '{}'".format(name),
            )
        options = entry.get("options")
        if isinstance(options, dict):
            for reserved in _RESERVED_OPTION_KEYS:
                if reserved in options:
                    raise _config_error(
                        source,
                        "Option key '{}' used in hierarchy '{}' is reserved by "
                        "Puppet".format(reserved, name),
                    )


def _check_duplicate_names(entries: list, area: str, source: "_ConfigSource") -> None:
    """Duplicate ``name`` within ``hierarchy``/``default_hierarchy`` only
    (``plan_hierarchy`` is type-checked but never dup-checked),
    at provider-creation time (``hiera_config.rb:645-655``)."""
    seen = {}
    for i, entry in enumerate(entries):
        name = entry.get("name")
        if name in seen:
            message = "Hierarchy name '{}' defined more than once.".format(name)
            first_line = _config_line(source.text, (area, seen[name], "name"))
            if first_line:
                message += " First defined at (line: {})".format(first_line)
            raise _config_error(
                source, message, line=_config_line(source.text, (area, i, "name"))
            )
        seen[name] = i


def _validate_v5(data: dict, source: "_ConfigSource", *, layer: str = "global") -> None:
    """Validate ``data`` against Puppet's hiera.yaml version 5 schema, in
    Puppet's own order: the whole-document type pass (every mismatch is
    reported), then ``defaults``' issues, then each hierarchy's issues, then
    duplicate names. Assumes :func:`_fill_v5_defaults` already ran, so
    ``defaults``/``hierarchy`` are present.

    ``layer`` (``"global"``/``"environment"``/``"module"``) gates two rules
    that differ by layer here: ``hiera3_backend`` is global-only, and
    ``default_hierarchy`` is module-only (``hiera_config.rb:754-757``) --
    checked right after ``hierarchy``/``plan_hierarchy`` validate and
    before ``default_hierarchy``'s own entries do, so a
    non-module-owned ``default_hierarchy`` is rejected before its
    (possibly also invalid) entries are ever inspected.
    """
    _check_config_type(source, _CONFIG_TYPE, data)
    defaults = data.get("defaults") or {}
    _validate_defaults_issues(defaults, source)
    _validate_hierarchy_issues(
        data.get("hierarchy") or [], "hierarchy", defaults, source, layer=layer
    )
    _validate_hierarchy_issues(
        data.get("plan_hierarchy") or [],
        "plan_hierarchy",
        defaults,
        source,
        layer=layer,
    )
    if "default_hierarchy" in data and layer != "module":
        raise _config_error(
            source,
            "'default_hierarchy' is only allowed in the module layer",
            _config_line(source.text, ("default_hierarchy",), key=True),
        )
    _validate_hierarchy_issues(
        data.get("default_hierarchy") or [],
        "default_hierarchy",
        defaults,
        source,
        layer=layer,
    )
    _check_duplicate_names(data.get("hierarchy") or [], "hierarchy", source)
    _check_duplicate_names(
        data.get("default_hierarchy") or [], "default_hierarchy", source
    )
