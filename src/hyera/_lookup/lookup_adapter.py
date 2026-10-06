# Ported from Puppet 8 lib/puppet/pops/lookup/lookup_adapter.rb,
# data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Lookup adapters for matching lookup_options against keys.

Ports Puppet's ``lookup_adapter.rb`` and the RichData check in
``data_provider.rb``.
"""

from __future__ import annotations

import typing as _ty

from ..exceptions import HieraLookupError
from .._types.new_function import new_instance
from .._types.parser import parse_type
from .._types.ruby_regexp import _ruby_regex
from .._types.inference import infer

#: ``Puppet::LookupValue`` (an alias for ``RichData``), parsed once and
#: cached: every value found at a root key is checked against it
#: (:func:`validate_data_value`).
_LOOKUP_VALUE_TYPE = None


def _lookup_value_type():
    global _LOOKUP_VALUE_TYPE
    if _LOOKUP_VALUE_TYPE is None:
        _LOOKUP_VALUE_TYPE = parse_type("Puppet::LookupValue")
    return _LOOKUP_VALUE_TYPE


def value_type_label(value) -> str:
    """The Puppet type of ``value`` as an error message names it; a value
    of a Python type with no Puppet counterpart is named by its class."""
    try:
        return str(infer(value))
    except TypeError:
        return type(value).__name__


def validate_data_value(value, function_name, location, root_key) -> None:
    """Puppet's RichData check on a value found at a root key
    (``data_provider.rb:84-91``), run once per location before the value
    is interpolated or enters a merge -- so one bad sibling key in a data
    file never breaks a lookup of any other key in the same file.

    ``Puppet::LookupValue`` (``RichData`` plus ``Undef``) excludes a hash
    keyed by anything other than a ``String``/numeric (a boolean, ``nil``,
    or a nested collection), and a Ruby symbol anywhere in the structure.
    A found ``None`` root value itself is valid (``Undef`` is RichData).

    ``location`` is ``None`` for a location-less hierarchy entry (a
    ``data_hash`` function called with no ``path``/``uri`` at all); the
    "when using location" clause is then omitted entirely, matching
    Puppet's own conditional message.
    """
    t = _lookup_value_type()
    if not t.instance(value):
        if location is None:
            raise HieraLookupError(
                "Value for key '{}', in hash returned from data_hash "
                "function '{}', has wrong type, expects Puppet::LookupValue, "
                "got {}".format(root_key, function_name, value_type_label(value))
            )
        raise HieraLookupError(
            "Value for key '{}', in hash returned from data_hash function "
            "'{}', when using location '{}', has wrong type, expects "
            "Puppet::LookupValue, got {}".format(
                root_key, function_name, location, value_type_label(value)
            )
        )


def validate_lookup_options(options, module_name=None):
    """Puppet's ``LookupAdapter#validate_lookup_options`` (:298-315).

    ``None`` (no ``lookup_options`` key at all) passes through unchanged;
    anything other than a hash raises, naming Puppet's own message.

    With a ``module_name``, every key must be qualified with that module's
    own name: a ``^``-prefixed pattern's prefix (up to the ``::`` a real
    module key would have) must equal ``"<module_name>::"``, or "all
    lookup_options patterns must match a key starting with module name
    '<module_name>'"; any other key must itself start with
    ``"<module_name>::"``, or "all lookup_options keys must start with
    module name '<module_name>'". A non-``str`` key counts as unqualified
    either way (Ruby would crash with ``NoMethodError`` there; this is the
    closest faithful outcome).
    """
    if options is None:
        return None
    if not isinstance(options, dict):
        raise HieraLookupError("value of lookup_options must be a hash")
    if module_name is None:
        return options
    prefix = module_name + "::"
    for key in options:
        if isinstance(key, str) and key.startswith("^"):
            if key[1 : 1 + len(prefix)] != prefix:
                raise HieraLookupError(
                    "all lookup_options patterns must match a key starting "
                    "with module name '{}'".format(module_name)
                )
        elif not (isinstance(key, str) and key.startswith(prefix)):
            raise HieraLookupError(
                "all lookup_options keys must start with module name "
                "'{}'".format(module_name)
            )
    return options


class CompiledOptions(_ty.NamedTuple):
    """A ``lookup_options`` mapping split into exact and pattern entries.

    ``patterns`` keeps the mapping's own order (lower-priority levels'
    patterns first, per the merge port's hash-strategy precedence), since
    the first pattern that matches wins.
    """

    exact: dict
    patterns: tuple


def compile_patterns(options):
    """Puppet's ``LookupAdapter#compile_patterns`` (:317-330).

    A key starting with ``^`` is a Ruby regex, compiled through
    :func:`hyera._types.ruby_regexp._ruby_regex`; anything else, including a non-``str``
    key, is an exact match. An invalid pattern raises immediately (no
    rescue, as in Puppet), naming the pattern.
    """
    if options is None:
        return None
    exact = {}
    patterns = []
    for key, entry in options.items():
        if isinstance(key, str) and key.startswith("^"):
            patterns.append((_ruby_regex(key), entry))
        else:
            exact[key] = entry
    return CompiledOptions(exact=exact, patterns=tuple(patterns))


def extract_lookup_options_for_key(root_key, compiled):
    """Puppet's ``LookupAdapter#extract_lookup_options_for_key`` (:249-262).

    The exact entry on ``root_key`` unless it is ``None``, else the value of
    the first pattern that matches by *searching* from the start of
    ``root_key`` (Ruby ``=~``, not a full match), in the compiled mapping's
    order. No match at all gives ``None``.
    """
    if compiled is None:
        return None
    entry = compiled.exact.get(root_key)
    if entry is not None:
        return _entry_options(entry, root_key)
    for pattern, entry in compiled.patterns:
        if pattern.search(root_key):
            return _entry_options(entry, root_key)
    return None


def _entry_options(entry, root_key):
    """The options a matched ``lookup_options`` entry contributes.

    A hash is used as-is. A string (or an exact ``None``, which only
    reaches here through the caller's own ``is not None`` check when a
    *pattern* entry's value is ``None``) applies no options at all -- Ruby's
    ``String#[]``/``NilClass#[]`` are both ``nil``, so Puppet neither merges
    nor converts, and (for an exact match) never falls through to a pattern.
    Anything else (an Array, Integer, Boolean, ...) is not a shape Puppet's
    ``Hash#[]`` would ever see there without raising; Puppet's own error is a
    Ruby internal, so this raises our own message instead.
    """
    if isinstance(entry, dict):
        return entry
    if entry is None or isinstance(entry, str):
        return None
    raise HieraLookupError(
        "The lookup_options entry for key '{}' is not a hash".format(root_key)
    )


def convert_result(key, convert_to, value, invocation=None):
    """Apply a ``lookup_options`` ``convert_to`` spec to a found value.

    Ports ``lookup_adapter.rb:98-124``. ``convert_to`` is ``None`` (no
    conversion), a type-expression string, a ``[type, *args]`` list passed
    to Puppet's ``new()``, or (rare, but Puppet accepts it) a bare non-list,
    non-string value used directly as the ``type`` argument to ``new()``
    (which then fails its own "expects a Type value" check). An empty list's
    first element is ``None``, same as an explicit ``convert_to: ~``.

    A ``str`` first element is parsed with :func:`hyera._types.parser.parse_type`;
    a parse failure re-raises as "Invalid data type in lookup_options for
    key '<key>' could not parse '<source>', error: '<msg>" (the unbalanced
    quote is Puppet's own format string, verbatim). The (possibly parsed)
    type and the remaining arguments then go to
    :func:`hyera._types.new_function.new_instance`; its failure re-raises as "The
    convert_to lookup_option for key '<key>' raised error: <msg>".

    ``invocation``, when given, reports the applied conversion as explain
    text (``lookup_adapter.rb:117``) -- called after the root ``data`` node
    has already closed (a no-op while its own explainer is unset, same as
    every other recording hook).
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

    if invocation is not None:
        from .._types.string_converter import convert as _puppet_string

        invocation.report_text(
            lambda: "Applying convert_to lookup_option with arguments "
            + _puppet_string([type_] + rest)
        )

    try:
        return new_instance(type_, value, *rest)
    except Exception as e:
        raise HieraLookupError(
            "The convert_to lookup_option for key '{}' raised error: {}".format(key, e)
        ) from e
