# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Hiera configuration: loading base config, building hierarchies and levels.

Ports Puppet's ``pops/lookup/hiera_config.rb``.
"""

import copy
import logging
import os
import re
import typing as _ty

import yaml
from pathlib_next import Path

from ..backends import Backend, YAMLBackend, has_hocon
from ..exceptions import BackendError, ConfigError
from .location_resolver import resolve_locations
from .._scope.scope import Scope
from ..backends._yaml_loader import RubySymbol, symkeys_to_string
from .._enums import _StrEnum, _plain

_LOGGER = logging.getLogger(__name__)

#: Puppet's built-in default configuration, used when hiera.yaml does not
#: exist (``hiera_config.rb:728-740``, ``HieraConfigV5::DEFAULT_CONFIG_HASH``).
#: Every use deep-copies this -- never mutate it in place.
DEFAULT_CONFIG_HASH = {
    "version": 5,
    "defaults": {"datadir": "data", "data_hash": "yaml_data"},
    "hierarchy": [{"name": "Common", "path": "common.yaml"}],
}

#: Puppet's Hiera 3 default configuration (``hiera_config.rb:433-437``,
#: ``HieraConfigV3::DEFAULT_CONFIG_HASH``): used both as the ``||=`` fill for
#: missing/``false`` top-level v3 keys (:func:`_fill_v3_defaults`) and, via
#: :func:`_read_base_config`, when a hiera.yaml exists but does not parse to
#: a YAML hash at all (Puppet falls back to this, then reads it as v3, at
#: every layer -- ``hiera_config.rb:139-144``). Every use deep-copies this.
V3_DEFAULT_CONFIG_HASH = {
    "backends": ["yaml"],
    "hierarchy": ["nodes/%{::trusted.certname}", "common"],
    "merge_behavior": "native",
}


class _ConfigSource(_ty.NamedTuple):
    """Where a base config came from: for error messages and, later, lines.

    ``label`` is what a Puppet-style message names as "The Lookup
    Configuration at '<label>'": an absolute path for a path-configured
    hiera.yaml, a stream's ``.name`` (else ``"<stream>"``), ``"<dict>"`` for
    a dict config, or ``"<default>"`` for ``Hiera(None, ...)``.
    """

    label: str
    #: The file path for a ``ConfigError.path``, else ``None`` (a dict or a
    #: stream with no real file to point at).
    path: "_ty.Optional[str]"
    #: The raw YAML source, so a later error can locate a line. ``None`` for
    #: a dict or default config, which have no text to walk.
    text: "_ty.Optional[str]"
    #: The absolute root relative paths/datadirs resolve against.
    root: Path


def _config_error(source: "_ConfigSource", message: str, line=None) -> ConfigError:
    """Puppet's ``LookupError`` file/line suffix (``issues.rb:841-871``),
    appended to ``message``. Returned, not raised -- callers ``raise`` it."""
    path = source.path if source else None
    if path and line:
        message = "{} (file: {}, line: {})".format(message, path, line)
    elif path:
        message = "{} (file: {})".format(message, path)
    elif line:
        message = "{} (line: {})".format(message, line)
    return ConfigError(message, path=path, line=line)


def _type_error(source: "_ConfigSource", detail: str, line=None) -> ConfigError:
    """Puppet's ``The Lookup Configuration at '<label>' has wrong type, ...``
    (``hiera_config.rb``'s ``Types::TypeMismatchDescriber`` on ``CONFIG_TYPE``)."""
    label = source.label if source else "<dict>"
    message = "The Lookup Configuration at '{}' has wrong type, {}".format(
        label, detail
    )
    if line:
        message = "{} (line: {})".format(message, line)
    path = source.path if source else None
    return ConfigError(message, path=path, line=line)


def _ruby_type_name(value) -> str:
    """The Puppet type name a value of this Python type reports as."""
    if isinstance(value, bool):
        return "Boolean"
    if value is None:
        return "Undef"
    if isinstance(value, RubySymbol):
        # A Ruby Symbol value survives `symkeys_to_string` (only dict *keys*
        # are normalized) and has no Puppet type of its own -- Puppet's own
        # TypeCalculator reports it as a bare `Runtime` (probe
        # `v3symmb`: `:merge_behavior: :deeper`).
        return "Runtime"
    if isinstance(value, str):
        return "String"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, list):
        return "Tuple"
    if isinstance(value, dict):
        if value and all(isinstance(k, str) for k in value):
            return "Struct"
        return "Hash"
    return type(value).__name__


