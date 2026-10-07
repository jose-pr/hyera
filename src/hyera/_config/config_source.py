# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb,
# lib/puppet/pops/issues.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Where a hiera.yaml came from, its error messages and its version number.

Ports the version dispatch of Puppet's ``pops/lookup/hiera_config.rb`` and the
file/line suffix of ``issues.rb``.
"""

from __future__ import annotations

import functools
import re
import typing as _ty

import yaml
from pathlib_next import Path

from .._types.mismatch import describe_instance_of, format_mismatches
from .._types.parser import parse_type
from ..exceptions import ConfigError


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


#: ``hiera_config.rb:574``, the ``nes_t`` of every string the schemas name.
_NES = "String[1]"
#: The ``version`` entry of every schema, without its range (``hiera_config.rb:363``,
#: ``:497``, ``:605``): what a ``version`` that is no Integer is checked against.
_VERSION_TYPE = parse_type("Struct[{version => Integer}]")


def _struct(*members: str) -> str:
    """The type expression of a ``Struct`` with these ``key => type`` members."""
    return "Struct[{" + ", ".join(members) + "}]"


def _optional(key: str, type_: str) -> str:
    """The ``Struct`` member ``Optional[key] => type_``."""
    return "Optional[{}] => {}".format(key, type_)


@functools.lru_cache(maxsize=8)
def _compose(text: str):
    try:
        return yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None


def _config_line(text, where: "_ty.Tuple", *, key: bool = False):
    """The 1-based line of the node at ``where`` in ``text`` (a hiera.yaml's
    raw source): a mapping key when ``key=True``, else its value (or a
    sequence item for an ``int`` step). ``None`` when there is no text, on a
    YAML error, or when ``where`` does not resolve (compose only runs when
    actually needed -- the happy path never pays for this).
    """
    if not text or not where:
        return None
    node = _compose(text)
    if node is None:
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


class _NotYaml:
    """Stands in for a tuple: a value of no Puppet type."""


def _hide_tuples(value):
    """``value`` with each tuple replaced by a :class:`_NotYaml`: a tuple is Python's,
    never YAML's, and a hiera.yaml's arrays are lists."""
    if isinstance(value, tuple):
        return _NotYaml()
    if isinstance(value, list):
        items = [_hide_tuples(v) for v in value]
        return items if any(a is not b for a, b in zip(items, value)) else value
    if isinstance(value, dict):
        entries = {k: _hide_tuples(v) for k, v in value.items()}
        changed = any(entries[k] is not v for k, v in value.items())
        return entries if changed else value
    return value


def _check_config_type(
    source: "_ConfigSource", config_type, data, *, lines: bool = True
) -> None:
    """Puppet's ``TypeAsserter.assert_instance_of`` on a hiera.yaml
    (``hiera_config.rb``'s ``validate_config``): raise a :class:`ConfigError` listing
    every mismatch of ``data`` with ``config_type``, in Puppet's text.

    With ``lines``, each mismatch ends with the ``(line: N)`` of the YAML node it
    points at and the error's ``line`` is the first mismatch's.
    """
    mismatches = describe_instance_of(config_type, _hide_tuples(data))
    if not mismatches:
        return
    label = source.label if source else "<dict>"
    text = source.text if source and lines else None
    found: "_ty.List[_ty.Optional[int]]" = []

    def annotate(mismatch) -> str:
        steps, of_key = mismatch.location()
        line = _config_line(text, steps, key=of_key)
        found.append(line)
        return " (line: {})".format(line) if line else ""

    message = format_mismatches(
        "The Lookup Configuration at '{}'".format(label), mismatches, annotate
    )
    raise ConfigError(
        message,
        path=source.path if source else None,
        line=found[0] if found else None,
    )


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
    The layer rule itself lives in the caller:
    :func:`~hyera._config.data_provider.load_global_layer` raises the version 4
    in the global layer error right after :func:`_read_v4` succeeds;
    :func:`~hyera._config.data_provider.usable_provider` applies the version 3
    outside the global layer rule at each use.
    """
    v = data.get("version")
    if v is None:
        return 3
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        _check_config_type(source, _VERSION_TYPE, {"version": v}, lines=False)
    if isinstance(v, str):
        m = _VERSION_LEADING_INT.match(v)
        n = int(m.group()) if m else 0
    elif isinstance(v, float):
        n = int(v)
    else:
        n = v
    if n == 5:
        if not isinstance(v, int):
            _check_config_type(source, _VERSION_TYPE, {"version": v}, lines=False)
        return 5
    if n == 3:
        return 3
    if n == 4:
        return 4
    raise _config_error(
        source, "This runtime does not support hiera.yaml version {}".format(n)
    )
