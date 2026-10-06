# Ported from Puppet 8 lib/puppet/pops/lookup/hiera_config.rb,
# lib/puppet/pops/issues.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Where a hiera.yaml came from, its error messages and its version number.

Ports the version dispatch of Puppet's ``pops/lookup/hiera_config.rb`` and the
file/line suffix of ``issues.rb``.
"""

from __future__ import annotations

import re
import typing as _ty

from pathlib_next import Path

from ..backends._psych import RubySymbol
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


def _ruby_type_name(value) -> str:
    """The Puppet type name a value of this Python type reports as."""
    if isinstance(value, bool):
        return "Boolean"
    if value is None:
        return "Undef"
    if isinstance(value, RubySymbol):
        # A Ruby Symbol value survives `symkeys_to_string` (only dict keys are
        # normalized) and has no Puppet type of its own: Puppet's TypeCalculator
        # reports a bare `Runtime`.
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
