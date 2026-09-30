# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
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

from .backends import Backend, YAMLBackend, has_hocon
from .exceptions import BackendError, ConfigError
from ._location_resolver import resolve_locations
from ._yaml_loader import symkeys_to_string

_LOGGER = logging.getLogger(__name__)

#: Puppet's built-in default configuration, used when hiera.yaml does not
#: exist (``hiera_config.rb:728-740``, ``HieraConfigV5::DEFAULT_CONFIG_HASH``).
#: Every use deep-copies this -- never mutate it in place.
DEFAULT_CONFIG_HASH = {
    "version": 5,
    "defaults": {"datadir": "data", "data_hash": "yaml_data"},
    "hierarchy": [{"name": "Common", "path": "common.yaml"}],
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


def _select_version(
    data: dict, source: "_ConfigSource", *, layer: str = "global"
) -> int:
    """Puppet's version dispatch (``hiera_config.rb:153-167``), returning the
    resolved version.

    At the global layer (``layer="global"``), anything but a literal Integer
    ``5`` raises :class:`ConfigError` -- version 3/4 parsing at the global
    layer is not implemented yet, so a missing ``version`` (Hiera 3's own
    signal) and an explicit ``3``/``4`` just name what they are, unsupported
    for now. Outside the global layer (``"environment"``/``"module"``), a
    missing ``version`` or an explicit ``3`` is not an error here: it
    returns ``3`` and leaves the ignore-or-raise decision to
    :meth:`~hyera.core.Hiera._usable`, applied with the invocation's own
    ``strict`` at the point the layer is actually consulted
    (``environment_data_provider.rb``/``module_data_provider.rb``). A
    version 4 there raises "not supported yet" (``hiera_v3_v4_configs``
    replaces this). Any other version raises the same way at every layer.
    """
    v = data.get("version")
    _v3_text = (
        "hiera.yaml version 3 is not supported yet (a hiera.yaml without "
        "'version' is version 3)"
    )
    if v is None:
        if layer == "global":
            raise _config_error(source, _v3_text)
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
        if layer == "global":
            raise _config_error(source, _v3_text)
        return 3
    if n == 4:
        if layer == "global":
            raise _config_error(
                source, "hiera.yaml version 4 cannot be used in the global layer"
            )
        raise _config_error(source, "hiera.yaml version 4 is not supported yet")
    raise _config_error(
        source, "This runtime does not support hiera.yaml version {}".format(n)
    )


def _fill_v5_defaults(data: dict) -> None:
    """Puppet's ``defaults ||=``/``hierarchy ||=`` fill
    (``validate_config``, ``hiera_config.rb:742-745``), run after
    :func:`_select_version`. ``is``, not ``==``: ``0 == False`` in Python,
    and Puppet's ``||=`` triggers on Ruby ``nil``/``false`` alike."""
    if data.get("defaults") is None or data.get("defaults") is False:
        data["defaults"] = copy.deepcopy(DEFAULT_CONFIG_HASH["defaults"])
    if data.get("hierarchy") is None or data.get("hierarchy") is False:
        data["hierarchy"] = copy.deepcopy(DEFAULT_CONFIG_HASH["hierarchy"])


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
    k_node, v_node = found
    target = k_node if key and k_node is not None else v_node
    if target is None:
        return None
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
    for key, value in data.items():
        if key == "version":
            continue  # already validated by _select_version
        if key == "defaults":
            _check_defaults_type(value, ("defaults",), source)
        elif key in ("hierarchy", "plan_hierarchy", "default_hierarchy"):
            _check_hierarchy_type(value, (key,), source)


def _validate_defaults_issues(defaults: dict, source: "_ConfigSource") -> None:
    """``validate_defaults`` (``hiera_config.rb:802-816``)."""
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


class HieraLevel(_ty.NamedTuple):
    """One hierarchy entry, stored exactly as written in hiera.yaml --
    ``locations`` are never interpolated or normalized here; that happens
    per lookup, against a bound :class:`~hyera.Scope`
    (:func:`~hyera._location_resolver.resolve_locations`)."""

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

    @classmethod
    def new(cls, conf: dict, backend: Backend) -> "HieraLevel":
        location_key = next((k for k in _LOCATION_KEYS if k in conf), None)
        if location_key is None:
            locations: "_ty.Tuple[str, ...]" = ()
        elif location_key in ("paths", "globs", "uris", "mapped_paths"):
            locations = tuple(conf[location_key])
        else:
            locations = (conf[location_key],)
        return cls(
            name=conf["name"],
            backend=backend,
            datadir=conf["datadir"],
            location_key=location_key,
            locations=locations,
        )

    def paths(self, base_path: Path, scope) -> "list":
        """The candidate source paths for this level in a bound
        :class:`~hyera.Scope`."""
        return [loc.location for loc in resolve_locations(self, base_path, scope)]


def _read_base_config(
    base_config, base_path, *, layer: str = "global"
) -> "_ty.Tuple[_ConfigSource, dict]":
    """Load the base configuration (``HieraConfig.create``, ``hiera_config.rb:127-168``).

    Returns ``(source, base)``: ``base`` is a dict this call owns outright
    (a deep copy of a dict/default config, or a freshly parsed file) --
    ``self.base_config`` keeps the caller's own argument untouched, and a
    dict passed in is never mutated. Raises :class:`ConfigError`
    (``.path`` set for a path- or stream-configured hiera.yaml) on any read,
    parse, or top-level-shape problem.

    At the global layer, a file that parses but is not a YAML hash raises.
    Outside it (``layer="environment"``/``"module"``), the same case instead
    logs Puppet's own warning and returns an empty ``base`` (``hiera_config.
    rb:143``), which :func:`_select_version` then reads as version 3 --
    left, like any other version-3 layer config, for
    :meth:`~hyera.core.Hiera._usable` to ignore or raise about.
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
        if layer != "global":
            _LOGGER.warning(
                "%s: File exists but does not contain a valid YAML hash. "
                "Falling back to Hiera version 3 default config",
                source.label,
            )
            return source, {}
        raise _config_error(
            source,
            "File exists but does not contain a valid YAML hash; Puppet "
            "falls back to the Hiera version 3 default config, which is "
            "not supported yet",
        )

    return source, base


def _function_of(entry: dict, defaults: dict):
    """Puppet's function-kind resolution (``hiera_config.rb:656-662``):
    returns ``(kind, name)``. The entry's own function key wins; ``defaults``
    is consulted only when the entry has none, and then only for
    ``_FUNCTION_KEYS`` (``defaults`` never carries ``v4_data_hash``).
    Unreachable with a ``None`` result once :func:`_validate_v5` has run
    (it guarantees exactly one function key, on the entry or in
    ``defaults``); kept total rather than assuming that here too.
    """
    for key in _ALL_FUNCTION_KEYS:
        if key in entry:
            return key, entry[key]
    for key in _FUNCTION_KEYS:
        if key in defaults:
            return key, defaults[key]
    return None, None


def _build_hierarchies(base, backends, source: "_ConfigSource"):
    """Build ``hierarchy`` and ``default_hierarchy`` from base config.

    Returns ``(hierarchy_levels, default_hierarchy_levels)``. Assumes
    :func:`_select_version`, :func:`_fill_v5_defaults` and
    :func:`_validate_v5` already ran, so ``defaults``/``hierarchy`` are
    present and every entry has exactly one function key (its own, or one
    from ``defaults``), at most one location key, and well-typed values.
    """
    hierarchy = base.get("hierarchy")
    defaults = base.get("defaults") or {}

    backend_levels = _build_levels(hierarchy, defaults, backends, source)
    default_levels = _build_levels(
        base.get("default_hierarchy") or [], defaults, backends, source
    )

    return backend_levels, default_levels


def _build_levels(hierarchy, defaults, backends, source: "_ConfigSource"):
    """Build HieraLevel instances from hierarchy configuration.

    Each entry's conf is built explicitly -- ``name``, its one location
    key (if any), ``datadir``, ``options`` (the entry's own, else
    ``defaults``'s, never merged -- ``hiera_config.rb:690``)
    and, for a ``data_hash`` level, ``data_hash: <name>`` -- rather than
    merging every ``defaults`` key in wholesale.

    ``backends`` is an allow-list of :class:`~hyera.backends.Backend`
    subclasses: a ``data_hash``/``lookup_key``/``data_dig`` name is
    resolved against the process-global registry (:meth:`Backend.find`,
    the one namespace all three share), then checked against this
    allow-list, so a name registered by a third party but not passed to
    ``Hiera(backends=...)`` is refused exactly like an unknown one.
    """
    levels: "list[HieraLevel]" = []
    for level in hierarchy:
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

        if kind == "data_hash":
            backend_cls = Backend.find(func_name, kind="function")
            if backend_cls is None or backend_cls not in backends:
                allowed_names = [
                    n
                    for n in Backend.names("function")
                    if Backend.find(n, "function") in backends
                ]
                raise _config_error(
                    source,
                    "Unable to find 'data_hash' function named '{}'; known: "
                    "{}".format(func_name, ", ".join(allowed_names)),
                ) from None
            conf["data_hash"] = func_name
            backend = Backend.new(func_name, conf, kind="function")
        elif kind in ("lookup_key", "data_dig"):
            backend_cls = Backend.find(func_name, kind="function")
            if backend_cls is None or backend_cls not in backends:
                raise _config_error(
                    source,
                    "Unable to find '{}' function named '{}'".format(kind, func_name),
                ) from None
            raise _config_error(
                source,
                "'{}' hierarchy entries are not supported yet (hierarchy "
                "'{}', function '{}')".format(kind, name, func_name),
            )
        elif kind == "hiera3_backend":
            raise _config_error(
                source,
                "'hiera3_backend' hierarchy entries are not supported "
                "(hierarchy '{}', backend '{}')".format(name, func_name),
            )
        elif kind == "v4_data_hash":
            raise _config_error(
                source,
                "Unable to find 'v4_data_hash' function named '{}'".format(func_name),
            ) from None
        else:
            # Unreachable: _validate_v5 guarantees a function key exists.
            raise _config_error(
                source, "Hierarchy level {!r} is missing a function key".format(name)
            )

        levels.append(HieraLevel.new(conf, backend))
    return levels
