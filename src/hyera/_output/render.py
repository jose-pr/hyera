"""CLI output rendering: ``puppet lookup --render-as s|json|yaml``.

Three ``render``-only :class:`~hyera.backends.Backend` subclasses. Each
implements only ``dumps(obj) -> str``; nothing else in the ``Backend``
contract (``loads``, the Hiera 5 provider hooks) applies to a render
backend. Registered purely for their side effect of subclassing ``Backend``
-- import this module once (``hyera/__init__.py`` does, right after
``backends``) to make ``s``/``json``/``yaml`` available via
``Backend.new(fmt, kind="render")``.
"""

from __future__ import annotations

import json as _json
import re as _re
import uuid as _uuid

import yaml as _yaml

from ..backends import Backend
from ..backends import _psych
from ..exceptions import BackendError
from .._digits import format_decimal_int as _format_int
from .._lookup.interpolation import _to_puppet_str
from .._types.types import Sensitive
from .._enums import _StrEnum

__all__ = ["RenderAs", "StringRender", "JSONRender", "YAMLRender"]


class RenderAs(_StrEnum):
    """A ``puppet lookup --render-as`` output format: the name registered
    for each render backend in :meth:`~hyera.backends.Backend.new`'s
    ``kind="render"`` namespace (``hyera.BackendKind.RENDER``).

    The ``hyera`` CLI's own ``--render-as`` flag stays a plain string
    (duho's ``Enum`` support resolves CLI text by member *name*, e.g.
    ``S``/``JSON``/``YAML``, not by value -- incompatible with Puppet's own
    lowercase flag values); this enum is for a Python caller of
    :meth:`~hyera.backends.Backend.new`/:meth:`~hyera.backends.Backend.find`
    directly, e.g. ``Backend.new(RenderAs.YAML, kind="render")``.
    """

    S = "s"
    """Ruby ``to_s`` rendering (:class:`StringRender`)."""

    JSON = "json"
    """Compact JSON (:class:`JSONRender`)."""

    YAML = "yaml"
    """YAML, Psych-compatible (:class:`YAMLRender`)."""


_YAML_FORCE_QUOTE_STRS = ("y", "Y", "n", "N")
_YAML_FORCE_QUOTE_RE = _re.compile(r"^:.")
#: Line breaks YAML 1.1 recognises beyond LF and CR: a plain or literal
#: scalar holding one does not read back as the same text.
_YAML_EXTRA_BREAKS_RE = _re.compile("[\x85\u2028\u2029]")


class StringRender(Backend):
    """``s``: Ruby ``to_s`` -- the same renderer the interpolation engine
    uses for a bare ``%{var}``/function-call result (Ruby 3.2 AIO hash
    form, ``Sensitive [value redacted]``)."""

    NAMES = {"render": ("s",)}

    def dumps(self, obj, **kw):
        return _to_puppet_str(obj)


