"""JSON (``json_data``) backend: matches Ruby's ``json`` gem (comments,
NaN/Infinity and lone-surrogate rejection Python's own decoder accepts).
"""

import json
import typing as _ty

from .._digits import parse_decimal_int
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


def loads_json(text: str):
    """``json.loads`` with ``NaN``/``Infinity`` rejected and integers of any
    length accepted (Python 3.11+ caps ``int(str)`` at 4300 digits; Ruby's
    ``json`` gem has no cap).

    :param text: the JSON text.
    :returns: the parsed value.
    :raises ValueError: ``text`` is not valid JSON (``json.JSONDecodeError``
        for syntax errors).
    """
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError:
        raise
    except ValueError:
        # Retried only after a failure: the hook costs a Python call per
        # integer.
        return json.loads(
            text, parse_constant=_reject_json_constant, parse_int=parse_decimal_int
        )


def _has_lone_surrogate(text: str) -> bool:
    return any("\ud800" <= ch <= "\udfff" for ch in text)


#: Deepest array/object nesting a loaded JSON value may have.
MAX_JSON_NESTING = 500
TOO_DEEP = "nested too deeply (more than {} levels)".format(MAX_JSON_NESTING)


def check_json_value(obj, surrogates: bool = True) -> None:
    """Walk a parsed JSON value without recursion and raise
    :class:`ValueError` when it nests deeper than :data:`MAX_JSON_NESTING`
    or, with ``surrogates``, any string (key or value) holds a lone
    (unpaired) surrogate code point -- Ruby's json gem rejects ``"\\ud800"``
    ("incomplete surrogate pair"); Python's decoder accepts it.

    :param obj: the value ``json.loads`` returned.
    :param surrogates: also reject lone surrogates.
    :raises ValueError: the value is too deep or holds a lone surrogate.
    """
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            if surrogates and _has_lone_surrogate(value):
                raise ValueError("incomplete surrogate pair")
        elif isinstance(value, (dict, list)):
            if depth > MAX_JSON_NESTING:
                raise ValueError(TOO_DEEP)
            if isinstance(value, dict):
                for key, item in value.items():
                    if surrogates and isinstance(key, str) and _has_lone_surrogate(key):
                        raise ValueError("incomplete surrogate pair")
                    stack.append((item, depth + 1))
            else:
                stack.extend((item, depth + 1) for item in value)


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
            result = loads_json(_strip_json_comments(text))
            check_json_value(result)
            return result
        except json.JSONDecodeError as e:
            problem = "{} at line {} column {}".format(e.msg, e.lineno, e.colno)
        except RecursionError:
            problem = TOO_DEEP
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
