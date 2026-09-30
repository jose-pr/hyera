# Ported from Puppet 8 lib/puppet/parser/compiler.rb, parser/scope.rb,
# node.rb, context/trusted_information.rb, node/facts.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""Scope: Puppet's top scope, as a value object.

Ports the node-parameter/fact/trusted-data build Puppet's compiler runs
before any manifest evaluates (``parser/compiler.rb``'s ``set_node_parameters``,
``node.rb``, ``context/trusted_information.rb``, ``node/facts.rb``'s
``sanitize_fact``) and the variable-lookup rules of ``parser/scope.rb``.
"""

import logging
import re
import threading
import typing as _ty
from collections.abc import Mapping

from .._lookup.navigation import _MISSING, _ruby_class
from ..exceptions import InterpolationError

_LOGGER = logging.getLogger(__name__)

_STRICT_VALUES = ("off", "warning", "error")
#: Names a Hiera 5 lookup may never bind directly (Puppet's privileged
#: ``setvar`` targets; only ``set_node_parameters`` itself may set them).
_RESERVED_TOP = ("trusted", "facts", "server_facts")
#: ``pops/patterns.rb:59`` ``NUMERIC_VAR_NAME``.
_NUMERIC_NAME_RE = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")
#: The three keys ``node.rb:227-238`` requires to "resurrect" a ``trusted``
#: fact/variable instead of falling back to the local hash.
_TRUSTED_REQUIRED_KEYS = frozenset(("authenticated", "certname", "extensions"))


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


class _WarnState:
    """Per-root warning-dedup state, shared by every ``Scope`` derived from
    one root (``with_local_scope``/``derive``) -- Puppet's warn-once-per-run
    (``util/logging.rb``'s ``warn_once``, ``util/warnings.rb``'s ``warnonce``)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._variable_keys = set()
        self._messages = set()

    def __getstate__(self):
        # A threading.Lock cannot be pickled/deepcopied; a copy starts with
        # a fresh, unlocked one (harmless -- dedup state itself still
        # carries over, and a lock is never held across a copy/pickle
        # boundary anyway). Keeps `Hiera` and its scoped views (holding a
        # `Scope`, holding this) picklable and deep-copyable, as documented.
        with self._lock:
            return {
                "_variable_keys": set(self._variable_keys),
                "_messages": set(self._messages),
            }

    def __setstate__(self, state):
        self._lock = threading.Lock()
        self._variable_keys = state["_variable_keys"]
        self._messages = state["_messages"]

    def variable_once(self, name) -> bool:
        """True the first time an undefined-variable warning for ``name`` is
        due (up to 100 distinct keys tracked, matching Puppet's own cap);
        False on a repeat."""
        key = "Variable: {}".format(name)
        with self._lock:
            if key in self._variable_keys:
                return False
            if len(self._variable_keys) < 100:
                self._variable_keys.add(key)
            return True

    def message_once(self, message) -> bool:
        """True the first time this exact message text is seen; False on a
        repeat."""
        with self._lock:
            if message in self._messages:
                return False
            self._messages.add(message)
            return True


def _warn_collision(warn_state, name, node_name, old, new) -> None:
    if node_name is None:
        message = (
            "The node parameter '{}' was already set to '{}'. It could not "
            "be set to '{}'.".format(name, old, new)
        )
    else:
        message = (
            "The node parameter '{}' for node '{}' was already set to '{}'. "
            "It could not be set to '{}'.".format(name, node_name, old, new)
        )
    if warn_state.message_once(message):
        _LOGGER.warning("%s", message)


