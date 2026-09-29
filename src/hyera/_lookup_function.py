"""The public ``lookup()`` call: Puppet's ``functions/lookup.rb`` dispatch
(the five call forms) plus ``pops/lookup.rb``'s ``Lookup.lookup`` (name
precedence, subjects, messages).

Original code; no phiera/Puppet-source header (see ``core.Hiera.lookup``,
which is the thin method wrapping this module).
"""

import typing as _ty

from ._invocation import _STRICT
from ._navigation import _MISSING
from ._type_parser import parse_type
from ._type_mismatch import assert_instance_of
from .exceptions import KeyNotFoundError

#: The keyword-equivalent option names a form-4/5 options hash may carry
#: (never ``"name"``, which is form 4's own key, and never ``"block"``,
#: which Puppet's dispatcher always takes as a separate block argument).
_OPTION_KEYS = (
    "value_type",
    "default_value",
    "override",
    "default_values_hash",
    "merge",
)


class LookupCall(_ty.NamedTuple):
    """One parsed ``lookup()`` call, every form normalized to the same
    shape (``functions/lookup.rb:151-223``)."""

    #: The name argument exactly as given (a ``str``, or the possibly-empty
    #: ``list`` a form-4 hash's own ``"name"`` unpacks to) -- what a
    #: default ``block`` is called with.
    name: "_ty.Union[str, list]"
    #: ``name`` as a tuple, always -- a single-element tuple for a ``str``.
    names: tuple
    #: A parsed type instance, or ``None``.
    value_type: object
    merge: object
    #: Whether a default value was given at all (``default_value=None`` is
    #: a real default; Puppet's own ``has_default``).
    has_default: bool
    default_value: object
    default_values_hash: dict
    override: dict
    block: object


def _hash_given(value) -> bool:
    return value is not None


def _default_given(value) -> bool:
    return value is not _MISSING


def _check_no_other_option_given(
    merge, default_value, default_values_hash, override, value_type=None
):
    if (
        value_type is not None
        or merge is not None
        or _default_given(default_value)
        or _hash_given(default_values_hash)
        or _hash_given(override)
    ):
        raise TypeError(
            "lookup(): an options hash cannot be combined with another "
            "positional argument or option keyword (block is the only "
            "exception)"
        )


def _validate_dict_keys(d, allowed, what):
    if not isinstance(d, dict) or not all(isinstance(k, str) for k in d):
        raise TypeError("lookup(): {} must be a dict with str keys".format(what))
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise TypeError("lookup(): unknown option(s) {}".format(unknown))


def _validate_name(name) -> None:
    if isinstance(name, str):
        return
    if isinstance(name, list) and all(isinstance(n, str) for n in name):
        return
    raise TypeError(
        "lookup(): name must be a str or a list of str, not {}".format(
            type(name).__name__
        )
    )


def _validate_merge(merge) -> None:
    if merge is None:
        return
    if isinstance(merge, str):
        if merge == "":
            raise TypeError("lookup(): merge must not be an empty string")
        return
    if isinstance(merge, dict) and all(isinstance(k, str) for k in merge):
        return
    raise TypeError(
        "lookup(): merge must be a str or a dict with str keys, not {}".format(
            type(merge).__name__
        )
    )


def _validate_hash_option(value, what) -> None:
    if value is None:
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return
    raise TypeError("lookup(): {} must be a dict with str keys".format(what))


