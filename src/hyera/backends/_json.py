"""JSON (``json_data``) backend: matches Ruby's ``json`` gem (comments,
NaN/Infinity and lone-surrogate rejection Python's own decoder accepts).
"""

import json
import typing as _ty

from ..exceptions import BackendError
from . import Backend, _Names

__all__ = ["JSONBackend"]


def _strip_json_comments(text: str) -> str:
    """Blank ``/* ... */`` and ``// ...`` outside string literals, the way
    Ruby's ``json`` gem (MultiJson's ``JsonGem`` adapter) accepts them but
    Python's ``json`` module does not. Replaces every non-newline character
    of a comment with a space, so a later ``JSONDecodeError``'s line/column
    still line up with the original text. Tracks ``"``/``\\`` escapes so a
    comment-*looking* substring inside a string is left alone. An
    unterminated ``/*`` is left in place (its own unbalanced ``/*`` then
    fails in ``json.loads`` exactly as it would without stripping).
    """
    n = len(text)
    out = list(text)
    i = 0
    in_string = False
    escaped = False
    while i < n:
        c = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
            i += 1
            continue
        if c == '"':
            in_string = True
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = (end + 2) if end != -1 else n
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def _reject_json_constant(name: str):
    # Ruby's json gem rejects `NaN`/`Infinity`/`-Infinity` outright, unlike
    # Python's own `json.loads`, which accepts them by default.
    raise ValueError("unexpected token '{}'".format(name))


def _has_lone_surrogate(text: str) -> bool:
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def _reject_lone_surrogates(obj) -> None:
    """Walk a parsed JSON value and raise if any string (key or value)
    holds a lone (unpaired) surrogate code point -- Ruby's json gem
    rejects ``"\\ud800"`` ("incomplete surrogate pair"); Python's decoder
    accepts it, keeping the bare surrogate in the resulting ``str``.
    """
    if isinstance(obj, str):
        if _has_lone_surrogate(obj):
            raise ValueError("incomplete surrogate pair")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and _has_lone_surrogate(key):
                raise ValueError("incomplete surrogate pair")
            _reject_lone_surrogates(value)
    elif isinstance(obj, list):
        for item in obj:
            _reject_lone_surrogates(item)


class JSONBackend(Backend):
    """JSON (``.json``) data, matching Ruby's ``json`` gem: ``/* */``/
    ``// ...`` comments allowed, ``NaN``/``Infinity``/``-Infinity`` and a
    lone (unpaired) surrogate code point rejected."""

    NAMES: _ty.ClassVar[_Names] = {"function": ("json_data",), "format": ("json",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".json",)

    def loads(self, text: str) -> _ty.Any:
        """Parse JSON the way Ruby's ``json`` gem does: ``/* */``/``//``
        comments allowed, ``NaN``/``Infinity``/``-Infinity`` and a lone
        surrogate rejected.

        :param text: the JSON text to parse.
        :returns: the parsed value.
        :raises BackendError: ``text`` is not valid JSON by that rule.
        """
        problem = None
        try:
            result = json.loads(
                _strip_json_comments(text), parse_constant=_reject_json_constant
            )
            _reject_lone_surrogates(result)
            return result
        except json.JSONDecodeError as e:
            problem = "{} at line {} column {}".format(e.msg, e.lineno, e.colno)
        except ValueError as e:
            problem = str(e)
        # Outside the except block, matching YAMLBackend's chain-free style.
        raise BackendError(problem)

    def dumps(self, obj: _ty.Any, **kw: _ty.Any) -> str:
        """Render ``obj`` as JSON, with non-ASCII characters left as-is.

        :param obj: the value to render.
        :param kw: forwarded to :func:`json.dumps`.
        :returns: the rendered JSON text.
        """
        kw.setdefault("ensure_ascii", False)
        return json.dumps(obj, **kw)
