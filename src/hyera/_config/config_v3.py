# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb,
# pops/types/type_mismatch_describer.rb, util/run_mode.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr. See NOTICE.
"""The deprecated Hiera 3 hiera.yaml dialect: defaults, validation and levels.

Ports ``HieraConfigV3`` of Puppet's ``pops/lookup/hiera_config.rb``, with the
messages it takes from ``type_mismatch_describer.rb`` and the code directory of
``util/run_mode.rb``.
"""

from __future__ import annotations

import copy
import os
import re
import typing as _ty

from pathlib_next import Path

from .._lookup.interpolation import _to_puppet_str
from .._types.compound_types import Struct, StructElement
from .._types.mismatch import type_name_of
from .._types.parser import parse_type
from .config_source import (
    _NES,
    _check_config_type,
    _config_error,
    _ConfigSource,
    _optional,
    _struct,
    _type_error,
)
from .hiera_config import V3_DEFAULT_CONFIG_HASH, _warn_deprecated
from .level_builder import _build_level, _v3_backend_class


def _fill_v3_defaults(data: dict) -> None:
    """Puppet's v3 ``||=`` fill (``HieraConfigV3#validate_config``,
    ``hiera_config.rb:444-448``), run after :func:`_config_version` returns
    ``3``. ``is``, not ``==``, as :func:`_fill_v5_defaults` already notes."""
    if data.get("version") is None or data.get("version") is False:
        data["version"] = 3
    if data.get("backends") is None or data.get("backends") is False:
        data["backends"] = copy.deepcopy(V3_DEFAULT_CONFIG_HASH["backends"])
    if data.get("hierarchy") is None or data.get("hierarchy") is False:
        data["hierarchy"] = copy.deepcopy(V3_DEFAULT_CONFIG_HASH["hierarchy"])
    if data.get("merge_behavior") is None or data.get("merge_behavior") is False:
        data["merge_behavior"] = V3_DEFAULT_CONFIG_HASH["merge_behavior"]
    if (
        data.get("deep_merge_options") is None
        or data.get("deep_merge_options") is False
    ):
        data["deep_merge_options"] = {}


#: ``hiera_config.rb:362-369``: ``HieraConfigV3``'s own keys. A backend's config
#: key (``:yaml:``, ...) is added per name in ``backends`` (``:455-456``).
_V3_CONFIG_TYPE = parse_type(
    _struct(
        _optional("version", "Integer[3, 3]"),
        _optional("backends", "Variant[{0}, Array[{0}]]".format(_NES)),
        _optional("logger", _NES),
        _optional("merge_behavior", "Enum['deep', 'deeper', 'native']"),
        _optional(
            "deep_merge_options", "Hash[{}, Variant[String, Boolean]]".format(_NES)
        ),
        _optional("hierarchy", "Variant[{0}, Array[{0}]]".format(_NES)),
    )
)
#: ``hiera_config.rb:455``: a backend's own config.
_V3_BACKEND_CONFIG = parse_type("Hash[{}, Any]".format(_NES))


def _v3_backend_names(value) -> "_ty.List[str]":
    """Distinct backend names from a ``backends`` value, in first-appearance
    order -- ``[]`` when it is not validly shaped (the type check reports that).
    Used to know which dynamic per-backend config keys are allowed top-level keys
    here, and (a later phase) to build one provider per name."""
    if isinstance(value, str):
        items = [value] if value else []
    elif isinstance(value, list):
        items = [v for v in value if isinstance(v, str) and v]
    else:
        items = []
    seen: "list" = []
    for name in items:
        if name not in seen:
            seen.append(name)
    return seen


def _v3_config_type(data: dict) -> Struct:
    """The type of ``data``: :data:`_V3_CONFIG_TYPE` with an optional entry holding
    a ``Hash`` for each backend named in ``backends`` (``hiera_config.rb:450-456``)."""
    elements = {e.key: e for e in _V3_CONFIG_TYPE.elements}
    for name in _v3_backend_names(data.get("backends")):
        elements[name] = StructElement(name, True, _V3_BACKEND_CONFIG)
    return Struct(list(elements.values()))


def _validate_v3(data: dict, source: "_ConfigSource") -> None:
    """Every Puppet v3 schema mismatch, in Puppet's struct-declaration order
    (``HieraConfigV3::CONFIG_TYPE``): all of them join into one
    :class:`ConfigError`. Assumes :func:`_fill_v3_defaults` already ran, so
    ``version``/``backends``/``hierarchy``/``merge_behavior``/
    ``deep_merge_options`` are always present.
    """
    _check_config_type(source, _v3_config_type(data), data, lines=False)


def _default_codedir() -> Path:
    """Puppet's AIO ``$codedir`` default per platform (``util/run_mode.rb``),
    used only for a version 3 hierarchy's default per-backend ``datadir``.
    Not this box's own Fedora-patched ``/etc/puppet/code`` (the package's
    own override) -- goldens never rely on the default for that reason."""
    if os.name == "nt":
        return (
            Path(os.environ.get("ALLUSERSPROFILE", r"C:\ProgramData"))
            / "PuppetLabs"
            / "code"
        )
    return Path("/etc/puppetlabs/code")


