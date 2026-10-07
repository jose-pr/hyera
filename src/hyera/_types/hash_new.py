# Ported from Puppet 8 lib/puppet/pops/types/types.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The tree forms of ``Hash.new``: ``PHashType.new_function`` in ``types.rb``.

``Hash.new(entries, 'tree')`` and ``Hash.new(entries, 'hash_tree')`` build a
nested Hash from ``[path, value]`` entries (``types.rb:2810-2832``), and
:class:`ArrayKey` is the hashable form of an Array used as a Hash key (Ruby
allows any value as a key; a Python ``dict`` does not).
"""

from __future__ import annotations

import typing as _ty

from ..exceptions import HieraLookupError

__all__ = ["ArrayKey", "freeze_key", "new_tree_hash"]

_BUILD_OPTIONS = ("tree", "hash_tree")


class ArrayKey(tuple):
    """An Array used as a Hash key. It renders as Ruby's ``Array#to_s``
    (``["a", 1]``) wherever a key is rendered."""

    __slots__ = ()

    def __str__(self) -> str:
        from .._lookup.interpolation import _ruby_inspect

        return _ruby_inspect(list(self))


def freeze_key(key: _ty.Any) -> _ty.Any:
    """``key`` as a ``dict`` key: an Array (at any depth) becomes an
    :class:`ArrayKey`; a Hash cannot be a key in Python.

    :raises HieraLookupError: for a Hash key.
    """
    if isinstance(key, (list, tuple)):
        return ArrayKey(freeze_key(v) for v in key)
    if isinstance(key, dict):
        raise HieraLookupError("unusable Hash key: a Hash cannot be a key")
    return key


def _ruby_class(value: _ty.Any) -> str:
    if value is None:
        return "NilClass"
    if isinstance(value, bool):
        return "TrueClass" if value else "FalseClass"
    return {int: "Integer", float: "Float", str: "String", list: "Array"}.get(
        type(value), "Object"
    )


def _is_tree_entry(entry: _ty.Any) -> bool:
    """``Tuple[Array, Any]``."""
    return (
        isinstance(entry, (list, tuple))
        and len(entry) == 2
        and isinstance(entry[0], (list, tuple))
    )


def _array_as_hash(value: _ty.Any) -> _ty.Any:
    """``PHashType.array_as_hash`` (``types.rb:2778``): every Array, at any
    depth, becomes a Hash keyed by its indexes."""
    if not isinstance(value, (list, tuple)):
        return value
    return {i: _array_as_hash(v) for i, v in enumerate(value)}


def _step(memo: _ty.Any, key: _ty.Any) -> _ty.Any:
    """One step down a path: an Array is indexed, a Hash gains an empty Hash
    for a missing key, anything else cannot be walked (``types.rb:2825``)."""
    if isinstance(memo, list):
        if not isinstance(key, int) or isinstance(key, bool):
            raise HieraLookupError(
                "no implicit conversion of {} into Integer".format(_ruby_class(key))
            )
        return memo[key] if -len(memo) <= key < len(memo) else None
    if isinstance(memo, dict):
        key = freeze_key(key)
        if key not in memo:
            memo[key] = {}
        return memo[key]
    raise HieraLookupError(
        "undefined method 'has_key?' for an instance of {}".format(_ruby_class(memo))
    )


def _assign_list(target: list, key: _ty.Any, value: _ty.Any) -> None:
    """``Array#[]=`` for an Integer index: past the end pads with nil."""
    if not isinstance(key, int) or isinstance(key, bool):
        raise HieraLookupError(
            "no implicit conversion of {} into Integer".format(_ruby_class(key))
        )
    if key < -len(target):
        raise HieraLookupError(
            "index {} too small for array; minimum: -{}".format(key, len(target))
        )
    if key >= len(target):
        target.extend([None] * (key - len(target) + 1))
    target[key] = value


def _assign_string(text: str, key: _ty.Any, value: _ty.Any) -> str:
    """``String#[]=`` with an Integer index or a substring; Python strings
    are immutable, so the caller stores the result back."""
    if not isinstance(value, str):
        raise HieraLookupError(
            "no implicit conversion of {} into String".format(_ruby_class(value))
        )
    if isinstance(key, str):
        if key not in text:
            raise HieraLookupError("string not matched")
        return text.replace(key, value, 1)
    if isinstance(key, int) and not isinstance(key, bool):
        if not -len(text) <= key <= len(text):
            raise HieraLookupError("index {} out of string".format(key))
        at = key + len(text) if key < 0 else key
        return text[:at] + value + text[at + 1 :]
    raise HieraLookupError(
        "no implicit conversion of {} into Integer".format(_ruby_class(key))
    )


def _store(parent: _ty.Any, parent_key: _ty.Any, target: _ty.Any, key, value) -> None:
    if isinstance(target, dict):
        target[freeze_key(key)] = value
    elif isinstance(target, list):
        _assign_list(target, key, value)
    elif isinstance(target, str):
        updated = _assign_string(target, key, value)
        if isinstance(parent, list):
            parent[parent_key] = updated
        else:
            parent[freeze_key(parent_key)] = updated
    else:
        raise HieraLookupError(
            "undefined method '[]=' for an instance of {}".format(_ruby_class(target))
        )


def _merge_root(result: dict, value: _ty.Any) -> None:
    """An entry whose path is empty merges its value into the result; an
    Array is turned into a Hash first (``types.rb:2819``)."""
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            result[index] = item
    elif isinstance(value, dict):
        result.update(value)
    else:
        raise HieraLookupError(
            "no implicit conversion of {} into Hash".format(_ruby_class(value))
        )


def new_tree_hash(entries: _ty.Any, args: _ty.Sequence[_ty.Any]) -> dict:
    """``Hash.new(entries, build)`` (``types.rb:2805``).

    :param entries: a non-empty Array of ``[path, value]`` entries, each
        ``path`` an Array of keys.
    :param args: the arguments after ``entries``: exactly one, ``'tree'``
        (Arrays stay Arrays) or ``'hash_tree'`` (every Array becomes a Hash).
    :returns: the nested Hash.
    :raises HieraLookupError: for a wrong argument count, entries that are
        not paths and values, an unknown option, or a path that cannot be
        walked.
    """
    if len(args) != 1:
        raise HieraLookupError(
            "'new_hash' expects between 1 and 2 arguments, got {}".format(1 + len(args))
        )
    build = args[0]
    if not (
        isinstance(entries, (list, tuple))
        and entries
        and all(_is_tree_entry(e) for e in entries)
    ):
        raise HieraLookupError(
            "The function 'new_hash' was called with arguments it does not "
            "accept: 'from' expects an Array[Tuple[Array, Any], 1] value"
        )
    if not isinstance(build, str) or build not in _BUILD_OPTIONS:
        raise HieraLookupError(
            "The function 'new_hash' was called with arguments it does not "
            "accept: 'build_option' expects a match for Enum['hash_tree', 'tree']"
        )
    all_hashes = build == "hash_tree"
    result: dict = {}
    for path, value in entries:
        if not path:
            _merge_root(result, value)
            continue
        parent: _ty.Any = None
        parent_key: _ty.Any = None
        target: _ty.Any = result
        for key in path[:-1]:
            parent, parent_key = target, key
            target = _step(target, key)
        _store(
            parent,
            parent_key,
            target,
            path[-1],
            _array_as_hash(value) if all_hashes else value,
        )
    return result