_VERSION_LEADING_INT = re.compile(r"\s*[+-]?\d+")


def _config_version(data: dict, source: "_ConfigSource") -> int:
    """Puppet's version dispatch (``hiera_config.rb:153-167``), returning the
    resolved version: ``3``, ``4`` or ``5`` -- the same for every layer.
    A missing ``version`` is Hiera 3's own signal and resolves to ``3``
    (``hiera_config.rb:130``: ``version = data['version'] || 3``). Any
    other version (``1``, ``2``, ``6``, ...) raises.

    This function only identifies *which* reader applies
    (:func:`_read_v3`/:func:`_read_v4`/the v5 path); it does not decide
    whether that version is allowed in the layer being read. Puppet
    validates a version 3 or 4 config's own schema before ever checking
    that (``HieraConfigV3``/``V4#initialize`` build the provider list as
    part of construction; ``assert_config_version`` runs strictly after,
    per-layer, at each use for 3, and once at global-layer load for 4 --
    probed: a schema-invalid ``version: 4`` file at the global layer
    raises its schema error, never "cannot be used in the global layer").
    The layer rule itself lives in the caller: :meth:`~hyera.core.
    Hiera._load_config` raises the version 4 in the global layer error
    right after :func:`_read_v4` succeeds; :meth:`~hyera.core.Hiera._usable`
    applies the version 3 outside the global layer rule at each use.
    """
    v = data.get("version")
    if v is None:
        return 3
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise _type_error(
            source,
            "entry 'version' expects an Integer value, got {}".format(
                _ruby_type_name(v)
            ),
        )
    if isinstance(v, str):
        m = _VERSION_LEADING_INT.match(v)
        n = int(m.group()) if m else 0
    elif isinstance(v, float):
        n = int(v)
    else:
        n = v
    if n == 5:
        if not isinstance(v, int):
            raise _type_error(
                source,
                "entry 'version' expects an Integer value, got {}".format(
                    _ruby_type_name(v)
                ),
            )
        return 5
    if n == 3:
        return 3
    if n == 4:
        return 4
    raise _config_error(
        source, "This runtime does not support hiera.yaml version {}".format(n)
    )


def _fill_v5_defaults(data: dict) -> None:
    """Puppet's ``defaults ||=``/``hierarchy ||=`` fill
    (``validate_config``, ``hiera_config.rb:742-745``), run after
    :func:`_config_version`. ``is``, not ``==``: ``0 == False`` in Python,
    and Puppet's ``||=`` triggers on Ruby ``nil``/``false`` alike."""
    if data.get("defaults") is None or data.get("defaults") is False:
        data["defaults"] = copy.deepcopy(DEFAULT_CONFIG_HASH["defaults"])
    if data.get("hierarchy") is None or data.get("hierarchy") is False:
        data["hierarchy"] = copy.deepcopy(DEFAULT_CONFIG_HASH["hierarchy"])


def _warn_deprecated(source: "_ConfigSource", version: int, scope) -> None:
    """Puppet's per-version deprecation warning (``hiera_config.rb:85``,
    ``:440``, ``:554``): logged once per read, silenced only when
    ``scope.strict == "off"`` (locations stay lenient regardless)."""
    if scope is not None and getattr(scope, "strict", None) == "off":
        return
    _LOGGER.warning(
        "%s: Use of 'hiera.yaml' version %d is deprecated. It should be "
        "converted to version 5",
        source.label,
        version,
    )


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