class Scope:
    """Puppet's top scope: node parameters, facts, trusted data,
    server_facts, and the reserved/main-class variables, as one immutable,
    hashable value.

    Every argument is keyword-only. ``variables`` are node parameters
    (``compiler.rb``'s ``set_node_parameters``): each becomes a top-scope
    variable unless a fact of the same name already claimed it. ``facts``
    are sanitized (:func:`_sanitize_fact`, not validated as Data) and become
    top-scope variables too, plus the whole sanitized mapping under
    ``$facts``. ``server_facts`` merge the same way under both ``$<name>``
    and the whole mapping (with ``environment`` forced last) under
    ``$server_facts``. ``trusted`` defaults to Puppet's local hash
    (certname from the ``clientcert`` variable/fact); a ``trusted``
    variable/fact is "resurrected" as-is only when it is a dict holding
    ``authenticated``, ``certname`` and ``extensions``. ``environment``
    defaults to ``"production"``. ``strict`` is ``"off"``, ``"warning"``
    (default) or ``"error"`` -- Puppet's own setting, applied by
    :meth:`lookupvar`.

    Do not mutate a value returned by :meth:`lookup`/:meth:`lookupvar`: it
    is the scope's own copy, shared by every caller.

    :param variables: node parameters (top-scope variables).
    :param facts: facts, sanitized and exposed under ``$facts`` too.
    :param trusted: trusted data; Puppet's own local hash if omitted.
    :param server_facts: server facts, exposed under ``$server_facts`` too.
    :param environment: this scope's ``$environment`` (``"production"`` if
        omitted).
    :param strict: strictness for an undefined variable: ``"off"``,
        ``"warning"`` (the default) or ``"error"``.
    :param node_name: the ``--node``/node name this scope was built for.
    """

    #: Sentinel for "not found" (``None`` is a legitimate, defined value).
    UNDEFINED = _MISSING

    def __init__(
        self,
        *,
        variables: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        trusted: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        server_facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        environment: _ty.Optional[str] = None,
        strict: str = "warning",
        node_name: _ty.Optional[str] = None,
    ) -> None:
        self._build(
            variables=variables,
            facts=facts,
            trusted=trusted,
            server_facts=server_facts,
            environment=environment,
            strict=strict,
            node_name=node_name,
            warn_state=_WarnState(),
        )

    # -- construction -------------------------------------------------

    def _build(
        self,
        *,
        variables,
        facts,
        trusted,
        server_facts,
        environment,
        strict,
        node_name,
        warn_state,
    ) -> None:
        if strict not in _STRICT_VALUES:
            raise ValueError(
                "strict must be one of {!r}, not {!r}".format(_STRICT_VALUES, strict)
            )
        if environment is not None and not isinstance(environment, str):
            raise TypeError(
                "environment must be a str, not {}".format(type(environment).__name__)
            )
        if environment == "":
            raise ValueError("environment must not be empty")
        if node_name is not None and not isinstance(node_name, str):
            raise TypeError(
                "node_name must be a str, not {}".format(type(node_name).__name__)
            )

        variables_checked = _check_data_mapping(variables, "variables") or {}
        server_facts_checked = _check_data_mapping(server_facts, "server_facts") or {}
        trusted_checked = _check_data_mapping(trusted, "trusted")
        facts_checked = _check_mapping_keys(facts, "facts") or {}

        # Step 1: params = the variables, in order.
        params = dict(variables_checked)

        # Step 2: resolve $environment.
        if environment is not None:
            resolved_env = environment
        elif isinstance(params.get("environment"), str) and params["environment"]:
            resolved_env = params["environment"]
        else:
            resolved_env = "production"
        params["environment"] = resolved_env

        # Step 3: merge facts into params without overriding.
        facts_sanitized = _sanitize_fact(facts_checked)
        for name, value in facts_sanitized.items():
            if name in params:
                _warn_collision(warn_state, name, node_name, params[name], value)
            else:
                params[name] = value
        params["environment"] = resolved_env

        # Step 4: the *given* server_facts merge into params without
        # overriding, same as facts. $environment is forced into the
        # exposed $server_facts hash afterwards, last, so it always wins
        # there -- but it is never itself re-merged into params through
        # this collision-checked path: $environment already has its own
        # authoritative resolution (step 2) and storage (step 6), and
        # re-merging the same, already-resolved value here would warn a
        # spurious "already set to 'production'. It could not be set to
        # 'production'" on every construction, even with no server_facts
        # given at all.
        for name, value in server_facts_checked.items():
            if name in params:
                _warn_collision(warn_state, name, node_name, params[name], value)
            else:
                params[name] = value
        server_facts_hash = dict(server_facts_checked)
        server_facts_hash["environment"] = resolved_env

        # Step 5: trusted data.
        tp = None
        if "trusted" in params and params["trusted"] not in (None, False):
            tp = params.pop("trusted")
        if trusted_checked is not None:
            resolved_trusted = trusted_checked
        elif isinstance(tp, dict) and _TRUSTED_REQUIRED_KEYS <= set(tp):
            resolved_trusted = tp
        else:
            clientcert = params.get("clientcert")
            if clientcert is not None and not isinstance(clientcert, str):
                raise TypeError(
                    "undefined method 'split' for an instance of {}".format(
                        _ruby_class(clientcert)
                    )
                )
            resolved_trusted = _local_trusted(clientcert)

        # Step 6: the top table.
        table = {}
        for name, value in params.items():
            if name == "environment":
                continue
            if _NUMERIC_NAME_RE.match(name):
                raise ValueError(
                    "Cannot assign to a numeric match result variable "
                    "'${}'".format(name)
                )
            if name in _RESERVED_TOP:
                raise ValueError(
                    "Attempt to assign to a reserved variable name: "
                    "'{}'".format(name)
                )
            table[name] = value
        table["environment"] = resolved_env
        table["trusted"] = resolved_trusted
        table["server_facts"] = server_facts_hash
        table["facts"] = facts_sanitized

        # Step 7: main-class variables.
        for name, value in (("module_name", ""), ("title", "main"), ("name", "main")):
            if name in table:
                raise ValueError("Cannot reassign variable '${}'".format(name))
            table[name] = value

        self._table = table
        self._locals = ()
        self._warn_state = warn_state
        self._variables_input = variables_checked
        self._facts_input = facts_checked
        self._server_facts_input = server_facts_checked
        self._trusted_input = trusted_checked
        self._environment = resolved_env
        self._strict = strict
        self._node_name = node_name
        self._identity = (_tag(table), (), strict, node_name)

    # -- read-only properties ------------------------------------------

    @property
    def environment(self) -> str:
        """This scope's resolved ``$environment`` (``"production"`` by
        default)."""
        return self._environment

    @property
    def strict(self) -> str:
        """This scope's strictness for an undefined variable: ``"off"``,
        ``"warning"`` (the default) or ``"error"``."""
        return self._strict

    @property
    def node_name(self) -> _ty.Optional[str]:
        """The ``--node``/``node_name`` this scope was built for, or
        ``None``."""
        return self._node_name

    # -- lookup ----------------------------------------------------------

    def lookup(self, name: str) -> _ty.Any:
        """``self[name]``, without strict side effects (Puppet's
        ``catch(:undefined_variable)`` form): the bound value (even
        ``None``), or :data:`Scope.UNDEFINED`.

        An explicit ``::`` prefix (``%{::x}``, never just a bare ``%{x}``)
        means the literal top-scope variable and skips every local layer a
        :meth:`with_local_scope` child added -- Puppet's own qualified-name
        rule, and how a ``mapped_paths`` template's ``%{::item}`` still
        reaches a top-scope fact of the same name as the mapped item
        variable rather than shadowing it.

        :param name: the variable name, optionally ``::``-qualified.
        :returns: the bound value, or :data:`Scope.UNDEFINED`.
        :raises TypeError: ``name`` is not a ``str``.
        """
        if not isinstance(name, str):
            raise TypeError(
                "scope variable name must be a str, not {}".format(type(name).__name__)
            )
        explicit_top = name.startswith("::")
        qualified, unqualified = _strip_qualifier(name)
        if not explicit_top:
            for layer in self._locals:
                if unqualified in layer:
                    return layer[unqualified]
        if unqualified in self._table:
            return self._table[unqualified]
        if not qualified:
            if unqualified == "caller_module_name":
                return None
            if _NUMERIC_NAME_RE.match(unqualified):
                return None
        return Scope.UNDEFINED

    def exist(self, name: str) -> bool:
        """``parser/scope.rb:283-304`` ``exist?``.

        :param name: the variable name, optionally ``::``-qualified.
        :returns: whether ``name`` is bound.
        :raises TypeError: ``name`` is not a ``str``.
        """
        if not isinstance(name, str):
            raise TypeError(
                "scope variable name must be a str, not {}".format(type(name).__name__)
            )
        explicit_top = name.startswith("::")
        qualified, unqualified = _strip_qualifier(name)
        if qualified:
            return False
        if _NUMERIC_NAME_RE.match(unqualified):
            return False
        if unqualified == "caller_module_name":
            return True
        if not explicit_top:
            for layer in self._locals:
                if unqualified in layer:
                    return True
        return unqualified in self._table

    def lookupvar(self, name: str, *, lenient: bool = False) -> _ty.Any:
        """:meth:`lookup`, with Puppet's :attr:`strict` applied to a miss
        (``parser/scope.rb:491-547``). ``lenient`` is Puppet's
        ``avoid_hiera_interpolation_errors``, used for hierarchy locations.

        :param name: the variable name, optionally ``::``-qualified.
        :param lenient: warn (never raise) on an undefined variable even
            under ``strict="error"``.
        :returns: the bound value, or ``None`` for an undefined variable.
        :raises TypeError: ``name`` is not a ``str``.
        :raises InterpolationError: the variable is undefined and
            ``strict="error"`` applies (with ``lenient`` false).
        """
        value = self.lookup(name)
        if value is not Scope.UNDEFINED:
            return value
        return self._undefined(name, lenient)

    def _undefined(self, name, lenient):
        qualified, unqualified = _strip_qualifier(name)
        suffix = ""
        if qualified:
            suffix = "; class {} could not be found".format(unqualified.split("::")[0])
        if self._strict == "off":
            return None
        if self._strict == "warning":
            if self._warn_state.variable_once(name):
                _LOGGER.warning("%s", "Undefined variable '{}'{}".format(name, suffix))
            return None
        # error
        if lenient:
            if self._warn_state.variable_once(name):
                _LOGGER.warning(
                    "%s",
                    "Interpolation failed with '{}', but compilation "
                    "continuing{}".format(name, suffix),
                )
            return None
        raise InterpolationError("Undefined variable '{}'{}".format(name, suffix))

    # -- layering ----------------------------------------------------------

    def with_local_scope(self, variables: _ty.Mapping[str, _ty.Any]) -> "Scope":
        """A child scope adding one local variable layer, sharing this
        scope's table and warning state. This scope is unchanged.

        :param variables: the local layer's own variables.
        :returns: the child scope.
        :raises TypeError: ``variables`` is not a mapping.
        """
        checked = _check_mapping_keys(variables, "variables")
        if checked is None:
            raise TypeError("variables must be a mapping, not None")
        layer = {k: _check_data(v) for k, v in checked.items()}
        child = object.__new__(Scope)
        child.__dict__.update(self.__dict__)
        child._locals = (layer,) + self._locals
        child._identity = (
            self._identity[0],
            tuple(_tag(l) for l in child._locals),
            self._identity[2],
            self._identity[3],
        )
        return child

    def derive(
        self,
        *,
        variables: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        trusted: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        server_facts: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        environment: _ty.Optional[str] = None,
        strict: _ty.Optional[str] = None,
        node_name: _ty.Optional[str] = None,
    ) -> "Scope":
        """A new root scope, rebuilt from this scope's own constructor
        inputs: ``variables``/``facts``/``server_facts`` shallow-update the
        parent's (the new values win); the rest replace the parent's when
        given. Local layers are not carried; the warning state is shared.

        :param variables: node parameters, shallow-updating this scope's own.
        :param facts: facts, shallow-updating this scope's own.
        :param trusted: replaces this scope's trusted data when given.
        :param server_facts: server facts, shallow-updating this scope's own.
        :param environment: replaces this scope's ``$environment`` when given.
        :param strict: replaces this scope's strictness when given.
        :param node_name: replaces this scope's node name when given.
        :returns: the new, derived root scope.
        """
        new_variables = dict(self._variables_input)
        new_variables.update(_check_data_mapping(variables, "variables") or {})
        new_facts = dict(self._facts_input)
        new_facts.update(_check_mapping_keys(facts, "facts") or {})
        new_server_facts = dict(self._server_facts_input)
        new_server_facts.update(_check_data_mapping(server_facts, "server_facts") or {})
        new_trusted = (
            _check_data_mapping(trusted, "trusted")
            if trusted is not None
            else self._trusted_input
        )
        new_environment = environment if environment is not None else self._environment
        new_strict = strict if strict is not None else self._strict
        new_node_name = node_name if node_name is not None else self._node_name

        result = object.__new__(Scope)
        result._build(
            variables=new_variables,
            facts=new_facts,
            trusted=new_trusted,
            server_facts=new_server_facts,
            environment=new_environment,
            strict=new_strict,
            node_name=new_node_name,
            warn_state=self._warn_state,
        )
        return result

    # -- value semantics ---------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Scope):
            return NotImplemented
        return self._identity == other._identity

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self) -> int:
        return hash(self._identity)

    def __repr__(self) -> str:
        return "Scope(environment={!r}, strict={!r}, variables={}, facts={})".format(
            self._environment, self._strict, len(self._table), len(self._facts_input)
        )
