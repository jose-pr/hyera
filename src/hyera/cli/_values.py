"""The option values of a parsed command, by field name."""

from __future__ import annotations

#: The ``Lookup`` fields that hold free text from the command line.
_FREE_TEXT_FIELDS = (
    "merge",
    "knock_out_prefix",
    "value_type",
    "default",
    "facts",
    "node",
    "scope",
    "hiera_config",
    "environment",
    "environmentpath",
    "modulepath",
    "basemodulepath",
    "codedir",
    "strict",
    "render_as",
)


def _free_text(command) -> dict:
    """Every free-text field of ``command``, by name."""
    return {name: getattr(command, name) for name in _FREE_TEXT_FIELDS}