#: ``HieraConfigV3``'s own struct keys (``hiera_config.rb:355-370``), in
#: schema-declaration order -- the order :func:`_validate_v3` reports
#: mismatches in. A backend's own config key (``:yaml:``, ``:eyaml:``, ...)
#: is a *dynamic* addition to this set, one per distinct name in
#: ``backends`` (``:397``), so it is not listed here.
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


def _v3_backend_class(name: str, source: "_ConfigSource", line=None):
    """Resolve a Hiera 3 backend name (a v3 ``backends:`` entry, or a v5
    ``hiera3_backend:``) against the registry's ``v3`` namespace: empty for
    every built-in -- ``yaml``/``json``/``hocon``/``eyaml`` map to the v5
    ``*_data``/``eyaml_lookup_key`` functions inside :func:`_v3_level_specs`
    itself and never reach here. Only a third-party backend registered
    under this name resolves; anything else raises, since a Ruby Hiera 3
    backend cannot run here (recorded as the ``v3-ruby-backend-unavailable``
    deviation in the conformance suite).
    """
    cls = Backend.find(name, kind="v3")
    if cls is None:
        raise _config_error(
            source,
            "Hiera 3 backend '{}' is not available: Ruby Hiera 3 backends "
            "cannot run here, and no Backend is registered under the v3 "
            "name '{}'".format(name, name),
            line=line,
        )
    if not cls.implements("data_hash"):
        raise _config_error(
            source,
            "Hiera 3 backend '{}' ({}) must implement data_hash".format(
                name, cls.__name__
            ),
            line=line,
        )
    return cls


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
        if "datadir" in conf:
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
        if "extension" in conf:
            extension = "." + conf["extension"]
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
        # v4's datadir is joined onto the config root literally, never
        # interpolated (`hiera_config.rb:525`, unlike v5's `:664-665`) --
        # HieraLevel.datadir_literal tells the location resolver to skip
        # interpolating it at all, so a literal '%' survives unchanged
        # (interpolating it with `allow_methods=False`, as every other
        # level's datadir is, rules out escaping it with
        # `%{literal('%')}`: that is itself a disallowed method call).
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
    # _step's own two return shapes are (None, a real sequence-item node)
    # for an int step, or (a real key node, a real value node) for a
    # found mapping entry -- never a tuple whose second element is None,
    # and the first element is only None in the int-step case, exactly
    # when `key and k_node is not None` would be false anyway. So `target`
    # is never None here.
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
    # Every key reaching this loop is already a member of _ENTRY_KEYS (the
    # "unrecognized key" check above already rejected anything else), and
    # the branches below cover _ENTRY_KEYS exactly: "name"/"datadir"/
    # "options" (3), "path"/"glob"/"uri"/"paths"/"globs"/"uris"/
    # "mapped_paths" (all 7 of _LOCATION_KEYS), and _ALL_FUNCTION_KEYS as a
    # whole. So this never falls through without matching one of them.
    for key, value in entry.items():
        if key == "name":
            _check_string(value, where + ("name",), source, nonempty=True)
        elif key == "datadir":
            _check_string(value, where + ("datadir",), source)
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
    # Every key reaching this loop is already a member of _DEFAULTS_KEYS
    # (the "unrecognized key" check above already rejected anything else):
    # "datadir"/"options" are handled by name, and the three remaining
    # members (data_hash/lookup_key/data_dig) are exactly the _FUNCTION_KEYS
    # entries _DEFAULTS_KEYS actually allows (hiera3_backend never reaches
    # here at all -- see _function_of). So this never falls through either.
    for key, v in value.items():
        if key == "datadir":
            _check_string(v, where + ("datadir",), source)
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
    # Every key reaching this loop is already a member of _TOP_KEYS (the
    # "unrecognized key" check above already rejected anything else):
    # "version" is skipped by name, and the other four members
    # (defaults/hierarchy/plan_hierarchy/default_hierarchy) are exactly
    # what the two branches below name. So this never falls through
    # without matching one of them either.
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
    ``defaults`` struct type excludes it the same way (confirmed against
    ``hiera_config.rb``'s ``@@CONFIG_TYPE`` and a real Puppet 8.10 run,
    see ``config-defaults-hiera3-backend-key``) -- kept as written, rather
    than narrowed, to stay a literal port.
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


