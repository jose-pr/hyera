# Ported from Puppet 8 lib/puppet/pops/lookup/explainer.rb,
# configured_data_provider.rb (https://github.com/puppetlabs/puppet), Apache-2.0.
# Modified by jose-pr. See NOTICE.
"""Puppet's explain tree (``pops/lookup/explainer.rb``) and its text
rendering: the private ``Explainer``/node classes that record and dump a
lookup's own trace, exactly as ``puppet lookup --explain``/``--render-as
s|json`` project it, plus the ``DebugExplainer`` a lookup wraps it in to
also log the same report at ``DEBUG``.

Ports ``explainer.rb``'s class hierarchy, close enough to cite line
numbers against throughout this module, with ``configured_data_provider.rb``
and ``pops/lookup.rb`` for the provider and lookup-name qualifiers.
Recording hooks live on ``_invocation.Invocation``; the node classes are in
:mod:`hyera._output.explain_nodes`, and this module holds the ``Explainer``
that builds the tree (and the debug wrapper), so it has no engine imports at
all beyond the key-joining helper.
"""

from __future__ import annotations

import copy
import logging
import typing as _ty

from .._lookup.navigation import join_key
from ..exceptions import HieraError
from .explain_nodes import _PUSH, _MergeSource, _Node

__all__ = ["ExplainResult"]  # everything else here is private.

_LOGGER = logging.getLogger(__name__)


class Explainer(_Node):
    """``Explainer`` (``explainer.rb:464-567``): the recording sink a
    lookup's ``Invocation`` pushes/pops nodes on and reports outcomes to;
    also the tree root ``to_hash``/``explain`` (text) render from.
    """

    def __init__(
        self, explain_options: bool = False, only_explain_options: bool = False
    ) -> None:
        super().__init__()
        self.current: _Node = self
        self.explain_options = explain_options
        self.only_explain_options = only_explain_options

    def push(self, kind: str, qualifier: _ty.Any) -> None:
        """Push a new node of ``kind`` (``"location"``, ``"module"``, ...)
        as a branch of the current node, and make it current."""
        build = _PUSH.get(kind)
        if build is None:
            raise ValueError("Unknown Explain type {}".format(kind))
        node = build(self.current, qualifier)
        self.current.branches.append(node)
        self.current = node

    def pop(self) -> None:
        """Make the current node's parent current again, undoing the last
        :meth:`push` (a no-op at the root)."""
        if self.current.parent is not None:
            self.current = self.current.parent

    def accept_found_in_overrides(self, key: _ty.Any, value: _ty.Any) -> None:
        """Record a value found through ``override_values``."""
        self.current.found_in_overrides(key, value)

    def accept_found_in_defaults(self, key: _ty.Any, value: _ty.Any) -> None:
        """Record a value found through ``default_values_hash``."""
        self.current.found_in_defaults(key, value)

    def accept_found(self, key: _ty.Any, value: _ty.Any) -> None:
        """Record a value found in ordinary hierarchy data."""
        self.current.found(key, value)

    def accept_merge_source(self, merge_source: _ty.Any) -> None:
        """Record which ``lookup_options`` source a merge strategy came
        from."""
        self.current.branches.append(_MergeSource(merge_source))

    def accept_not_found(self, key: _ty.Any) -> None:
        """Record a miss for the current node."""
        self.current.not_found(key)

    def accept_location_not_found(self) -> None:
        """Record that the current hierarchy location does not exist."""
        self.current.location_not_found()

    def accept_module_not_found(self, module_name: str) -> None:
        """Record that module ``module_name`` does not exist."""
        self.push("module", module_name)
        self.current.module_not_found()
        self.pop()

    def accept_module_provider_not_found(self, module_name: str) -> None:
        """Record that module ``module_name`` has no ``hiera.yaml``
        provider."""
        self.push("module", module_name)
        self.current.module_provider_not_found()
        self.pop()

    def accept_result(self, value: _ty.Any) -> None:
        """Record the top-level lookup's final result."""
        self.current.result(value)

    def accept_text(self, text: str) -> None:
        """Record one free-text line (``LookupContext.explain``) on the
        current node."""
        self.current.text(text)

    def dump_on(self, parts: _ty.List[str], indent: str, first_indent: str) -> None:
        """Append every branch's (and this node's own) text form to
        ``parts``, depth-first."""
        for b in self.branches:
            b.dump_on(parts, indent, first_indent)
        self._dump_texts(parts, indent)

    def to_hash(self) -> "_ty.Dict[str, _ty.Any]":
        """The tree's own ``dict``/``list`` shape: unwrapped one level when
        there is exactly one top-level branch, matching
        ``puppet lookup --explain``'s own JSON."""
        if len(self.branches) == 1:
            return self.branches[0].to_hash()
        return super().to_hash()


