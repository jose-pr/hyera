# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb,
# lib/puppet/pops/issues.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Hiera 5 hiera.yaml schema validation.

Ports the ``validate_config`` checks of Puppet's ``pops/lookup/hiera_config.rb``
with the messages ``issues.rb`` defines.
"""

from __future__ import annotations

import re
import typing as _ty

import yaml

from ..backends import has_hocon
from .config_source import _ConfigSource, _config_error, _ruby_type_name, _type_error

#: ``hiera_config.rb:71-73``.
_FUNCTION_KEYS = ("data_hash", "lookup_key", "data_dig", "hiera3_backend")
_ALL_FUNCTION_KEYS = _FUNCTION_KEYS + ("v4_data_hash",)
_LOCATION_KEYS = ("path", "paths", "glob", "globs", "uri", "uris", "mapped_paths")
#: ``hiera_config.rb:726``.
_RESERVED_OPTION_KEYS = ("path", "uri")
#: Puppet's option-name pattern (``hiera_config.rb:580``), kept verbatim --
#: including its own ``(:?`` (an optional literal ``:``, not a non-capturing
#: group). Matched with ``fullmatch``, so no ``\A``/``\z`` anchors needed.
_OPTION_NAME_RE = re.compile(r"[A-Za-z](:?[0-9A-Za-z_-]*[0-9A-Za-z])?")

_TOP_KEYS = ("version", "defaults", "hierarchy", "plan_hierarchy", "default_hierarchy")
_DEFAULTS_KEYS = ("data_hash", "lookup_key", "data_dig", "datadir", "options")
_ENTRY_KEYS = ("name", "options", "datadir") + _ALL_FUNCTION_KEYS + _LOCATION_KEYS


def _is_data(value) -> bool:
    """Puppet's ``Data`` type: ``Undef``, ``Boolean``, ``Numeric``, ``String``,
    an ``Array`` of ``Data``, or a ``String``-keyed ``Hash`` of ``Data``."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_data(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_data(v) for k, v in value.items())
    return False


def _where(path: "_ty.Tuple") -> str:
    """Puppet's dotted-path rendering: ``("hierarchy", 0, "paths", 1)`` ->
    ``"entry 'hierarchy' index 0 entry 'paths' index 1"``."""
    return " ".join(
        "index {}".format(step) if isinstance(step, int) else "entry '{}'".format(step)
        for step in path
    )


def _msg(where: "_ty.Tuple", tail: str) -> str:
    prefix = _where(where)
    return "{} {}".format(prefix, tail) if prefix else tail


def _config_line(text, where: "_ty.Tuple", *, key: bool = False):
    """The 1-based line of the node at ``where`` in ``text`` (a hiera.yaml's
    raw source): a mapping key when ``key=True``, else its value (or a
    sequence item for an ``int`` step). ``None`` when there is no text, on a
    YAML error, or when ``where`` does not resolve (compose only runs when
    actually needed -- the happy path never pays for this).
    """
    if not text or not where:
        return None
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None

    def _step(node, step):
        if isinstance(step, int):
            if not isinstance(node, yaml.SequenceNode):
                return None
            try:
                return (None, node.value[step])
            except IndexError:
                return None
        if not isinstance(node, yaml.MappingNode):
            return None
        for k_node, v_node in node.value:
            if getattr(k_node, "value", None) == step:
                return (k_node, v_node)
        return None

    for step in where[:-1]:
        found = _step(node, step)
        if found is None:
            return None
        node = found[1]

    found = _step(node, where[-1])
    if found is None:
        return None
    # `_step` returns (None, an item node) for an int step and (a key node, a value
    # node) for a mapping entry, so `target` is never None here.
    k_node, v_node = found
    target = k_node if key and k_node is not None else v_node
    return target.start_mark.line + 1


def _check_string(value, where: "_ty.Tuple", source, *, nonempty: bool = False) -> None:
    if not isinstance(value, str):
        raise _type_error(
            source,
            _msg(
                where, "expects a String value, got {}".format(_ruby_type_name(value))
            ),
            line=_config_line(source.text, where),
        )
    if nonempty and value == "":
        raise _type_error(
            source,
            _msg(where, "expects a String[1] value, got String"),
            line=_config_line(source.text, where),
        )


def _check_string_array(
    value, where: "_ty.Tuple", source, *, size: int = None, min_size: int = None
) -> None:
    if not isinstance(value, list):
        raise _type_error(
            source,
            _msg(
                where, "expects an Array value, got {}".format(_ruby_type_name(value))
            ),
            line=_config_line(source.text, where),
        )
    if size is not None and len(value) != size:
        raise _type_error(
            source,
            _msg(where, "expects size to be {}, got {}".format(size, len(value))),
            line=_config_line(source.text, where),
        )
    if min_size is not None and len(value) < min_size:
        raise _type_error(
            source,
            _msg(
                where,
                "expects size to be at least {}, got {}".format(min_size, len(value)),
            ),
            line=_config_line(source.text, where),
        )
    for i, item in enumerate(value):
        _check_string(item, where + (i,), source, nonempty=True)


def _check_options(value, where: "_ty.Tuple", source) -> None:
    if not isinstance(value, dict):
        raise _type_error(
            source,
            _msg(where, "expects a Hash value, got {}".format(_ruby_type_name(value))),
            line=_config_line(source.text, where),
        )
    for k, v in value.items():
        if not isinstance(k, str) or not _OPTION_NAME_RE.fullmatch(k):
            raise _type_error(
                source,
                _msg(
                    where,
                    "key of entry '{}' expects a match for Pattern[/\\A[A-Za-z]"
                    "(:?[0-9A-Za-z_-]*[0-9A-Za-z])?\\z/], got '{}'".format(k, k),
                ),
                line=_config_line(source.text, where + (k,), key=True),
            )
        if not _is_data(v):
            raise _type_error(
                source,
                _msg(
                    where,
                    "entry '{}' expects a Data value, got {}".format(
                        k, _ruby_type_name(v)
                    ),
                ),
                line=_config_line(source.text, where + (k,)),
            )