class FunctionKind(_StrEnum):
    """A hierarchy entry's resolved function kind: :attr:`HieraLevel.kind`,
    and the ``kind=`` argument of :meth:`HieraLevel.new`. Which Puppet
    Hiera 5 provider hook a level's backend implements."""

    DATA_HASH = "data_hash"
    """Reads a whole data source at once (``YAMLBackend``/``JSONBackend``/
    ``HOCONBackend``/...); the found values are merged across locations and
    levels by hyera itself, never by the backend."""

    LOOKUP_KEY = "lookup_key"
    """Resolves one root key itself, given a
    :class:`~hyera.LookupContext`."""

    DATA_DIG = "data_dig"
    """Resolves one full (possibly dotted) key itself, given a
    :class:`~hyera.LookupContext`."""


class HieraLevel(_ty.NamedTuple):
    """One hierarchy entry, stored exactly as written in hiera.yaml --
    ``locations`` are never interpolated or normalized here; that happens
    per lookup, against a bound :class:`~hyera.Scope`
    (:func:`~hyera._config.location_resolver.resolve_locations`)."""

    name: str
    backend: Backend
    datadir: str
    #: The one location key this entry declared (``"path"``, ``"paths"``,
    #: ``"glob"``, ``"globs"``, ``"uri"``, ``"uris"`` or ``"mapped_paths"``),
    #: or ``None`` for a location-less entry.
    location_key: "_ty.Optional[str]"
    #: The raw declared value(s): one string for a singular key, the tuple
    #: as written for a plural one, and ``(collection_var, item_var,
    #: template)`` for ``mapped_paths``.
    locations: "_ty.Tuple[str, ...]"
    #: This entry's resolved function kind: ``"data_hash"``, ``"lookup_key"``
    #: or ``"data_dig"``.
    kind: str = "data_hash"
    #: The entry's own ``options``, else ``defaults``'s (never merged),
    #: exactly as declared -- interpolated per lookup, per scope, not here.
    options: "_ty.Optional[_ty.Dict[str, _ty.Any]]" = None
    #: A version 3/``hiera3_backend`` extension, appended to each declared
    #: ``path``/``paths`` location (after interpolation) unless it already
    #: ends with it (``location_resolver.rb:59-61``). ``None`` for a v4/v5
    #: level (Puppet appends the extension for those during config reading,
    #: not at lookup time -- see :func:`_v4_levels`/:func:`_build_levels`).
    extension: "_ty.Optional[str]" = None
    #: The root a version 3 level's ``datadir`` resolves against -- the
    #: process cwd *at construction*, never the hiera.yaml
    #: directory. ``None`` for a v4/v5 level, which uses the caller's own
    #: ``base_path``.
    datadir_base: "_ty.Optional[Path]" = None
    #: ``True`` for a version 4 level only: ``datadir`` is joined onto the
    #: config root literally, with no interpolation at all
    #: (``hiera_config.rb:525``, unlike v5's ``:664-665``) -- not even the
    #: strict, method-free substitution every other level's ``datadir``
    #: gets, since that would still trip over a literal ``%`` the way a
    #: plain string substitution attempt (``allow_methods=False`` rules out
    #: escaping it with ``%{literal('%')}``) cannot avoid.
    datadir_literal: bool = False

    @classmethod
    def new(
        cls,
        conf: _ty.Dict[str, _ty.Any],
        backend: Backend,
        kind: _ty.Union[FunctionKind, str] = "data_hash",
        *,
        extension: _ty.Optional[str] = None,
        datadir_base: "_ty.Optional[Path]" = None,
        datadir_literal: bool = False,
    ) -> "HieraLevel":
        """Build a level from one already-resolved hierarchy entry
        ``conf`` (a hiera.yaml entry, or ``defaults`` filled in): reads
        ``conf["name"]``/``["datadir"]``, whichever location key is
        present, and ``conf.get("options")``, leaving every location
        uninterpolated.

        :param conf: the entry's own mapping, with ``defaults`` already
            merged in by the caller where the entry names nothing.
        :param backend: the resolved backend for this level's function.
        :param kind: this level's function kind (``"data_hash"``,
            ``"lookup_key"`` or ``"data_dig"``).
        :param extension: appended to a v3-derived location (``None`` for
            v4/v5).
        :param datadir_base: the root a v3 level's ``datadir`` resolves
            against (``None`` for v4/v5).
        :param datadir_literal: whether ``datadir`` is joined onto the
            config root literally, with no interpolation (v4 only).
        :returns: the built level.
        """
        location_key = next((k for k in _LOCATION_KEYS if k in conf), None)
        if location_key is None:
            locations: "_ty.Tuple[str, ...]" = ()
        elif location_key in ("paths", "globs", "uris", "mapped_paths"):
            locations = tuple(conf[location_key])
        else:
            locations = (conf[location_key],)
        kind = _plain(kind)
        return cls(
            name=conf["name"],
            backend=backend,
            datadir=conf["datadir"],
            location_key=location_key,
            locations=locations,
            kind=kind,
            options=conf.get("options"),
            extension=extension,
            datadir_base=datadir_base,
            datadir_literal=datadir_literal,
        )

    def paths(self, base_path: Path, scope: Scope) -> "_ty.List[str]":
        """The candidate source (file) paths for this level in a bound
        :class:`~hyera.Scope`. A location-less entry, or one using ``uri``/
        ``uris`` (which never resolve to a filesystem path), yields ``[]``.

        :param base_path: the root relative locations resolve against.
        :param scope: the scope location templates interpolate against.
        :returns: the candidate paths, interpolated but not filtered by
            existence.
        """
        resolved = resolve_locations(self, base_path, scope)
        if resolved is None:
            return []
        return [loc.location for loc in resolved if not loc.is_uri]


