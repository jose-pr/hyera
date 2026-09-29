"""Lookup adapters for matching lookup_options against keys.

Ports Puppet's ``lookup_adapter.rb``.
"""

import re


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
