"""Lookup adapters for matching lookup_options against keys.

Ports Puppet's ``lookup_adapter.rb``.
"""

import re

from .exceptions import HieraLookupError
from ._new_function import new_instance
from ._type_parser import parse_type


def _is_regex(pattern):
    """Hiera 5's rule: a lookup_options key is a regex only when ``^``-anchored.

    A key without a leading ``^`` is matched literally, so a dotted key like
    ``db.port`` never regex-matches an unrelated key such as ``dbxport``.
    """
    return isinstance(pattern, str) and pattern.startswith("^")


def _extract_lookup_options_for_key(key, options):
    """Return the merged ``lookup_options`` entry matching ``key``, or None.

    ``lookup_options`` is a reserved data key: ``{pattern: {merge, convert_to}}``.
    Higher-priority (earlier) levels win per pattern. An exact key match
    wins over a regex pattern match; the first regex match otherwise wins.
    """
    if options is None:
        return None
    if key in options and isinstance(options[key], dict):
        return options[key]
    for pattern, entry in options.items():
        if not isinstance(entry, dict):
            continue
        if _is_regex(pattern):
            try:
                if re.fullmatch(pattern, key):
                    return entry
            except re.error:
                continue
    return None


def convert_result(key, convert_to, value):
    """Apply a ``lookup_options`` ``convert_to`` spec to a found value.

    Ports ``lookup_adapter.rb:98-124``. ``convert_to`` is ``None`` (no
    conversion), a type-expression string, a ``[type, *args]`` list passed
    to Puppet's ``new()``, or (rare, but Puppet accepts it) a bare non-list,
    non-string value used directly as the ``type`` argument to ``new()``
    (which then fails its own "expects a Type value" check). An empty list's
    first element is ``None``, same as an explicit ``convert_to: ~``.

    A ``str`` first element is parsed with :func:`hyera._type_parser.parse_type`;
    a parse failure re-raises as "Invalid data type in lookup_options for
    key '<key>' could not parse '<source>', error: '<msg>" (the unbalanced
    quote is Puppet's own format string, verbatim). The (possibly parsed)
    type and the remaining arguments then go to
    :func:`hyera._new_function.new_instance`; its failure re-raises as "The
    convert_to lookup_option for key '<key>' raised error: <msg>".
    """
    if convert_to is None:
        return value
    if isinstance(convert_to, list):
        spec = list(convert_to)
    else:
        spec = [convert_to]
    type_arg = spec[0] if spec else None
    rest = spec[1:]

    if isinstance(type_arg, str):
        try:
            type_ = parse_type(type_arg)
        except HieraLookupError as e:
            raise HieraLookupError(
                "Invalid data type in lookup_options for key '{}' could not "
                "parse '{}', error: '{}".format(key, type_arg, e)
            ) from e
    else:
        type_ = type_arg

    try:
        return new_instance(type_, value, *rest)
    except HieraLookupError as e:
        raise HieraLookupError(
            "The convert_to lookup_option for key '{}' raised error: {}".format(key, e)
        ) from e