def _read_base_config(base_config, base_path) -> "_ty.Tuple[_ConfigSource, dict]":
    """Load the base configuration (``HieraConfig.create``, ``hiera_config.rb:127-168``).

    Returns ``(source, base)``: ``base`` is a dict this call owns outright
    (a deep copy of a dict/default config, or a freshly parsed file) --
    ``self.base_config`` keeps the caller's own argument untouched, and a
    dict passed in is never mutated. Raises :class:`ConfigError`
    (``.path`` set for a path- or stream-configured hiera.yaml) on any read,
    parse, or top-level-shape problem.

    A file that parses but is not a YAML hash never raises here, at any
    layer: it logs Puppet's own warning and falls back to
    :data:`V3_DEFAULT_CONFIG_HASH` (``hiera_config.rb:139-144``), which
    :func:`_config_version` then reads as version 3 -- read in full by
    :func:`_read_v3` at the global layer, or left, like any other version-3
    layer config, for :meth:`~hyera.core.Hiera._usable` to ignore or raise
    about outside it.
    """
    if base_config is None:
        # Puppet's missing-file default (`hiera_config.rb:147-149`): no file
        # to point at, so `.path` stays None.
        root = Path(os.getcwd() if base_path is None else base_path).absolute()
        return _ConfigSource("<default>", None, None, root), copy.deepcopy(
            DEFAULT_CONFIG_HASH
        )

    if isinstance(base_config, dict):
        root = Path(os.getcwd() if base_path is None else base_path).absolute()
        return _ConfigSource("<dict>", None, None, root), copy.deepcopy(base_config)

    # Read once, hold no open handle: keeps the caller's path/stream free to
    # be replaced or pickled across, and leaves a stream at the caller's
    # mercy. Decoded as strict UTF-8 -- Puppet reads every data file this
    # way (``context.rb:53``) and ``hiera_config.rb`` parses ``hiera.yaml``
    # with the same ``safe_load`` data files use.
    if hasattr(base_config, "read"):
        name = getattr(base_config, "name", None)
        label = str(name) if name else "<stream>"
        path = str(name) if name else None
        root = Path(os.getcwd() if base_path is None else base_path).absolute()
        content = base_config.read()
        raw = content if isinstance(content, bytes) else content.encode("utf-8")
    else:
        configpath = Path(base_config).absolute()
        root = (
            Path(base_path).absolute() if base_path is not None else configpath.parent
        )
        label = str(configpath)
        path = label
        if configpath.is_dir():
            raise ConfigError(
                "Unable to read the Lookup Configuration at '{}': Is a "
                "directory".format(label),
                path=path,
            )
        try:
            raw = configpath.read_bytes()
        except OSError as e:
            raise ConfigError(
                "Unable to read the Lookup Configuration at '{}': {}".format(
                    label, e.strerror or e
                ),
                path=path,
            ) from e

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConfigError("({}): {}".format(label, e), path=path) from e
    source = _ConfigSource(label, path, text, root)
    # `puppet lookup` reads hiera.yaml via `HieraConfig.create` ->
    # `cached_file_data` -> `Puppet::Util::Yaml.safe_load(content, ...)`
    # directly on the file's content -- *not* through
    # `Puppet::Util::Yaml.safe_load_file`'s BOM-stripping
    # `Puppet::FileSystem.read(path, encoding: "bom|utf-8")`. A leading
    # BOM therefore reaches `YAML.safe_load` exactly as it does for a
    # data file (`context.rb:53`), keeping a literal U+FEFF character;
    # measured 2026-09-29 against real Puppet 8.10.0 on a
    # `<BOM>---\nversion: 5\n...` config (`config-hiera-yaml-bom`):
    # Puppet errors identically to a data file with the same content, so
    # this is *not* stripped here -- `YAMLBackend.loads` (via
    # `_yaml_loader.safe_load`'s BOM-swap) handles it the same way.
    try:
        base = YAMLBackend().loads(text)
    except BackendError as e:
        raise ConfigError("({}): {}".format(label, e), path=path) from e
    if isinstance(base, dict):
        # hiera_config.rb:181 -- symbol keys (however written) become
        # plain strings for every config version, not just data files.
        base = symkeys_to_string(base)

    if not isinstance(base, dict):
        _LOGGER.warning(
            "%s: File exists but does not contain a valid YAML hash. "
            "Falling back to Hiera version 3 default config",
            source.label,
        )
        return source, copy.deepcopy(V3_DEFAULT_CONFIG_HASH)

    return source, base