def _check_entry(entry, where: "_ty.Tuple", source) -> None:
    if not isinstance(entry, dict):
        raise _type_error(
            source,
            _msg(
                where, "expects a Struct value, got {}".format(_ruby_type_name(entry))
            ),
            line=_config_line(source.text, where),
        )
    for k in entry:
        if k not in _ENTRY_KEYS:
            raise _type_error(
                source,
                _msg(where, "unrecognized key '{}'".format(k)),
                line=_config_line(source.text, where + (k,), key=True),
            )
    if "name" not in entry:
        raise _type_error(
            source,
            _msg(where, "expects a value for key 'name'"),
            line=_config_line(source.text, where),
        )
    # Every key here is in _ENTRY_KEYS (checked above) and the branches below cover
    # _ENTRY_KEYS exactly, so the loop never falls through unmatched.
    for key, value in entry.items():
        if key == "name":
            _check_string(value, where + ("name",), source, nonempty=True)
        elif key == "datadir":
            _check_string(value, where + ("datadir",), source, nonempty=True)
        elif key == "options":
            _check_options(value, where + ("options",), source)
        elif key in ("path", "glob", "uri"):
            _check_string(value, where + (key,), source, nonempty=True)
        elif key in ("paths", "globs", "uris"):
            _check_string_array(value, where + (key,), source, min_size=1)
        elif key == "mapped_paths":
            _check_string_array(value, where + (key,), source, size=3)
        elif key in _ALL_FUNCTION_KEYS:
            _check_string(value, where + (key,), source, nonempty=True)


def _check_hierarchy_type(value, where: "_ty.Tuple", source) -> None:
    if not isinstance(value, list):
        raise _type_error(
            source,
            _msg(
                where, "expects an Array value, got {}".format(_ruby_type_name(value))
            ),
            line=_config_line(source.text, where),
        )
    for i, entry in enumerate(value):
        _check_entry(entry, where + (i,), source)


def _check_defaults_type(value, where: "_ty.Tuple", source) -> None:
    if not isinstance(value, dict):
        raise _type_error(
            source,
            _msg(
                where, "expects a Struct value, got {}".format(_ruby_type_name(value))
            ),
            line=_config_line(source.text, where),
        )
    for k in value:
        if k not in _DEFAULTS_KEYS:
            raise _type_error(
                source,
                _msg(where, "unrecognized key '{}'".format(k)),
                line=_config_line(source.text, where + (k,), key=True),
            )
    # Every key here is in _DEFAULTS_KEYS (checked above) and the branches below cover
    # it exactly (hiera3_backend never reaches here: see _function_of).
    for key, v in value.items():
        if key == "datadir":
            _check_string(v, where + ("datadir",), source, nonempty=True)
        elif key == "options":
            _check_options(v, where + ("options",), source)
        elif key in _FUNCTION_KEYS:
            _check_string(v, where + (key,), source, nonempty=True)


def _check_top(data: dict, source: "_ConfigSource") -> None:
    for k in data:
        if k not in _TOP_KEYS:
            raise _type_error(
                source,
                _msg((), "unrecognized key '{}'".format(k)),
                line=_config_line(source.text, (k,), key=True),
            )
    # Every key here is in _TOP_KEYS (checked above) and the branches below cover it
    # exactly, so the loop never falls through unmatched.
    for key, value in data.items():
        if key == "version":
            continue  # already validated by _config_version
        if key == "defaults":
            _check_defaults_type(value, ("defaults",), source)
        elif key in ("hierarchy", "plan_hierarchy", "default_hierarchy"):
            _check_hierarchy_type(value, (key,), source)


def _validate_defaults_issues(defaults: dict, source: "_ConfigSource") -> None:
    """``validate_defaults`` (``hiera_config.rb:802-816``).

    ``hiera3_backend`` is one of ``_FUNCTION_KEYS`` but not of
    ``_DEFAULTS_KEYS``, so ``defaults`` can never actually carry it by the
    time this runs -- ``_check_defaults_type`` already rejected it as an
    unrecognized key. This mirrors Puppet's own ``validate_defaults``
    exactly: its ``FUNCTION_KEYS`` list (and so this error's own message
    text) names ``hiera3_backend`` too, even though Puppet's own
    ``defaults`` struct type excludes it the same way (``hiera_config.rb``'s
    ``@@CONFIG_TYPE``; conformance case ``config-defaults-hiera3-backend-key``)
    -- kept as written, rather than narrowed, to stay a literal port.
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
    Puppet's own order: the whole-document type pass, then ``defaults``'
    issues, then each hierarchy's issues, then duplicate names. Reports only
    the first mismatch; assumes :func:`_fill_v5_defaults`
    already ran, so ``defaults``/``hierarchy`` are present.

    ``layer`` (``"global"``/``"environment"``/``"module"``) gates two rules
    that differ by layer here: ``hiera3_backend`` is global-only, and
    ``default_hierarchy`` is module-only (``hiera_config.rb:754-757``) --
    checked right after ``hierarchy``/``plan_hierarchy`` validate and
    before ``default_hierarchy``'s own entries do, so a
    non-module-owned ``default_hierarchy`` is rejected before its
    (possibly also invalid) entries are ever inspected.
    """
    _check_top(data, source)
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
