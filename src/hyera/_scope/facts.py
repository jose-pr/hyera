# Ported from Puppet 8 lib/puppet/application/lookup.rb, util/yaml.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Facts: loading a Puppet ``--facts`` file, and running bare ``facter``.

Ports ``application/lookup.rb``'s ``--facts`` handling and
``Puppet::Util::Yaml.safe_load``'s no-permitted-classes rule.
"""

import json
import os
import typing as _ty

from ..backends import _psych as _psych
from ..backends._json import (
    TOO_DEEP,
    _reject_json_constant,
    check_json_value,
    loads_json,
)
from .._subprocess import run as _run
from ..exceptions import BackendError

#: The four trusted-identity facts Puppet requires all-or-nothing
#: (``application/lookup.rb:12`` ``TRUSTED_INFORMATION_FACTS``).
_TRUSTED_FACT_NAMES = ("hostname", "domain", "fqdn", "clientcert")

#: Sentinel meaning "this parser did not produce a result" (an unrecognized
#: extension tries JSON, then YAML; either failing is not itself an error).
_NO_RESULT = object()


def _read_bytes(path) -> bytes:
    reader = getattr(path, "read_bytes", None)
    if reader is not None:
        return reader()
    with open(os.fspath(path), "rb") as fh:
        return fh.read()


def _reject_symbols(value, label) -> None:
    """Recursively raise if ``value`` contains a :class:`RubySymbol`
    (key or value) -- ``Puppet::Util::Yaml.safe_load`` (unlike the
    data-file/hiera.yaml loader) permits no classes at all, Symbol
    included."""
    if isinstance(value, _psych.RubySymbol):
        raise BackendError(
            "({}): Tried to load unspecified class: Symbol".format(label), path=label
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_symbols(k, label)
            _reject_symbols(v, label)
    elif isinstance(value, list):
        for item in value:
            _reject_symbols(item, label)


def _parse_json_facts(raw: bytes, label: str):
    # Every error is raised after its handler, so no chained exception holds
    # the document (a decode error's `.object`, a JSON error's `.doc`).
    problem = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        problem = str(e)
    if problem is None:
        try:
            parsed = loads_json(text)
            check_json_value(parsed, surrogates=False)
            return parsed
        except json.JSONDecodeError as e:
            problem = "{} at line {} column {}".format(e.msg, e.lineno, e.colno)
        except RecursionError:
            problem = TOO_DEEP
        except ValueError as e:  # from _reject_json_constant (NaN/Infinity)
            problem = str(e)
    raise BackendError("({}): {}".format(label, problem), path=label)


def _parse_yaml_facts(raw: bytes, label: str):
    problem = None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        problem = str(e)
    if problem is None:
        try:
            parsed = _psych.safe_load(text)
        except BackendError as e:
            problem = str(e)
    if problem is not None:
        raise BackendError("({}): {}".format(label, problem), path=label)
    _reject_symbols(parsed, label)
    return parsed


def load_facts(path: _ty.Union[str, "os.PathLike[str]"]) -> _ty.Dict[str, _ty.Any]:
    """Read a Puppet ``--facts`` file (``application/lookup.rb:349-371``).

    ``path`` is a ``str`` or ``os.PathLike``; every message uses
    ``str(path)`` as given. ``.json`` parses as JSON, ``.yml``/``.yaml`` as
    YAML (``Puppet::Util::Yaml.safe_load``'s rules -- no permitted classes
    at all: a YAML date/time/symbol raises, unlike a hiera.yaml/data file,
    which permits Symbol); any other extension tries JSON, then YAML, and a
    failure of either there is not itself an error. The result must be a
    mapping, and ``hostname``/``domain``/``fqdn``/``clientcert`` must be
    all-or-nothing. Raises :class:`~hyera.BackendError` (``.path`` set) for
    every failure; the caller (typically :class:`~hyera.Scope`) sanitizes
    the returned dict -- this function does not.

    :param path: the facts file to read.
    :returns: the parsed facts, unsanitized.
    :raises BackendError: the file could not be read or parsed, is not a
        mapping, or has only some of ``hostname``/``domain``/``fqdn``/
        ``clientcert``.
    """
    label = str(path)
    raw = _read_bytes(path)
    suffix = os.path.splitext(os.fspath(path))[1]

    if suffix == ".json":
        parsed = _parse_json_facts(raw, label)
    elif suffix in (".yml", ".yaml"):
        parsed = _parse_yaml_facts(raw, label)
    else:
        parsed = _NO_RESULT
        try:
            parsed = _parse_json_facts(raw, label)
        except BackendError:
            pass
        if parsed is _NO_RESULT:
            try:
                parsed = _parse_yaml_facts(raw, label)
            except BackendError:
                pass
        if parsed is _NO_RESULT:
            parsed = None

    if not isinstance(parsed, dict):
        raise BackendError(
            "Incorrectly formatted data in {} given via the --facts flag "
            "(only accepts yaml and json files)".format(label),
            path=label,
        )

    present = [name for name in _TRUSTED_FACT_NAMES if name in parsed]
    if present and len(present) != len(_TRUSTED_FACT_NAMES):
        raise BackendError(
            "When overriding any of the hostname,domain,fqdn,clientcert "
            "facts with {} given via the --facts flag, they must all be "
            "overridden.".format(label),
            path=label,
        )

    return parsed


def facts_from_facter(*, timeout: int = 30) -> _ty.Dict[str, _ty.Any]:
    """Run a bare ``facter -j`` and return its facts.

    No queries, no ``--show-legacy``: a queried ``facter -j a b`` returns
    flat dotted keys that ``$facts`` cannot navigate. Does not add
    ``clientcert``/``clientversion``/``clientnoop`` -- those come from
    Puppet's agent, not facter; add ``clientcert`` yourself if
    ``trusted.certname`` should be set. Raises :class:`~hyera.BackendError`
    for a missing binary, a non-zero exit (the last 2,000 characters of
    stderr), or output that is not a JSON object; a timeout raises
    :class:`~hyera.BackendTimeoutError` after killing facter and its
    children. A ``facter`` that ``PATH`` resolves relative to the current
    directory is refused.

    :param timeout: seconds to wait for ``facter`` before giving up.
    :returns: the parsed facts, unsanitized.
    :raises BackendTimeoutError: ``facter`` did not finish in ``timeout``.
    :raises BackendError: ``facter`` is missing, refused, exits non-zero, or
        its output is not a JSON object.
    """
    stdout = _run("facter", ["-j"], timeout=timeout)

    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BackendError("facter output is not valid UTF-8: {}".format(e)) from e
    try:
        result = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as e:
        raise BackendError("facter output is not valid JSON: {}".format(e)) from e
    except ValueError as e:
        raise BackendError("facter output is not valid JSON: {}".format(e)) from e
    if not isinstance(result, dict):
        raise BackendError("facter output is not a JSON object")
    return result
