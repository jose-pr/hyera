"""Merge-option validation in Puppet's order."""

from __future__ import annotations

import typing as _ty

from ._scope import _UsageError

#: The four merge strategy names puppet lookup --merge accepts; there
#: are no array/set aliases.
_MERGE_STRATEGIES = ("first", "unique", "hash", "deep")


def _merge_options(
    merge: _ty.Optional[str],
    knock_out_prefix: _ty.Optional[str],
    sort_merged_arrays: bool,
    merge_hash_arrays: bool,
) -> _ty.Optional[dict]:
    """The merge options dict for ``Hiera.lookup``, or None for "let
    lookup_options decide". Raises _UsageError for a deep-only flag
    without ``--merge deep`` or an unknown strategy."""
    if (
        knock_out_prefix is not None or sort_merged_arrays or merge_hash_arrays
    ) and merge != "deep":
        raise _UsageError(
            "The options --knock-out-prefix, --sort-merged-arrays, "
            "and --merge-hash-arrays are only available with "
            "'--merge deep'"
        )
    if merge is not None and merge not in _MERGE_STRATEGIES:
        raise _UsageError(
            "The --merge option only accepts 'first', 'hash', 'unique', or 'deep'"
        )
    if merge == "deep":
        options: dict = {
            "strategy": "deep",
            "sort_merged_arrays": bool(sort_merged_arrays),
            "merge_hash_arrays": bool(merge_hash_arrays),
        }
        if knock_out_prefix is not None:
            options["knockout_prefix"] = knock_out_prefix
        return options
    return {"strategy": merge} if merge is not None else None