def _function_of(entry: dict, defaults: dict):
    """Puppet's function-kind resolution (``hiera_config.rb:656-662``):
    returns ``(kind, name)``. The entry's own function key wins; ``defaults``
    is consulted only when the entry has none, and then only for
    ``_FUNCTION_KEYS`` (``defaults`` never carries ``v4_data_hash``).
    Unreachable with a ``None`` result once :func:`_validate_v5` has run
    (it guarantees exactly one function key, on the entry or in
    ``defaults``); kept total rather than assuming that here too.

    The ``defaults`` loop's own ``hiera3_backend`` case can never match:
    ``defaults`` only ever reaches here after ``_check_defaults_type``
    rejected any key outside ``_DEFAULTS_KEYS``, which excludes
    ``hiera3_backend`` -- the same restriction Puppet's own ``defaults``
    struct type places on it (``hiera_config.rb``'s ``@@CONFIG_TYPE``,
    confirmed against a real Puppet 8.10 run in
    ``config-defaults-hiera3-backend-key``). Puppet's own
    ``function_kind = FUNCTION_KEYS.find { |key| defaults.include?(key) }``
    has the identical shape, so this stays a literal port rather than a
    narrower, hand-trimmed key list.
    """
    for key in _ALL_FUNCTION_KEYS:
        if key in entry:
            return key, entry[key]
    for key in _FUNCTION_KEYS:
        if key in defaults:
            return key, defaults[key]
    return None, None


