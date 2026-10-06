"""Running one lookup: building the Hiera, resolving it, rendering the result."""

import os as _os
import typing as _ty

from ..backends import Backend
from ..core import Hiera
from ..exceptions import HieraError, _one_line
from .._types.parser import parse_type
from ._scope import _paths


def _describe(e) -> str:
    """One-line description of an exception for the CLI's failure log."""
    if isinstance(e, (HieraError, OSError)):
        return _one_line(e)
    return "{}: {}".format(type(e).__name__, _one_line(e))


def _resolve(
    opts: dict, scope, keys: list, merge_options, explaining: bool, only_options: bool
):
    """Build the Hiera for ``opts`` and run the lookup or the explain.

    Returns the value found, or the ``ExplainResult`` when explaining.
    Raises ``KeyNotFoundError`` for a miss and whatever the lookup raises.
    """
    config = opts["hiera_config"]
    extra = {}
    if config is not None:
        config = _os.path.abspath(config)
    elif _os.path.exists(_os.path.abspath("hiera.yaml")):
        config = _os.path.abspath("hiera.yaml")
    else:
        extra["base_path"] = _os.getcwd()
    kwargs = {}
    if opts["default"] is not None:
        kwargs["default_value"] = opts["default"]
    value_type = opts["value_type"]
    if value_type is not None:
        parse_type(value_type)  # a syntax error exits 2, even on a miss
    hiera = Hiera(
        config,
        scope=scope,
        environmentpath=_paths(opts["environmentpath"]),
        basemodulepath=_paths(opts["basemodulepath"]) or (),
        modulepath=_paths(opts["modulepath"]),
        codedir=opts["codedir"],
        **extra,
    )
    names = keys[0] if len(keys) == 1 else keys
    if explaining:
        return hiera.explain(
            names, value_type, merge_options, explain_options=only_options, **kwargs
        )
    return hiera.lookup(names, value_type, merge_options, **kwargs)


def _render(fmt: str, outcome: _ty.Any, explaining: bool) -> str:
    """The text to print for ``outcome`` in render format ``fmt``."""
    if not explaining:
        return Backend.new(fmt, kind="render").dumps(outcome)
    if fmt == "s":
        return outcome.text()
    return Backend.new(fmt, kind="render").dumps(outcome.to_hash())
