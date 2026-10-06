# Ported from Puppet 8 lib/puppet/pops/lookup/explainer.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The nodes of Puppet's explain tree and their text and ``dict`` rendering.

Ports the ``ExplainTreeNode`` hierarchy of ``pops/lookup/explainer.rb``,
close enough to cite line numbers against throughout this module.
"""

from __future__ import annotations

import typing as _ty

from .._lookup.interpolation import _to_puppet_str
from .explain_refs import _LocationRef, _ProviderRef, _dump_value


class _Node:
    """``ExplainNode`` (``explainer.rb:11-50``): a lazily-branching node
    with its own queued texts, dumped depth-first."""

    def __init__(self) -> None:
        self.branches: "_ty.List[_Node]" = []
        self.texts: "_ty.Optional[_ty.List[str]]" = None
        self.parent: "_ty.Optional[_Node]" = None

    def to_hash(self) -> "_ty.Dict[str, _ty.Any]":
        """This node (and every branch, recursively) as the plain
        ``dict``/``list`` shape ``puppet lookup --explain`` renders as
        JSON."""
        hash_: "_ty.Dict[str, _ty.Any]" = {}
        if self.branches:
            hash_["branches"] = [b.to_hash() for b in self.branches]
        return hash_

    def explain(self) -> str:
        """This node's (and every branch's) text form, as
        :meth:`dump_on` renders it."""
        parts: _ty.List[str] = []
        self.dump_on(parts, "", "")
        return "".join(parts)

    def text(self, text: str) -> None:
        """Queue one free-text line on this node."""
        if self.texts is None:
            self.texts = []
        self.texts.append(text)

    def dump_on(self, parts: _ty.List[str], indent: str, first_indent: str) -> None:
        """Append this node's (and every branch's) text form to ``parts``,
        depth-first."""
        self._dump_texts(parts, indent)

    def _dump_texts(self, parts: list, indent: str) -> None:
        if self.texts:
            for t in self.texts:
                parts.append(indent)
                parts.append(t)
                parts.append("\n")


class _TreeNode(_Node):
    """``ExplainTreeNode`` (``explainer.rb:52-161``): the outcome-bearing
    base every real branch is built on (``key``/``event``/``value``, plus
    ``dump_outcome``/``dump_value``)."""

    def __init__(self, parent: _ty.Optional[_Node]) -> None:
        super().__init__()
        self.parent = parent
        self.key = None  # type: _ty.Optional[str]
        self.event = None  # type: _ty.Optional[str]
        self.value = None

    def found_in_overrides(self, key, value) -> None:
        self.key = _to_puppet_str(key)
        self.value = value
        self.event = "found_in_overrides"

    def found_in_defaults(self, key, value) -> None:
        self.key = _to_puppet_str(key)
        self.value = value
        self.event = "found_in_defaults"

    def found(self, key, value) -> None:
        self.key = _to_puppet_str(key)
        self.value = value
        self.event = "found"

    def result(self, value) -> None:
        self.value = value
        self.event = "result"

    def not_found(self, key) -> None:
        self.key = _to_puppet_str(key)
        self.event = "not_found"

    def location_not_found(self) -> None:
        self.event = "location_not_found"

    @staticmethod
    def increase_indent(indent: str) -> str:
        return indent + "  "

    @property
    def type(self) -> str:
        return "root"

    def to_hash(self) -> dict:
        hash_ = super().to_hash()
        if self.key is not None:
            hash_["key"] = self.key
        if self.event in ("found", "found_in_defaults", "found_in_overrides", "result"):
            hash_["value"] = self.value
        if self.event is not None:
            hash_["event"] = self.event
        if self.texts is not None:
            hash_["texts"] = list(self.texts)
        hash_["type"] = self.type
        return hash_

    def dump_outcome(self, parts: list, indent: str) -> None:
        if self.event == "not_found":
            parts.append(indent)
            parts.append('No such key: "')
            parts.append(self.key)
            parts.append('"\n')
        elif self.event in ("found", "found_in_overrides", "found_in_defaults"):
            parts.append(indent)
            parts.append('Found key: "')
            parts.append(self.key)
            parts.append('" value: ')
            _dump_value(parts, indent, self.value)
            if self.event == "found_in_overrides":
                parts.append(" in overrides")
            elif self.event == "found_in_defaults":
                parts.append(" in defaults")
            parts.append("\n")
        self._dump_texts(parts, indent)


class _Top(_TreeNode):
    """``ExplainTop`` (``explainer.rb:163-175``): ``Searching for "<key>"``.
    ``kind`` (Puppet's ``:meta``/``:data``) is stored but never read back --
    the same dead field the Ruby class carries (its own ``type`` stays the
    inherited ``"root"`` regardless of it)."""

    def __init__(self, parent: _Node, kind: str, key) -> None:
        super().__init__(parent)
        self.kind = kind
        self.key = _to_puppet_str(key)

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(first_indent)
        parts.append('Searching for "')
        parts.append(self.key)
        parts.append('"\n')
        indent = self.increase_indent(indent)
        for b in self.branches:
            b.dump_on(parts, indent, indent)


class _InvalidKey(_TreeNode):
    """``ExplainInvalidKey`` (``explainer.rb:177-190``): ``Invalid key
    "<key>"`` -- the reserved-key rejection, a terminal node."""

    def __init__(self, parent: _Node, key) -> None:
        super().__init__(parent)
        self.key = _to_puppet_str(key)

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(first_indent)
        parts.append('Invalid key "')
        parts.append(self.key)
        parts.append('"\n')

    @property
    def type(self) -> str:
        return "invalid_key"


class _MergeSource(_Node):
    """``ExplainMergeSource`` (``explainer.rb:192-210``): not a tree node --
    appended straight into the current node's branches, never made current
    itself. ``to_hash`` is exactly ``{type, merge_source}``, no ``branches``
    key at all."""

    def __init__(self, merge_source) -> None:
        super().__init__()
        self.merge_source = merge_source

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(first_indent)
        parts.append('Using merge options from "')
        parts.append(self.merge_source)
        parts.append('" hash\n')

    def to_hash(self) -> dict:
        return {"type": "merge_source", "merge_source": self.merge_source}


class _Module(_TreeNode):
    """``ExplainModule`` (``explainer.rb:212-238``): a module absent from
    the modulepath, or present but without its own ``hiera.yaml``. Its
    ``to_hash`` is the plain inherited one (no module-name field) since the
    Ruby class defines none of its own."""

    def __init__(self, parent: _Node, module_name: str) -> None:
        super().__init__(parent)
        self.module_name = module_name

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        if self.event == "module_not_found":
            parts.append(indent)
            parts.append('Module "')
            parts.append(self.module_name)
            parts.append('" not found\n')
        elif self.event == "module_provider_not_found":
            parts.append(indent)
            parts.append('Module data provider for module "')
            parts.append(self.module_name)
            parts.append('" not found\n')

    def module_not_found(self) -> None:
        self.event = "module_not_found"

    def module_provider_not_found(self) -> None:
        self.event = "module_provider_not_found"

    @property
    def type(self) -> str:
        return "module"


class _Interpolate(_TreeNode):
    """``ExplainInterpolate`` (``explainer.rb:240-261``): ``Interpolation
    on "<expr>"``."""

    def __init__(self, parent: _Node, expression: str) -> None:
        super().__init__(parent)
        self.expression = expression

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(first_indent)
        parts.append('Interpolation on "')
        parts.append(self.expression)
        parts.append('"\n')
        indent = self.increase_indent(indent)
        for b in self.branches:
            b.dump_on(parts, indent, indent)

    def to_hash(self) -> dict:
        hash_ = super().to_hash()
        hash_["expression"] = self.expression
        return hash_

    @property
    def type(self) -> str:
        return "interpolate"


class _Merge(_TreeNode):
    """``ExplainMerge`` (``explainer.rb:263-313``): transparent with zero or
    one branch (no node printed / the branch's own dump verbatim); a real
    ``Merge strategy <KEY>`` header, its non-``strategy`` options and a
    ``Merged result:`` line only with two or more.

    ``merge`` duck-types Puppet's ``MergeStrategy``: a ``KEY`` class
    attribute and an ``options`` dict attribute (never ``None``).
    """

    def __init__(self, parent: _Node, merge) -> None:
        super().__init__(parent)
        self.merge = merge

    def _options_wo_strategy(self):
        options = self.merge.options
        if options and "strategy" in options:
            options = dict(options)
            del options["strategy"]
        return options or None

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        if not self.branches:
            return
        if len(self.branches) == 1:
            self.branches[0].dump_on(parts, indent, first_indent)
            return
        parts.append(first_indent)
        parts.append("Merge strategy ")
        parts.append(self.merge.KEY)
        parts.append("\n")
        indent = self.increase_indent(indent)
        options = self._options_wo_strategy()
        if options is not None:
            parts.append(indent)
            parts.append("Options: ")
            _dump_value(parts, indent, options)
            parts.append("\n")
        for b in self.branches:
            b.dump_on(parts, indent, indent)
        if self.event == "result":
            parts.append(indent)
            parts.append("Merged result: ")
            _dump_value(parts, indent, self.value)
            parts.append("\n")

    def to_hash(self) -> dict:
        if len(self.branches) == 1:
            return self.branches[0].to_hash()
        hash_ = super().to_hash()
        hash_["merge"] = self.merge.KEY
        options = self._options_wo_strategy()
        if options is not None:
            hash_["options"] = options
        return hash_

    @property
    def type(self) -> str:
        return "merge"


class _DataProvider(_TreeNode):
    """``ExplainDataProvider`` (``explainer.rb:339-370``): one provider in
    the layer stack (global/environment/module) or one hierarchy entry --
    ``provider`` is a :class:`_ProviderRef`."""

    def __init__(self, parent: _Node, provider: _ProviderRef) -> None:
        super().__init__(parent)
        self.provider = provider

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(first_indent)
        parts.append(self.provider.name)
        parts.append("\n")
        indent = self.increase_indent(indent)
        path = self.provider.config_path
        if path is not None:
            parts.append(indent)
            parts.append('Using configuration "')
            parts.append(path)
            parts.append('"\n')
        for b in self.branches:
            b.dump_on(parts, indent, indent)
        self.dump_outcome(parts, indent)

    def to_hash(self) -> dict:
        hash_ = super().to_hash()
        hash_["name"] = self.provider.name
        if self.provider.config_path is not None:
            hash_["configuration_path"] = self.provider.config_path
        if self.provider.module_name is not None:
            hash_["module"] = self.provider.module_name
        return hash_

    @property
    def type(self) -> str:
        return "data_provider"


class _Location(_TreeNode):
    """``ExplainLocation`` (``explainer.rb:372-405``): one hierarchy
    candidate -- ``location`` is a :class:`_LocationRef`. ``type`` (and so
    whether it renders/serializes as ``Path``/``path`` or ``URI``/``uri``)
    comes straight from the ref's own ``kind``."""

    def __init__(self, parent: _Node, location: _LocationRef) -> None:
        super().__init__(parent)
        self.location = location

    @property
    def type(self) -> str:
        return self.location.kind

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        type_name = "Path" if self.type == "path" else "URI"
        parts.append(indent)
        parts.append(type_name)
        parts.append(' "')
        parts.append(self.location.location)
        parts.append('"\n')
        indent = self.increase_indent(indent)
        parts.append(indent)
        parts.append("Original ")
        parts.append(type_name.lower())
        parts.append(': "')
        parts.append(self.location.original_location)
        parts.append('"\n')
        for b in self.branches:
            b.dump_on(parts, indent, indent)
        if self.event == "location_not_found":
            parts.append(indent)
            parts.append(type_name)
            parts.append(" not found\n")
        self.dump_outcome(parts, indent)

    def to_hash(self) -> dict:
        hash_ = super().to_hash()
        if self.type == "path":
            hash_["original_path"] = self.location.original_location
            hash_["path"] = self.location.location
        else:
            hash_["original_uri"] = self.location.original_location
            hash_["uri"] = self.location.location
        return hash_


class _SubLookup(_TreeNode):
    """``ExplainSubLookup`` (``explainer.rb:407-423``): ``Sub key:
    "<segments joined by .>"``. ``segments`` is the list of dotted-key
    segments (``str``/``int``)."""

    def __init__(self, parent: _Node, segments: list) -> None:
        super().__init__(parent)
        self.segments = segments

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(indent)
        parts.append('Sub key: "')
        parts.append(".".join(_to_puppet_str(s) for s in self.segments))
        parts.append('"\n')
        indent = self.increase_indent(indent)
        for b in self.branches:
            b.dump_on(parts, indent, indent)
        self.dump_outcome(parts, indent)

    @property
    def type(self) -> str:
        return "sub_key"


class _Segment(_TreeNode):
    """``ExplainKeySegment`` (``explainer.rb:425-438``): one sub-key step --
    outcome only, no header line of its own."""

    def __init__(self, parent: _Node, segment) -> None:
        super().__init__(parent)
        self.segment = segment

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        self.dump_outcome(parts, indent)

    @property
    def type(self) -> str:
        return "segment"


class _Scope(_TreeNode):
    """``ExplainScope`` (``explainer.rb:440-462``): a named scope lookup
    (``"Global Scope"``, ``"Searching default_hierarchy of module \"m\""``,
    the ``lookup_options`` gathering sections, ...)."""

    def __init__(self, parent: _Node, name: str) -> None:
        super().__init__(parent)
        self.name = name

    def dump_on(self, parts: list, indent: str, first_indent: str) -> None:
        parts.append(indent)
        parts.append(self.name)
        parts.append("\n")
        indent = self.increase_indent(indent)
        for b in self.branches:
            b.dump_on(parts, indent, indent)
        self.dump_outcome(parts, indent)

    def to_hash(self) -> dict:
        hash_ = super().to_hash()
        hash_["name"] = self.name
        return hash_

    @property
    def type(self) -> str:
        return "scope"


#: ``Explainer#push``'s dispatch table (``explainer.rb:487-506``). ``:global`` is absent: this
#: library's global layer is always Hiera, so ``ExplainGlobal`` (a non-Hiera ``data_binding_terminus``
#: report) is not ported and pushing ``"global"`` raises, like any unknown kind.
_PUSH = {
    "meta": lambda current, qualifier: _Top(current, "meta", qualifier),
    "data": lambda current, qualifier: _Top(current, "data", qualifier),
    "location": lambda current, qualifier: _Location(current, qualifier),
    "interpolate": lambda current, qualifier: _Interpolate(current, qualifier),
    "data_provider": lambda current, qualifier: _DataProvider(current, qualifier),
    "merge": lambda current, qualifier: _Merge(current, qualifier),
    "module": lambda current, qualifier: _Module(current, qualifier),
    "scope": lambda current, qualifier: _Scope(current, qualifier),
    "sub_lookup": lambda current, qualifier: _SubLookup(current, qualifier),
    "segment": lambda current, qualifier: _Segment(current, qualifier),
    "invalid_key": lambda current, qualifier: _InvalidKey(current, qualifier),
}
