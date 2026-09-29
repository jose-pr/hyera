# Ported from Puppet 8 lib/puppet/pops/lookup/location_resolver.rb, the
# location half of hiera_config.rb (https://github.com/puppetlabs/puppet),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Location resolution: expanding hierarchy levels into candidate source paths.

Resolves a :class:`~hyera._hiera_config.HieraLevel`'s ``path``/``paths``/
``glob``/``globs``/``mapped_paths`` declarations into concrete filesystem
candidates, using the same ``%{...}`` interpolation engine as data values
(:mod:`hyera._interpolation`) instead of a private ``str.format`` grammar --
matching Puppet's own ``location_resolver.rb`` and the location half of
``hiera_config.rb``.
"""

import logging
import os
import re
import typing as _ty

from pathlib_next import Path

from ._interpolation import _scope_lookup, _to_puppet_str, interpolate
from ._invocation import Invocation
from .exceptions import ConfigError

_LOGGER = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"


class ResolvedLocation(_ty.NamedTuple):
    """One candidate source path: the declared (uninterpolated) template,
    the resolved filesystem path, and whether it exists.

    ``exist=False`` candidates are kept (never silently omitted) so a future
    ``explain`` can show them; callers that only want real files filter on
    ``.exist``.
    """

    original: str
    location: "Path"
    exist: bool


def _no_lookup(key, invocation):
    """The ``lookup`` callable for a location's :class:`Invocation`.

    Unreachable in practice: locations resolve with ``allow_methods=False``,
    which rejects every method call (``%{hiera()}``/``%{lookup()}``/
    ``%{alias()}``) -- the only way a sub-lookup would ever be attempted --
    before it could reach this callable.
    """
    raise RuntimeError("hierarchy locations never perform a sub-lookup")


def _win_slash(s: str) -> str:
    """On Windows, a host-path-shaped string (``datadir``, a ``path``/
    ``paths``/mapped-template entry) may use ``\\`` as its separator; convert
    it to ``/`` before interpolation and joining. Left alone on every other
    OS, and never applied to a glob pattern, where ``\\`` is Ruby's escape
    character."""
    return s.replace("\\", "/") if _WINDOWS else s


#: A Windows drive-letter anchor (``C:/...``); combined with a leading ``/``
#: or ``//`` (UNC), this is what "rooted" means for :func:`_pathname_plus`
#: and the glob root/pattern split.
_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


def _is_rooted(s: str) -> bool:
    return s.startswith("/") or bool(_DRIVE_RE.match(s))


def _split_anchor(s: str):
    """Split a ``/``-separated string into its leading anchor (``/``,
    ``//``, ``C:/`` or ``""``) and its non-empty components."""
    if s.startswith("//"):
        anchor, rest = "//", s[2:]
    elif s.startswith("/"):
        anchor, rest = "/", s[1:]
    elif _DRIVE_RE.match(s):
        anchor, rest = s[:3], s[3:]
    else:
        anchor, rest = "", s
    return anchor, [p for p in rest.split("/") if p != ""]


def _pathname_plus(base: str, rel: str) -> str:
    """Ruby ``Pathname#+``: join ``rel`` onto ``base`` lexically (``..``
    removes a preceding real component of ``base``, never touches the
    filesystem, and is dropped once a rooted ``base`` is exhausted but kept
    for a relative one). A rooted ``rel`` (leading ``/``, or on Windows a
    drive/UNC anchor) is returned unchanged. Everything in ``rel`` after its
    leading ``.``/``..`` handling is appended verbatim, so an inner ``..`` or
    ``//`` in ``rel`` survives untouched.
    """
    if _is_rooted(rel):
        return rel
    anchor, base_parts = _split_anchor(base)
    parts = [p for p in base_parts if p != "."]

    rel_parts = rel.split("/")
    i = 0
    while i < len(rel_parts) and rel_parts[i] in (".", ""):
        i += 1
    rel_parts = rel_parts[i:]

    idx = 0
    n = len(rel_parts)
    while idx < n and rel_parts[idx] == "..":
        if parts and parts[-1] != "..":
            parts.pop()
        elif not anchor:
            parts.append("..")
        # else: a rooted base is exhausted -- further '..' are dropped.
        idx += 1

    return anchor + "/".join(parts + rel_parts[idx:])


def _guarded_glob(root: str, pattern: str):
    """``sorted(Path(root).glob(pattern))``, but never raised over a missing
    directory: checks the pattern's literal (non-magic) leading segments
    exist as a real directory first, and swallows ``FileNotFoundError``/
    ``NotADirectoryError`` from the glob call itself. Returns ``[]``, never
    raises, for a missing or vanished directory (Puppet's ``expand_globs``
    matches nothing there, rather than crashing)."""
    root_path = Path(root)
    segments = pattern.split("/")
    prefix = segments[:-1]
    for i, segment in enumerate(prefix):
        if any(c in segment for c in "*?["):
            prefix = segments[:i]
            break
    if not root_path.joinpath(*prefix).is_dir():
        _LOGGER.debug("Skipping glob %r under %s: not a directory", pattern, root)
        return []
    try:
        return sorted(root_path.glob(pattern))
    except (FileNotFoundError, NotADirectoryError):
        _LOGGER.debug(
            "Glob %r under %s matched nothing (directory vanished)", pattern, root
        )
        return []


def _resolve_paths(datadir, declared, invocation, extension=None):
    """``path``/``paths`` (``location_resolver.rb:56-66``): each entry
    interpolates (methods disallowed), gets ``extension`` appended unless it
    already ends with it, and joins onto ``datadir``."""
    results = []
    for d in declared:
        p = interpolate(_win_slash(d), invocation, allow_methods=False)
        if extension and not p.endswith(extension):
            p = p + extension
        loc = _pathname_plus(datadir, p)
        results.append(ResolvedLocation(d, Path(loc), os.path.exists(loc)))
    return results


def _expand_globs(datadir, declared, invocation):
    """``glob``/``globs`` (``location_resolver.rb:43-47``): each pattern
    interpolates (methods disallowed), matches relative to ``datadir``
    unless the interpolated pattern is itself rooted, and drops any match
    that is a directory."""
    results = []
    for raw in declared:
        interp = interpolate(_win_slash(raw), invocation, allow_methods=False)
        if _is_rooted(interp):
            anchor, parts = _split_anchor(interp)
            root, pattern = anchor, "/".join(parts)
        else:
            root, pattern = datadir, interp
        original = _pathname_plus(datadir, interp)
        for match in _guarded_glob(root, pattern):
            if os.path.isdir(str(match)):
                continue
            results.append(ResolvedLocation(original, match, True))
    return results


def _mapped_collection_items(collection, collection_var, level_name):
    """The items a mapped_paths collection variable expands to:
    ``None``/``""``/``[]``/``{}`` -> no items; a ``str`` -> itself, alone; a
    ``list`` -> itself; a ``dict`` -> its ``[key, value]`` pairs, in order.
    Anything else (``bool``, then ``int``, then ``float`` -- checked in that
    order since ``bool`` is a Python ``int`` subclass) raises
    :class:`~hyera.ConfigError`, mirroring the Ruby ``NoMethodError`` Puppet
    itself raises calling ``.empty?`` on a non-collection value.
    """
    if collection is None or collection in ("", [], {}):
        return []
    if isinstance(collection, str):
        return [collection]
    if isinstance(collection, list):
        return collection
    if isinstance(collection, dict):
        return [[k, v] for k, v in collection.items()]
    if isinstance(collection, bool):
        type_name, rendered = "Boolean", _to_puppet_str(collection)
    elif isinstance(collection, int):
        type_name, rendered = "Integer", str(collection)
    elif isinstance(collection, float):
        type_name, rendered = "Float", _to_puppet_str(collection)
    else:
        type_name, rendered = type(collection).__name__, str(collection)
    raise ConfigError(
        "mapped_paths collection '{}' in hierarchy '{}' must be a String, an "
        "Array or a Hash, got {} {}".format(
            collection_var, level_name, type_name, rendered
        )
    )


def _expand_mapped_paths(datadir, level, invocation):
    """``mapped_paths`` (``location_resolver.rb:78-98``): the collection is
    a scope reference (dotted, ``::``-qualified, lenient); each item binds
    as one local variable layer (so an unqualified template reference reads
    the item, and ``%{::x}`` still reaches the top scope) while the template
    interpolates (methods disallowed)."""
    collection_var, item_var, template = level.locations
    collection = _scope_lookup(collection_var, invocation, "mapped_path[0]")
    items = _mapped_collection_items(collection, collection_var, level.name)

    results = []
    template_norm = _win_slash(template)
    for item in items:
        child_scope = invocation.scope.with_local_scope({item_var: item})
        child_inv = Invocation(child_scope, _no_lookup, lenient=True)
        p = interpolate(template_norm, child_inv, allow_methods=False)
        loc = _pathname_plus(datadir, p)
        results.append(ResolvedLocation(template, Path(loc), os.path.exists(loc)))
    return results


def resolve_locations(level, base_path, scope):
    """The candidate :class:`ResolvedLocation` list for one hierarchy level
    in a bound :class:`~hyera.Scope` (``hiera_config.rb:664-687``).

    ``level.datadir`` interpolates first, under ``strict`` (no shield) --
    Puppet does this outside ``Puppet.override(avoid_hiera_interpolation_
    errors: true)``. Every other location interpolates leniently (an
    undefined variable becomes ``''`` plus the strict-mode warning, never an
    error) through the same engine as data values, with method-call syntax
    (``%{lookup(...)}`` etc.) rejected outright -- Puppet's own restriction
    on this context.
    """
    strict_inv = Invocation(scope, _no_lookup)
    lenient_inv = Invocation(scope, _no_lookup, lenient=True)

    datadir = interpolate(_win_slash(level.datadir), strict_inv, allow_methods=False)
    base = _pathname_plus(Path(base_path).as_posix(), datadir)

    key = level.location_key
    if key in ("path", "paths"):
        return _resolve_paths(base, level.locations, lenient_inv)
    if key in ("glob", "globs"):
        return _expand_globs(base, level.locations, lenient_inv)
    if key == "mapped_paths":
        return _expand_mapped_paths(base, level, lenient_inv)
    # "uri"/"uris"/None: no locations yet (uri handling is not implemented).
    return []