def _build_hierarchies(base, backends, source: "_ConfigSource", *, scope=None):
    """Build ``hierarchy`` and ``default_hierarchy`` from base config.

    Returns ``(hierarchy_levels, default_hierarchy_levels)``. Assumes
    :func:`_config_version`, :func:`_fill_v5_defaults` and
    :func:`_validate_v5` already ran, so ``defaults``/``hierarchy`` are
    present and every entry has exactly one function key (its own, or one
    from ``defaults``), at most one location key, and well-typed values.
    ``scope`` is only consulted for a ``hiera3_backend`` entry's own
    backend construction (its ``strict``); a caller that can never reach
    one (a version-3/missing-version layer, already rejected by
    :func:`_validate_v5`'s global-only rule before this runs) may omit it.
    """
    hierarchy = base.get("hierarchy")
    defaults = base.get("defaults") or {}

    backend_levels = _build_levels(
        hierarchy, defaults, backends, source, scope=scope, area="hierarchy"
    )
    default_levels = _build_levels(
        base.get("default_hierarchy") or [],
        defaults,
        backends,
        source,
        scope=scope,
        area="default_hierarchy",
    )

    return backend_levels, default_levels


def _kind_mismatch_text(backend_cls, func_name: str, kind: str) -> str:
    """Puppet's function-arity/parameter-type text when a hierarchy level
    names a function that does not implement the kind it is used as
    (``lookup_key_function_provider.rb``/``data_dig_function_provider.rb``/
    ``data_hash_function_provider.rb``'s own dispatch by arity): the same
    text Puppet raises on every lookup, reported here once at level build.
    Only called when ``not backend_cls.implements(kind)``.
    """
    has_dh = backend_cls.implements("data_hash")
    has_lk = backend_cls.implements("lookup_key")
    has_dd = backend_cls.implements("data_dig")
    if not (has_dh or has_lk or has_dd):
        return "'{}' implements none of data_hash, lookup_key or data_dig".format(
            func_name
        )
    if kind == "data_hash":
        return "'{}' expects 3 arguments, got 2".format(func_name)
    if has_dh:
        return "'{}' expects 2 arguments, got 3".format(func_name)
    # Only "data_dig" and "lookup_key" are left for `kind` (Puppet has no
    # fourth function kind), has_dh is now known false, and at least one
    # of has_lk/has_dh/has_dd was true (the first check above already
    # returned otherwise) -- so exactly one of has_lk/has_dd is true,
    # matching whichever of the two kind values `kind` is not (the
    # precondition above rules out backend_cls implementing `kind`
    # itself). Neither branch's own "and has_lk"/"and has_dd" condition
    # can ever be false when reached, so there is no remaining case for a
    # trailing fallback to catch.
    if kind == "data_dig":
        return "'{}' parameter 'key' expects a String value, got Tuple".format(
            func_name
        )
    return "'{}' parameter 'key_segments' expects an Array value, got String".format(
        func_name
    )


