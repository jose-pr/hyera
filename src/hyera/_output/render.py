"""CLI output rendering: ``puppet lookup --render-as s|json|yaml``.

Three ``render``-only :class:`~hyera.backends.Backend` subclasses. Each
implements only ``dumps(obj) -> str``; nothing else in the ``Backend``
contract (``loads``, the Hiera 5 provider hooks) applies to a render
backend. Registered purely for their side effect of subclassing ``Backend``
-- import this module once (``hyera/__init__.py`` does, right after
``backends``) to make ``s``/``json``/``yaml`` available via
``Backend.new(fmt, kind="render")``.
"""

import json as _json
import re as _re

import yaml as _yaml

from ..backends import Backend
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
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if obj != obj:
            raise ValueError("NaN not allowed in JSON")
        if obj == float("inf"):
            raise ValueError("Infinity not allowed in JSON")
        if obj == float("-inf"):
            raise ValueError("-Infinity not allowed in JSON")
        return obj
    raise TypeError("{} is not a Puppet data value".format(type(obj).__name__))


class JSONRender(Backend):
    """``json``: compact, UTF-8, insertion-ordered, no NaN/Infinity."""

    NAMES = {"render": ("json",)}

    def dumps(self, obj, **kw):
        data = _json_data(obj)
        return _json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )


class _PuppetYAMLDumper(_yaml.SafeDumper):
    """A ``SafeDumper`` whose scalar styles are tuned to match Psych's
    output for the shapes ``puppet lookup --render-as yaml`` actually
    prints, measured against real Puppet 8."""


def _represent_none(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:null", "", style="")


def _represent_str(dumper, data):
    if "\n" in data:
        style = "|"
    elif data in _YAML_FORCE_QUOTE_STRS or _YAML_FORCE_QUOTE_RE.match(data):
        style = '"'
    else:
        style = None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


def _represent_sensitive(dumper, data):
    return _represent_str(dumper, "Sensitive [value redacted]")


def _represent_tuple(dumper, data):
    return dumper.represent_list(list(data))


_PuppetYAMLDumper.add_representer(type(None), _represent_none)
_PuppetYAMLDumper.add_representer(str, _represent_str)
_PuppetYAMLDumper.add_representer(Sensitive, _represent_sensitive)
_PuppetYAMLDumper.add_representer(tuple, _represent_tuple)


class YAMLRender(Backend):
    """``yaml``: Psych ``to_yaml`` byte parity for the measured shapes."""

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
