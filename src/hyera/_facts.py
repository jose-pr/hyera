# Ported from Puppet 8 lib/puppet/application/lookup.rb, util/yaml.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Facts: loading a Puppet ``--facts`` file, and running bare ``facter``.

Ports ``application/lookup.rb``'s ``--facts`` handling and
``Puppet::Util::Yaml.safe_load``'s no-permitted-classes rule.
"""

import json
import os
import shutil
import subprocess

from . import _yaml_loader
from .backends import _reject_json_constant
from .exceptions import BackendError

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
    if isinstance(value, _yaml_loader.RubySymbol):
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
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise BackendError("({}): {}".format(label, e), path=label) from e
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as e:
        raise BackendError(
            "({}): {} at line {} column {}".format(label, e.msg, e.lineno, e.colno),
            path=label,
        ) from e
    except ValueError as e:  # from _reject_json_constant (NaN/Infinity)
        raise BackendError("({}): {}".format(label, e), path=label) from e


def _parse_yaml_facts(raw: bytes, label: str):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise BackendError("({}): {}".format(label, e), path=label) from e
    try:
        parsed = _yaml_loader.safe_load(text)
    except BackendError as e:
        raise BackendError("({}): {}".format(label, e), path=label) from e
    _reject_symbols(parsed, label)
    return parsed


def load_facts(path) -> dict:
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


def facts_from_facter(*, timeout: int = 30) -> dict:
    """Run a bare ``facter -j`` and return its facts.

    No queries, no ``--show-legacy``: a queried ``facter -j a b`` returns
    flat dotted keys that ``$facts`` cannot navigate. Does not add
    ``clientcert``/``clientversion``/``clientnoop`` -- those come from
    Puppet's agent, not facter; add ``clientcert`` yourself if
    ``trusted.certname`` should be set. Raises :class:`~hyera.BackendError`
    for a missing binary, a timeout, a non-zero exit (stderr captured), or
    output that is not a JSON object.
    """
    exe = shutil.which("facter")
    if exe is None:
        raise BackendError("facter executable not found on PATH")

    timed_out = False
    try:
        proc = subprocess.run(
            [exe, "-j"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Recorded, not re-raised, inside the except: matches the sops
        # runner's own hardening (R4b) -- raising outside the handler keeps
        # __context__ genuinely None rather than holding the partial output.
        timed_out = True
    except OSError as e:
        raise BackendError("Failed to run facter: {}".format(e)) from e

    if timed_out:
        raise BackendError("facter timed out after {}s".format(timeout)) from None

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise BackendError(
            "facter failed (exit {}): {}".format(
                proc.returncode, detail or "<no stderr>"
            )
        )

    try:
        text = proc.stdout.decode("utf-8")
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
