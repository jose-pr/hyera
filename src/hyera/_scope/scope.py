# Ported from Puppet 8 lib/puppet/parser/compiler.rb, parser/scope.rb, node.rb,
# context/trusted_information.rb, node/facts.rb (https://github.com/puppetlabs/puppet),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Scope: Puppet's top scope, as a value object.

Ports the node-parameter/fact/trusted-data build Puppet's compiler runs
before any manifest evaluates (``parser/compiler.rb``'s ``set_node_parameters``,
``node.rb``, ``context/trusted_information.rb``, ``node/facts.rb``'s
``sanitize_fact``) and the variable-lookup rules of ``parser/scope.rb``.
"""

from __future__ import annotations

import logging
import re
import threading
import typing as _ty

from .._lookup.navigation import _MISSING, _ruby_class
from ..exceptions import InterpolationError
from .._enums import _StrEnum, _plain
from .scope_data import (
    _check_data,
    _check_data_mapping,
    _check_mapping_keys,
    _local_trusted,
    _sanitize_fact,
    _strip_qualifier,
    _tag,
)

_LOGGER = logging.getLogger(__name__)


class Strict(_StrEnum):
    """Strictness for an undefined variable: the ``strict=`` argument of
    :class:`~hyera.Scope`/:meth:`~hyera.Scope.derive` and
    :meth:`~hyera.Hiera.scoped`, Puppet's own ``strict`` setting
    (applied by :meth:`~hyera.Scope.lookupvar`)."""

    OFF = "off"
    """An undefined variable resolves to ``None``, silently."""

    WARNING = "warning"
    """An undefined variable resolves to ``None``, logged once per name."""

    ERROR = "error"
    """An undefined variable raises :class:`~hyera.InterpolationError`."""


# : Plain strings, not :class:`Strict` members: interpolated into the ``ValueError``
# below, whose text : shows ``'off'``, not ``repr()`` of a member (``<Strict.OFF:
# 'off'>``).
_STRICT_VALUES = ("off", "warning", "error")
#: Names a Hiera 5 lookup may never bind directly (Puppet's privileged
#: ``setvar`` targets; only ``set_node_parameters`` itself may set them).
_RESERVED_TOP = ("trusted", "facts", "server_facts")
#: ``pops/patterns.rb:59`` ``NUMERIC_VAR_NAME``.
_NUMERIC_NAME_RE = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")
#: The three keys ``node.rb:227-238`` requires to "resurrect" a ``trusted``
#: fact/variable instead of falling back to the local hash.
_TRUSTED_REQUIRED_KEYS = frozenset(("authenticated", "certname", "extensions"))


class _WarnState:
    """Per-root warning-dedup state, shared by every ``Scope`` derived from
    one root (``with_local_scope``/``derive``) -- Puppet's warn-once-per-run
    (``util/logging.rb``'s ``warn_once``, ``util/warnings.rb``'s ``warnonce``)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._variable_keys = set()
        self._messages = set()

    def __getstate__(self):
        # A threading.Lock cannot be pickled/deepcopied; a copy starts with a fresh,
        # unlocked one (the dedup state carries over). Keeps `Hiera` and its scoped
        # views, which hold a `Scope`, picklable and deep-copyable.
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
        strict: _ty.Union[Strict, str] = "warning",
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
        strict = _plain(strict)
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

        # Step 4: server_facts merge into params without overriding, as facts do.
        # $environment goes into the exposed $server_facts last, so it wins there, but
        # is not re-merged into params (steps 2 and 6 set it; re-merging would warn).
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
        strict: _ty.Optional[_ty.Union[Strict, str]] = None,
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
