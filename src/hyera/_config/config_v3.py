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
from ..exceptions import ConfigError
from .config_source import _ConfigSource, _config_error, _ruby_type_name, _type_error
from .config_v5 import _msg
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


#: ``HieraConfigV3``'s own struct keys (``hiera_config.rb:355-370``), in schema order,
#: which is the order :func:`_validate_v3` reports mismatches in. A backend's config key
#: (``:yaml:``, ...) is added per name in ``backends`` (``:397``), so it is not listed.
_V3_TOP_KEYS = (
    "version",
    "backends",
    "logger",
    "merge_behavior",
    "deep_merge_options",
    "hierarchy",
)
_V3_MERGE_BEHAVIORS = ("deep", "deeper", "native")


def _v3_string_detail(value) -> "_ty.Optional[str]":
    """Puppet's ``String[1]`` mismatch detail, or ``None``."""
    if not isinstance(value, str):
        return "expects a String value, got {}".format(_ruby_type_name(value))
    if value == "":
        return "expects a String[1] value, got String"
    return None


def _v3_string_or_array_details(value) -> "_ty.List[str]":
    """Puppet's ``Variant[String[1], Array[String[1]]]`` mismatch detail(s)
    (``backends``/``hierarchy``), unwrapped (the caller applies ``entry
    '<name>'`` via :func:`_msg`). A structurally-wrong value (not a String,
    not an Array) merges into one "expects a value of type ... or ..." line;
    an Array with bad items instead names BOTH failing variants, one line
    per failing item, exactly as Puppet's ``TypeMismatchDescriber`` does for
    a ``Variant`` whose value is at least shaped like one of its members
    (probed: ``[5, common]`` -> two lines, one naming the Array variant's
    own bad index)."""
    if isinstance(value, str):
        if value == "":
            return ["expects a value of type String[1] or Array[String[1]], got String"]
        return []
    if isinstance(value, list):
        bad = [
            (i, item)
            for i, item in enumerate(value)
            if not isinstance(item, str) or item == ""
        ]
        if not bad:
            return []
        lines = ["variant 0 expects a String value, got Tuple"]
        for i, item in bad:
            if not isinstance(item, str):
                lines.append(
                    "variant 1 index {} expects a String value, got {}".format(
                        i, _ruby_type_name(item)
                    )
                )
            else:
                lines.append(
                    "variant 1 index {} expects a String[1] value, got String".format(i)
                )
        return lines
    return [
        "expects a value of type String or Array, got {}".format(_ruby_type_name(value))
    ]


def _v3_merge_behavior_detail(value) -> "_ty.Optional[str]":
    """``Enum['deep', 'deeper', 'native']`` mismatch detail, or ``None``."""
    enum = "Enum['deep', 'deeper', 'native']"
    if isinstance(value, str):
        if value in _V3_MERGE_BEHAVIORS:
            return None
        return "expects a match for {}, got '{}'".format(enum, value)
    return "expects a match for {}, got {}".format(enum, _ruby_type_name(value))


def _v3_backend_names(value) -> "_ty.List[str]":
    """Distinct backend names from a ``backends`` value, in first-appearance
    order -- ``[]`` when it is not validly shaped (its own mismatch is
    reported by :func:`_v3_string_or_array_details` instead). Used both to
    know which dynamic per-backend config keys are allowed top-level keys
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


def _validate_v3(data: dict, source: "_ConfigSource") -> None:
    """Every Puppet v3 schema mismatch, in Puppet's struct-declaration order
    (``HieraConfigV3::CONFIG_TYPE``, ``hiera_config.rb:355-370``, walked by
    ``pops/types/type_mismatch_describer.rb``'s ``describe_PStructType``):
    each mismatch renders as its own "The Lookup Configuration ... has
    wrong type, ..." detail line; all of them join with ``"\\n"`` into one
    :class:`ConfigError` (unlike v5's :func:`_validate_v5`, which reports
    only the first: a versionless v5-shaped file needs every mismatch
    visible in one pass to be fixable). Assumes :func:`_fill_v3_defaults`
    already ran, so ``version``/``backends``/``hierarchy``/
    ``merge_behavior``/``deep_merge_options`` are always present.
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

    backends = data.get("backends")
    for detail in _v3_string_or_array_details(backends):
        details.append(_msg(("backends",), detail))

    if "logger" in data:
        detail = _v3_string_detail(data["logger"])
        if detail:
            details.append(_msg(("logger",), detail))

    detail = _v3_merge_behavior_detail(data.get("merge_behavior"))
    if detail:
        details.append(_msg(("merge_behavior",), detail))

    dmo = data.get("deep_merge_options")
    if not isinstance(dmo, dict):
        details.append(
            _msg(
                ("deep_merge_options",),
                "expects a Hash value, got {}".format(_ruby_type_name(dmo)),
            )
        )
    else:
        for k, val in dmo.items():
            if not isinstance(k, str) or k == "":
                details.append(
                    _msg(
                        ("deep_merge_options",),
                        "key of entry '{}' expects a String[1] value, got {}".format(
                            k, _ruby_type_name(k)
                        ),
                    )
                )
                continue
            if not isinstance(val, (str, bool)):
                details.append(
                    _msg(
                        ("deep_merge_options", k),
                        "expects a value of type String or Boolean, got {}".format(
                            _ruby_type_name(val)
                        ),
                    )
                )

    hierarchy = data.get("hierarchy")
    for detail in _v3_string_or_array_details(hierarchy):
        details.append(_msg(("hierarchy",), detail))

    backend_names = _v3_backend_names(backends)
    for name in backend_names:
        if name not in data:
            continue
        conf = data[name]
        if not isinstance(conf, dict):
            details.append(
                _msg(
                    (name,),
                    "expects a Hash value, got {}".format(_ruby_type_name(conf)),
                )
            )
            continue
        for k in conf:
            if not isinstance(k, str) or k == "":
                details.append(
                    _msg(
                        (name,),
                        "key of entry '{}' expects a String[1] value, got {}".format(
                            k, _ruby_type_name(k)
                        ),
                    )
                )

    allowed = set(_V3_TOP_KEYS) | set(backend_names)
    for k in data:
        if k not in allowed:
            details.append(_msg((), "unrecognized key '{}'".format(k)))

    if not details:
        return
    label = source.label if source else "<dict>"
    message = "The Lookup Configuration at '{}' has wrong type, {}".format(
        label, "\n".join(details)
    )
    raise ConfigError(message, path=source.path if source else None)


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
                    "{}".format(b, _ruby_type_name(datadir_value)),
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