def _build_level(
    conf: dict,
    kind: str,
    function: str,
    backends,
    source: "_ConfigSource",
    scope,
    *,
    area: str = "hierarchy",
    index: int = 0,
    extension=None,
    datadir_base=None,
    datadir_literal=False,
    backend_cls=None,
) -> HieraLevel:
    """Resolve one hierarchy entry's backend and build its
    :class:`HieraLevel`.

    With ``backend_cls`` given (a v3 backend, resolved by the caller
    against the ``v3`` registry namespace -- :func:`_v3_backend_class`),
    ``function`` is that v3 name and the function-namespace lookup below is
    skipped entirely. Otherwise this is the v5 ``data_hash``/``lookup_key``/
    ``data_dig`` path: ``backends`` is an allow-list of
    :class:`~hyera.backends.Backend` subclasses -- ``function`` is resolved
    against the process-global registry (:meth:`Backend.find`, the one
    namespace all three share), then checked against this allow-list, so a
    name registered by a third party but not passed to
    ``Hiera(backends=...)`` is refused exactly like an unknown one.
    """
    name = conf.get("name")
    if backend_cls is not None:
        backend = Backend.new(
            function, conf, kind="v3", strict=getattr(scope, "strict", None)
        )
    elif kind in ("data_hash", "lookup_key", "data_dig"):
        resolved_cls = Backend.find(function, kind="function")
        if resolved_cls is None or resolved_cls not in backends:
            if kind == "data_hash":
                allowed_names = [
                    n
                    for n in Backend.names("function")
                    if Backend.find(n, "function") in backends
                ]
                raise _config_error(
                    source,
                    "Unable to find 'data_hash' function named '{}'; "
                    "known: {}".format(function, ", ".join(allowed_names)),
                ) from None
            raise _config_error(
                source,
                "Unable to find '{}' function named '{}'".format(kind, function),
            ) from None
        if not resolved_cls.implements(kind):
            raise _config_error(
                source,
                _kind_mismatch_text(resolved_cls, function, kind),
                line=_config_line(source.text, (area, index, kind), key=True),
            )
        conf = dict(conf)
        conf[kind] = function
        backend = Backend.new(function, conf, kind="function")
    else:
        # Unreachable via the v5 path: _validate_v5 guarantees a function
        # key exists. _build_levels handles hiera3_backend/v4_data_hash
        # itself before ever calling this.
        raise _config_error(
            source, "Hierarchy level {!r} is missing a function key".format(name)
        )
    return HieraLevel.new(
        conf,
        backend,
        kind,
        extension=extension,
        datadir_base=datadir_base,
        datadir_literal=datadir_literal,
    )


def _build_levels(
    hierarchy,
    defaults,
    backends,
    source: "_ConfigSource",
    *,
    scope=None,
    area: str = "hierarchy",
):
    """Build HieraLevel instances from hierarchy configuration.

    Each entry's conf is built explicitly -- ``name``, its one location
    key (if any), ``datadir``, ``options`` (the entry's own, else
    ``defaults``'s, never merged -- ``hiera_config.rb:690``)
    and, for a ``data_hash`` level, ``data_hash: <name>`` -- rather than
    merging every ``defaults`` key in wholesale.
    """
    levels: "list[HieraLevel]" = []
    for i, level in enumerate(hierarchy):
        name = level.get("name")
        kind, func_name = _function_of(level, defaults)
        datadir = level.get("datadir") or defaults.get("datadir") or "data"
        options = level.get("options", defaults.get("options"))

        conf = {"name": name, "datadir": datadir}
        for loc_key in _LOCATION_KEYS:
            if loc_key in level:
                conf[loc_key] = level[loc_key]
        if options is not None:
            conf["options"] = options

        if kind == "hiera3_backend":
            # Global-only (_validate_v5 already rejected this entry
            # everywhere else); replaces a v5 data_hash function with a
            # registered v3-namespace backend, matching Puppet's own
            # strip-then-Hiera-3-appends extension rule
            # (hiera_config.rb:692-714, hiera/backend.rb:57-58) by giving
            # it the same "append unless already present" extension a v3
            # `backends:` entry gets.
            line = _config_line(source.text, (area, i, "hiera3_backend"), key=True)
            backend_cls = _v3_backend_class(func_name, source, line)
            levels.append(
                _build_level(
                    conf,
                    "data_hash",
                    func_name,
                    backends,
                    source,
                    scope,
                    area=area,
                    index=i,
                    extension="." + func_name,
                    backend_cls=backend_cls,
                )
            )
            continue
        if kind == "v4_data_hash":
            raise _config_error(
                source,
                "Unable to find 'v4_data_hash' function named '{}'".format(func_name),
            ) from None

        levels.append(
            _build_level(
                conf, kind, func_name, backends, source, scope, area=area, index=i
            )
        )
    return levels