def _find_line_matching(text, pattern, start_line: int = 1) -> "_ty.Optional[int]":
    """The first 1-based line number at or after ``start_line`` whose
    comment-stripped text matches ``pattern`` (``hiera_config.rb:226-250``'s
    ``find_line_matching``): a ``#`` inside a quoted string never starts a
    comment; quote tracking does not itself span lines. ``None`` without
    ``text`` or a match."""
    if not text:
        return None
    compiled = pattern if hasattr(pattern, "search") else re.compile(pattern)
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if lineno < start_line:
            continue
        in_single = in_double = False
        stripped = raw
        for i, ch in enumerate(raw):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                stripped = raw[:i]
                break
        if compiled.search(stripped):
            return lineno
    return None


_V3_NAME_RE_TEMPLATE = r"[^\w]{}(?:[^\w]|$)"


def _v3_level_specs(data: dict, source: "_ConfigSource", codedir: Path) -> "list":
    """One provider spec per distinct name in ``backends``, in list order
    (``create_configured_data_providers``, ``hiera_config.rb:372-431``).
    Assumes :func:`_fill_v3_defaults`/:func:`_validate_v3` already ran.
    Each spec is a dict: ``name``, ``datadir``, ``extension``,
    ``locations``, ``kind``, ``function``, ``options``, ``backend_cls``
    (``None`` for the four built-in mappings, else the class
    :func:`_v3_backend_class` resolved).
    """
    text = source.text
    raw_backends = data["backends"]
    names = [raw_backends] if isinstance(raw_backends, str) else list(raw_backends)
    raw_hierarchy = data["hierarchy"]
    locations = (
        [raw_hierarchy] if isinstance(raw_hierarchy, str) else list(raw_hierarchy)
    )

    specs = []
    first_line: "dict" = {}
    for b in names:
        name_re = _V3_NAME_RE_TEMPLATE.format(re.escape(b))
        if b in first_line:
            first = first_line[b]
            second = (
                _find_line_matching(text, name_re, start_line=first + 1)
                if first
                else None
            )
            message = "Backend '{}' is defined more than once.".format(b)
            if second:
                raise _config_error(
                    source,
                    message + " First defined at (line: {})".format(first),
                    line=second,
                )
            raise _config_error(source, message, line=first)
        line = _find_line_matching(text, name_re)
        first_line[b] = line

        conf = data.get(b) or {}
        if conf.get("datadir") is not None:
            datadir_value = conf["datadir"]
            if not isinstance(datadir_value, str):
                raise _type_error(
                    source,
                    "entry '{}' entry 'datadir' expects a String value, got "
                    "{}".format(b, type_name_of(datadir_value)),
                    line=line,
                )
            datadir = datadir_value
        else:
            datadir = "{}/environments/%{{::environment}}/hieradata".format(
                codedir.as_posix()
            )
        if conf.get("extension") is not None:
            extension = "." + _to_puppet_str(conf["extension"])
        elif b == "hocon":
            extension = ".conf"
        else:
            extension = "." + b
        options_without_datadir = {k: v for k, v in conf.items() if k != "datadir"}

        if b in ("yaml", "json"):
            kind, function, options, backend_cls = (
                "data_hash",
                "{}_data".format(b),
                {},
                None,
            )
        elif b == "hocon":
            kind, function, options, backend_cls = "data_hash", "hocon_data", {}, None
        elif b == "eyaml":
            kind, function, options, backend_cls = (
                "lookup_key",
                "eyaml_lookup_key",
                options_without_datadir,
                None,
            )
        else:
            backend_cls = _v3_backend_class(b, source, line)
            kind, function, options = "data_hash", b, options_without_datadir

        specs.append(
            {
                "name": b,
                "datadir": datadir,
                "extension": extension,
                "locations": locations,
                "kind": kind,
                "function": function,
                "options": options,
                "backend_cls": backend_cls,
            }
        )
    return specs


def _v3_levels(
    data: dict, source: "_ConfigSource", backends, scope, codedir: Path, cwd: Path
) -> "list":
    """One :class:`HieraLevel` per :func:`_v3_level_specs` entry
    (backend-major: one data source per backend, over the whole
    hierarchy), each rooted at ``cwd`` (a relative v3 datadir follows the
    process working directory, per Puppet's own ``Pathname(datadir)``)
    rather than the hiera.yaml directory."""
    levels = []
    for spec in _v3_level_specs(data, source, codedir):
        conf = {
            "name": spec["name"],
            "paths": spec["locations"],
            "datadir": spec["datadir"],
        }
        if spec["options"]:
            conf["options"] = spec["options"]
        levels.append(
            _build_level(
                conf,
                spec["kind"],
                spec["function"],
                backends,
                source,
                scope,
                extension=spec["extension"],
                datadir_base=cwd,
                backend_cls=spec["backend_cls"],
                lenient_locations=False,
            )
        )
    return levels


def _read_v3(
    data: dict,
    source: "_ConfigSource",
    scope,
    backends,
    codedir: Path,
    cwd: Path,
) -> "_ty.Tuple[list, list]":
    """Read a Hiera version 3 base config (``HieraConfigV3``,
    ``hiera_config.rb:350-485``): the deprecation warning, the ``||=``
    fill, full schema validation, then the backend-major provider build.
    Version 3 has no ``default_hierarchy`` concept, so the second element
    of the returned pair is always ``[]``.
    """
    _warn_deprecated(source, 3, scope)
    _fill_v3_defaults(data)
    _validate_v3(data, source)
    return _v3_levels(data, source, backends, scope, codedir, cwd), []