def _json_data(obj):
    """Project a Python value the way Puppet's own JSON renderer would see
    it: a :class:`~hyera.Sensitive` becomes its redacted text, a ``dict``
    keeps insertion order (non-``str`` keys rendered like Ruby ``to_s``), a
    ``list``/``tuple`` becomes a plain ``list``. A non-finite ``float``
    raises :class:`ValueError` with Puppet's own text (Ruby's ``json`` gem
    rejects ``NaN``/``Infinity``/``-Infinity`` outright); anything else not
    representable as Puppet data raises :class:`TypeError`.
    """
    if isinstance(obj, Sensitive):
        return "Sensitive [value redacted]"
    if isinstance(obj, dict):
        return {
            (k if isinstance(k, str) else _to_puppet_str(k)): _json_data(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [_json_data(v) for v in obj]
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return _BigInt(obj) if obj.bit_length() > _BIG_BITS else obj
    if isinstance(obj, float):
        if obj != obj:
            raise ValueError("NaN not allowed in JSON")
        if obj == float("inf"):
            raise ValueError("Infinity not allowed in JSON")
        if obj == float("-inf"):
            raise ValueError("-Infinity not allowed in JSON")
        return obj
    raise TypeError("{} is not a Puppet data value".format(type(obj).__name__))


#: Integers past this bit length (about 4000 digits) are written by
#: :func:`_format_int`: ``json`` and ``str`` refuse them on Python 3.11+.
_BIG_BITS = 13300


class _BigInt(int):
    """An integer too long for ``json`` to write; it dumps as a quoted
    placeholder that :meth:`JSONRender.dumps` replaces with its digits."""


class JSONRender(Backend):
    """``json``: compact, UTF-8, insertion-ordered, no NaN/Infinity."""

    NAMES = {"render": ("json",)}

    def dumps(self, obj, **kw):
        mark = "bigint-{}-".format(_uuid.uuid4().hex)
        big: list = []
        data = _placeholders(_json_data(obj), big, mark)
        text = _json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        for index, value in enumerate(big):
            text = text.replace('"{}{}"'.format(mark, index), _format_int(value))
        return text


def _placeholders(data, big: list, mark: str):
    """``data`` with every :class:`_BigInt` replaced by a placeholder
    string recorded in ``big``."""
    if isinstance(data, _BigInt):
        big.append(int(data))
        return "{}{}".format(mark, len(big) - 1)
    if isinstance(data, dict):
        return {k: _placeholders(v, big, mark) for k, v in data.items()}
    if isinstance(data, list):
        return [_placeholders(v, big, mark) for v in data]
    return data


class _PuppetYAMLDumper(_yaml.SafeDumper):
    """A ``SafeDumper`` whose output reads back as the value dumped, under
    PyYAML and Psych alike, with Psych's quoting for the common shapes."""

    def ignore_aliases(self, data):
        return True

    def check_simple_key(self):
        # An empty string key prints as ``'': v``, not ``? ''``.
        event = self.event
        if (
            isinstance(event, _yaml.ScalarEvent)
            and event.value == ""
            and event.style == "'"
        ):
            return True
        return super().check_simple_key()


def _reads_back(text: str) -> bool:
    """Whether a plain scalar ``text`` loads as the string ``text`` under
    Psych's scalar scanner (PyYAML's own resolver is applied by the
    emitter)."""
    try:
        return _psych._tokenize(text) == text
    except BackendError:
        return False


def _represent_none(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:null", "", style="")


def _represent_str(dumper, data):
    if _YAML_EXTRA_BREAKS_RE.search(data):
        style = '"'
    elif "\n" in data:
        style = "|"
    elif data in _YAML_FORCE_QUOTE_STRS or _YAML_FORCE_QUOTE_RE.match(data):
        style = '"'
    elif not data or not _reads_back(data):
        style = "'"
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _represent_sensitive(dumper, data):
    return _represent_str(dumper, "Sensitive [value redacted]")


def _represent_int(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:int", _format_int(data))


def _represent_tuple(dumper, data):
    return dumper.represent_list(list(data))


_PuppetYAMLDumper.add_representer(type(None), _represent_none)
_PuppetYAMLDumper.add_representer(str, _represent_str)
_PuppetYAMLDumper.add_representer(Sensitive, _represent_sensitive)
_PuppetYAMLDumper.add_representer(tuple, _represent_tuple)
_PuppetYAMLDumper.add_representer(int, _represent_int)


class YAMLRender(Backend):
    """``yaml``: text that reads back as the value rendered, in Psych's
    quoting style for the common shapes."""

    NAMES = {"render": ("yaml",)}

    def dumps(self, obj, **kw):
        text = _yaml.dump(
            obj,
            Dumper=_PuppetYAMLDumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            explicit_start=True,
        )
        lines = text.splitlines(keepends=True)
        if lines and lines[-1].rstrip("\n") == "...":
            lines.pop()
            text = "".join(lines)
        return text
