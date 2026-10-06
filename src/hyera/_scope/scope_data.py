# Ported from Puppet 8 lib/puppet/parser/scope.rb, node/facts.rb,
# context/trusted_information.rb (https://github.com/puppetlabs/puppet),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Validating and sanitizing the data a :class:`~hyera.Scope` is built from.

Variables, facts and trusted data are checked as Puppet ``Data``, fact values
are sanitized as ``node/facts.rb`` does, and a local ``trusted`` hash is
rebuilt as ``context/trusted_information.rb`` does.
"""

from __future__ import annotations

from collections.abc import Mapping


def _tag(value):
    """A hashable, type-tagged rendering of a Puppet Data value, so ``True``,
    ``1`` and ``1.0`` never compare or hash equal (unlike plain Python)."""
    if isinstance(value, bool):
        return ("bool", value)
    if value is None:
        return ("none", None)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("float", value)
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_tag(v) for v in value))
    if isinstance(value, dict):
        return ("dict", tuple((_tag(k), _tag(v)) for k, v in value.items()))
    return ("other", value)


def _check_data(value):
    """Validate ``value`` as Puppet Data (``parser/scope.rb:835-851``'s
    ``deep_freeze``) and return a deep copy, recursively. A tuple becomes a
    list. Raises ``TypeError("Unsupported data type: '<type name>'")`` for
    anything else, including a non-``str`` dict key."""
    if isinstance(value, bool):
        return value
    if value is None or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_check_data(v) for v in value]
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError("Unsupported data type: '{}'".format(type(k).__name__))
            result[k] = _check_data(v)
        return result
    raise TypeError("Unsupported data type: '{}'".format(type(value).__name__))


def _check_mapping_keys(value, name):
    """``None``, or a shallow copy of ``value`` with every key checked to be
    a ``str``. Raises ``TypeError`` for a non-``Mapping`` or a non-``str``
    key."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(
            "{} must be a mapping, not {}".format(name, type(value).__name__)
        )
    for k in value:
        if not isinstance(k, str):
            raise TypeError(
                "{} keys must be strings, not {}".format(name, type(k).__name__)
            )
    return dict(value)


def _check_data_mapping(value, name):
    """``None``, or ``value``'s keys checked (``_check_mapping_keys``) with
    every value validated and deep-copied as Puppet Data (``_check_data``)."""
    checked = _check_mapping_keys(value, name)
    if checked is None:
        return None
    return {k: _check_data(v) for k, v in checked.items()}


def _sanitize_fact(value):
    """Puppet's ``node/facts.rb:140-163`` ``sanitize_fact``: recurse dicts
    and lists/tuples (tuples become lists); keep ``bool``/``int``/``float``/
    ``str``; ``None`` becomes ``""``; anything else becomes ``str(value)``.
    A list/tuple used as a dict key raises ``TypeError`` (Ruby has no such
    restriction; Python dicts do)."""
    if isinstance(value, dict):
        return {_sanitize_fact_key(k): _sanitize_fact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_fact(v) for v in value]
    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    if value is None:
        return ""
    return str(value)


def _sanitize_fact_key(key):
    if isinstance(key, (list, tuple)):
        raise TypeError("a {} cannot be used as a fact key".format(type(key).__name__))
    return _sanitize_fact(key)


def _strip_qualifier(name):
    """Strip one leading ``::``; a remaining ``::`` still marks the name
    qualified (``parser/scope.rb``: a Puppet class-scoped variable)."""
    unqualified = name[2:] if name.startswith("::") else name
    return "::" in unqualified, unqualified


def _split_certname(certname):
    """Ruby ``certname.split('.', 2)``: ``None``/``""`` gives ``(None,
    None)``; no dot gives ``(certname, None)``; otherwise the two parts of a
    limit-2 split."""
    if not certname:
        return None, None
    if "." not in certname:
        return certname, None
    hostname, domain = certname.split(".", 1)
    return hostname, domain


def _local_trusted(clientcert):
    """The local trusted-data hash (``trusted_information.rb:67-73``), key
    order ``authenticated``, ``certname``, ``extensions``, ``hostname``,
    ``domain``, ``external``."""
    hostname, domain = _split_certname(clientcert)
    return {
        "authenticated": "local",
        "certname": clientcert,
        "extensions": {},
        "hostname": hostname,
        "domain": domain,
        "external": {},
    }