def parse_call(
    name, value_type, merge, default_value, default_values_hash, override, block
) -> LookupCall:
    """Puppet's five ``lookup()`` call forms (``functions/lookup.rb:151-
    223``), normalized to one shape. Every call-shape problem raises
    ``TypeError``, as Python itself reports a bad argument; everything
    data/string-shaped (an unparsable ``value_type``) is left to
    :func:`~hyera._type_parser.parse_type`, which raises
    ``hyera.HieraLookupError``.
    """
    if isinstance(name, dict):
        # Form 4: {name => ..., <option> => ..., ...}. block is still its
        # own, separate argument -- Puppet's own dispatcher allows it here.
        _validate_dict_keys(name, ("name",) + _OPTION_KEYS, "a name hash")
        if "name" not in name:
            raise TypeError("lookup(): a name hash must have a 'name' key")
        _check_no_other_option_given(
            merge, default_value, default_values_hash, override, value_type
        )
        real_name = name["name"]
        opts = name
    elif isinstance(value_type, dict):
        # Form 5: lookup(name, {<option> => ...}). "name" is not a key
        # here -- the real name is the call's own first argument.
        _validate_dict_keys(value_type, _OPTION_KEYS, "an options hash")
        _check_no_other_option_given(
            merge, default_value, default_values_hash, override
        )
        real_name = name
        opts = value_type
    else:
        real_name = name
        opts = None

    if opts is not None:
        value_type = opts.get("value_type")
        merge = opts.get("merge")
        default_value = opts.get("default_value", _MISSING)
        default_values_hash = opts.get("default_values_hash")
        override = opts.get("override")

    _validate_name(real_name)
    if value_type is not None and not isinstance(value_type, str):
        raise TypeError(
            "lookup(): value_type must be a str, not {}".format(
                type(value_type).__name__
            )
        )
    parsed_type = parse_type(value_type) if value_type is not None else None
    _validate_merge(merge)
    _validate_hash_option(override, "override")
    _validate_hash_option(default_values_hash, "default_values_hash")
    if block is not None and not callable(block):
        raise TypeError("lookup(): block must be callable")

    names = tuple(real_name) if isinstance(real_name, list) else (real_name,)
    return LookupCall(
        name=real_name,
        names=names,
        value_type=parsed_type,
        merge=merge,
        has_default=_default_given(default_value),
        default_value=None if default_value is _MISSING else default_value,
        default_values_hash=default_values_hash or {},
        override=override or {},
        block=block,
    )


def _assert(call: LookupCall, subject: str, value):
    if call.value_type is not None:
        assert_instance_of(subject, call.value_type, value)
    return value


def lookup(call: LookupCall, invocation, search):
    """Puppet's ``Lookup.lookup`` (``pops/lookup.rb:24-96``): the single
    entry every public engine call (``Hiera.lookup``, and ``dig``/``get``/
    ``explain`` built on top of it) shares.

    ``search(key, invocation, merge) -> value | _MISSING`` is the host's
    full lookup (``core.Hiera._search_and_merge``). Per name, in order: the
    override hash, else ``search``; the first name that finds either wins.
    On a full miss: ``default_values_hash`` per name, then ``call.block``
    (given ``call.name`` exactly as passed), then ``call.default_value``
    (only when one was actually given); otherwise ``KeyNotFoundError``.
    ``call.value_type``, when given, asserts every one of these outcomes
    with Puppet's own subject text.

    Binds :data:`~hyera._invocation._STRICT` from ``invocation.scope.strict``
    for the whole call and resets it in ``finally`` -- the one place that
    binding happens, so every caller that reaches the engine through here
    reads the bound scope's own strictness, the same way ``core.Hiera._get``
    used to before this module existed.
    """
    strict_token = _STRICT.set(invocation.scope.strict)
    try:
        for name in call.names:
            if name in invocation.override_values:
                return _assert(
                    call,
                    "Value found for key '{}' in override hash".format(name),
                    invocation.override_values[name],
                )
            value = search(name, invocation, call.merge)
            if value is not _MISSING:
                return _assert(call, "Found value", value)

        for name in call.names:
            if name in invocation.default_values:
                return _assert(
                    call,
                    "Value found for key '{}' in default values hash".format(name),
                    invocation.default_values[name],
                )

        if call.block is not None:
            return _assert(
                call, "Value returned from default block", call.block(call.name)
            )

        if call.has_default:
            return _assert(call, "Default value", call.default_value)

        raise KeyNotFoundError(list(call.names)) from None
    finally:
        _STRICT.reset(strict_token)


def nested_lookup(key, invocation, search):
    """A nested lookup from inside a value's own interpolation
    (``%{hiera()}``/``%{lookup()}``/``%{alias()}``): ``interpolation.
    rb:77-86`` -- the override hash, else a full lookup with ``merge=None``,
    else the default values hash, else a miss. Named apart from
    :func:`~hyera._navigation.sub_lookup` (a different kind of "sub"
    lookup: this one is a full nested Hiera lookup, not a dig into an
    already-found value).
    """
    if key in invocation.override_values:
        return invocation.override_values[key]
    value = search(key, invocation, None)
    if value is not _MISSING:
        return value
    if key in invocation.default_values:
        return invocation.default_values[key]
    return _MISSING