class ExplainResult:
    """``Hiera.explain(...)``'s return value: the report
    ``puppet lookup --explain``/``--explain-options`` builds, projected the
    same two ways (``--render-as json`` -> :meth:`to_hash`, ``--render-as
    s`` -> :meth:`text`).

    :param explainer: the recorded explain tree.
    :param error: the :class:`~hyera.HieraError` the lookup ended with, or
        ``None`` when a value was found or defaulted to.
    """

    def __init__(
        self, explainer: Explainer, error: _ty.Optional[HieraError] = None
    ) -> None:
        self._explainer = explainer
        self._error = error

    def to_hash(self) -> "_ty.Dict[str, _ty.Any]":
        """A deep copy of the explain tree, projected through ``to_hash()``
        -- mutating the returned structure, or a later lookup/explain on the
        same instance, never changes what an earlier result holds.

        :returns: the report as plain ``dict``/``list`` data, Puppet's
            own keys.
        """
        return copy.deepcopy(self._explainer.to_hash())

    def text(self) -> str:
        """The indented report ``puppet lookup --explain``'s ``s`` render
        shows: every line ends in ``"\\n"``.

        :returns: the report text.
        """
        return self._explainer.explain()

    def __str__(self) -> str:
        return self.text()

    @property
    def error(self) -> _ty.Optional[HieraError]:
        """The :class:`~hyera.HieraError` this lookup ended with, reported
        as the report's own last line -- ``None`` when a value was found or
        defaulted to."""
        return self._error


class _DebugExplainer(Explainer):
    """Puppet's ``DebugExplainer`` (``explainer.rb:569-595``): every
    recording hook a lookup makes still lands here -- ``push``/``pop``/
    every ``accept_*`` are inherited from :class:`Explainer` unchanged, so
    this is a transparent proxy over the same node graph, whether or not
    ``wrapped`` (a real ``Explainer``, when an actual ``explain()`` call is
    also in progress) is given.

    ``wrapped is None`` (an ordinary ``.lookup()`` with the ``hyera._output.explain``
    logger at ``DEBUG``): this instance is its own root, exactly like a
    bare ``Explainer()`` -- built and thrown away once the trace is logged.
    ``wrapped`` given (``Hiera.explain()`` also has ``DEBUG`` on): every
    push/pop still runs through this wrapper's own ``current``, but the
    actual nodes it builds are appended onto ``wrapped``'s own tree
    (``push`` starts from ``self.current == wrapped``), so ``wrapped``
    itself ends up exactly as it would from an unwrapped explain -- the
    caller keeps a reference to `wrapped`, never to this proxy.
    """

    def __init__(self, wrapped: "_ty.Optional[Explainer]") -> None:
        explain_options = wrapped.explain_options if wrapped is not None else False
        only_explain_options = (
            wrapped.only_explain_options if wrapped is not None else False
        )
        super().__init__(explain_options, only_explain_options)
        self.wrapped = wrapped
        self.current = wrapped if wrapped is not None else self

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        """Dump the node that is current right now -- the whole tree at the
        top level, or (for a nested emission, mid-interpolation) just the
        subtree of whichever node the lookup is inside of when this fires."""
        if self.current is self:
            super().dump_on(parts, indent, first_indent)
        else:
            self.current.dump_on(parts, indent, first_indent)

    def emit_debug_info(self, preamble: str) -> None:
        """``pops/lookup.rb:62,66``'s ``emit_debug_info`` +
        ``explainer.rb:588-594``: one DEBUG record, the preamble line
        followed by the ordinary text report, indented two spaces
        (``dump_on(parts, "  ", "  ")``, matching ``Puppet.debug``'s own
        ``chomp!`` of exactly one trailing newline)."""
        parts = [preamble, "\n"]
        self.dump_on(parts, "  ", "  ")
        message = "".join(parts)
        # `message` always ends in "\n" here (`parts` starts with one after `preamble`,
        # and `dump_on`/`_dump_texts` append nothing or text ending in "\n"). The guard
        # mirrors Ruby's `chomp!`, a no-op without one, not that invariant.
        if message.endswith("\n"):
            message = message[:-1]
        _LOGGER.debug("%s", message)


def _debug_preamble(names) -> str:
    """Puppet's ``debug_preamble`` (``pops/lookup.rb:71-78``): ``Lookup of
    'a'`` for one name, ``Lookup of 'a', 'b'`` for several. A tuple key
    path renders as :func:`~hyera._lookup.navigation.join_key`'s dotted
    text, same as everywhere else a name reaches text."""
    return "Lookup of " + ", ".join(
        "'{}'".format(join_key(n) if isinstance(n, tuple) else n) for n in names
    )
