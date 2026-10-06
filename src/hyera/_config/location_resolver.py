# Ported from Puppet 8 lib/puppet/pops/lookup/location_resolver.rb and hiera_config.rb
# (https://github.com/puppetlabs/puppet, Apache-2.0) and Ruby uri/lib/uri/rfc3986_parser.rb
# (https://github.com/ruby/uri, BSD-2-Clause). Modified by jose-pr. See NOTICE.
"""Location resolution: expanding hierarchy levels into candidate source paths.

Resolves a :class:`~hyera._config.hiera_config.HieraLevel`'s ``path``/``paths``/
``glob``/``globs``/``mapped_paths`` declarations into concrete filesystem
candidates, using the same ``%{...}`` interpolation engine as data values
(:mod:`hyera._lookup.interpolation`) instead of a private ``str.format`` grammar --
matching Puppet's own ``location_resolver.rb`` and the location half of
``hiera_config.rb``.
"""

from __future__ import annotations

import os
import re
import typing as _ty

from pathlib_next import Path

from .._lookup.interpolation import (
    _ruby_inspect_str,
    _scope_lookup,
    _to_puppet_str,
    interpolate,
)
from .._lookup.invocation import Invocation
from ..exceptions import ConfigError
from .dir_glob import glob
from .pathname import (
    _is_rooted,
    _native,
    _pathname_plus,
    _split_anchor,
    _win_slash,
)

if _ty.TYPE_CHECKING:
    from .._scope.scope import Scope
    from .hiera_config import HieraLevel


class ResolvedLocation(_ty.NamedTuple):
    """One candidate source: the declared (uninterpolated) template, the
    resolved location, whether it is a ``uri``/``uris`` location (a plain
    string, never a filesystem path) and whether it exists.

    ``exist=False`` candidates are kept (never silently omitted) so a future
    ``explain`` can show them; callers that only want real files filter on
    ``.exist``. A ``uri`` location is always ``exist=True`` (Puppet never
    fetches or stats it -- a provider decides what it means). ``location`` is
    a plain ``str``: the joined path exactly as written (a trailing ``/`` or
    an inner ``//`` kept), or the normalized URI.
    """

    original: str
    location: str
    is_uri: bool
    exist: bool


def _no_lookup(key, invocation):
    """The ``lookup`` callable for a location's :class:`Invocation`.

    Unreachable in practice: locations resolve with ``allow_methods=False``,
    which rejects every method call (``%{hiera()}``/``%{lookup()}``/
    ``%{alias()}``) -- the only way a sub-lookup would ever be attempted --
    before it could reach this callable.
    """
    raise RuntimeError("hierarchy locations never perform a sub-lookup")


def _glob_root_and_pattern(config_root: str, datadir: str, g: str):
    """The literal root directory and the (still glob-magic) pattern to
    hand :func:`glob` for one interpolated glob string ``g`` against a
    level's (also interpolated) ``datadir``. A rooted ``g`` wins outright;
    otherwise a rooted ``datadir`` anchors the walk and ``g`` joins onto
    its remainder; otherwise both join onto ``config_root``, so glob
    metacharacters in ``datadir`` are live (Puppet's own behavior) while
    the config root itself -- an absolute host path -- never is.
    """
    dd = _win_slash(datadir)
    if _is_rooted(g):
        anchor, parts = _split_anchor(g)
        return anchor, "/".join(parts)
    if _is_rooted(dd):
        anchor, parts = _split_anchor(dd)
        return anchor, _pathname_plus("/" + "/".join(parts), g).lstrip("/")
    return config_root, _pathname_plus(dd, g)


def _interpolate_path(text, invocation):
    """``text`` interpolated (methods disallowed) as a host path: on Windows
    a ``\\`` separator, written or interpolated, reads as ``/``."""
    return _win_slash(interpolate(_win_slash(text), invocation, allow_methods=False))


def _entry_datadir(level, config_root, invocation) -> str:
    """A level's data directory, rooted: Puppet's ``entry_datadir``. A
    version 5 ``datadir`` is joined onto the config root first and the result
    interpolated, so a variable that expands to an absolute path still lands
    under the root; a version 3 one is interpolated first (``Pathname`` of an
    absolute value is that value), a version 4 one never is.
    """
    if level.datadir_literal:
        return _pathname_plus(config_root, _win_slash(level.datadir))
    if level.datadir_base is not None:
        return _pathname_plus(config_root, _interpolate_path(level.datadir, invocation))
    return _interpolate_path(
        _pathname_plus(config_root, _win_slash(level.datadir)), invocation
    )


def _glob_datadir(entry: str, config_root: str) -> str:
    """``entry`` as the datadir :func:`_glob_root_and_pattern` takes: relative
    to ``config_root`` (whose own characters are never glob-live) when it is
    under it, else as is."""
    prefix = config_root.rstrip("/") + "/"
    return entry[len(prefix) :] if entry.startswith(prefix) else entry


def _resolve_paths(datadir, declared, invocation, extension=None):
    """``path``/``paths`` (``location_resolver.rb:56-66``): each entry
    interpolates (methods disallowed), gets ``extension`` appended unless it
    already ends with it, and joins onto ``datadir``.

    Existence is checked through ``invocation``'s filesystem memo
    (:meth:`~hyera._lookup.invocation.Invocation._memo_probe`), not a bare
    ``os.path.exists``: when ``invocation`` shares a memo with the rest of
    the current top-level lookup (``_LocationStore.location_entry_for``), this
    is the one real probe of ``loc`` that lookup ever makes, and a caller
    checking the same path again (to tell a directory from a plain miss)
    reads the cached result instead of probing twice.
    """
    results = []
    for d in declared:
        p = _interpolate_path(d, invocation)
        if extension and not p.endswith(extension):
            p = p + extension
        loc = _pathname_plus(datadir, p)
        exists = invocation._memo_probe(loc).kind != "absent"
        results.append(ResolvedLocation(d, _native(loc), False, exists))
    return results


class _GlobSpec(_ty.NamedTuple):
    """One ``glob``/``globs`` declared string, interpolated and rooted, before
    it is walked -- the interpolation half of ``_expand_globs``, split out
    so a caller (``_LocationStore.location_entry_for``) can defer the actual
    ``Dir.glob`` match listing to per-lookup materialization instead of
    paying for it on every hierarchy build."""

    original: str
    root: str
    pattern: str


def _glob_specs(config_root, datadir, declared, invocation) -> "_ty.List[_GlobSpec]":
    """Interpolate and root every declared ``glob``/``globs`` string
    (``location_resolver.rb:43-47``'s pattern half), without walking the
    filesystem."""
    base = _pathname_plus(config_root, datadir)
    specs = []
    for raw in declared:
        # Never _win_slash a glob string: '\' is Ruby's escape character
        # here, not a Windows path separator.
        interp = interpolate(raw, invocation, allow_methods=False)
        root, pattern = _glob_root_and_pattern(config_root, datadir, interp)
        original = _pathname_plus(base, interp)
        specs.append(_GlobSpec(original, root, pattern))
    return specs


def resolve_glob_specs(level, base_path, scope, refs=None, fs_memo=None):
    """The rooted, interpolated :class:`_GlobSpec` list for one ``glob``/
    ``globs`` hierarchy level -- the location half of :func:`resolve_locations`
    for this one location kind, kept separate because its own caller
    resolves the actual matches lazily. See :func:`resolve_locations` for
    ``refs``/``fs_memo``."""
    strict_inv = Invocation(
        scope, _no_lookup, scope_interpolations=refs, _fs_memo=fs_memo
    )
    lenient_inv = Invocation(
        scope,
        _no_lookup,
        lenient=level.lenient_locations,
        scope_interpolations=refs,
        _fs_memo=fs_memo,
    )
    config_root = Path(base_path).as_posix()
    entry = _entry_datadir(level, config_root, strict_inv)
    datadir = _glob_datadir(entry, config_root)
    return _glob_specs(config_root, datadir, level.locations, lenient_inv)


def _expand_globs(config_root, datadir, declared, invocation):
    """``glob``/``globs`` (``location_resolver.rb:43-47``): each pattern
    interpolates (methods disallowed) and matches through hyera's own Ruby
    ``Dir.glob`` port (:func:`glob`), rooted by :func:`_glob_root_and_pattern`
    -- live ``datadir`` metacharacters, a rooted pattern or ``datadir``
    anchoring the walk instead. Any match that is a directory is dropped
    (``reject(&:directory?)``); a missing or unreadable directory simply
    contributes no matches.

    Eager, unlike :func:`resolve_glob_specs`: used by
    :meth:`~hyera._config.hiera_config.HieraLevel.paths`, which has no lazy
    materialization step to defer the walk to.
    """
    results = []
    for original, root, pattern in _glob_specs(
        config_root, datadir, declared, invocation
    ):
        for match in glob(root, pattern):
            if os.path.isdir(match):
                continue
            results.append(ResolvedLocation(original, _native(match), False, True))
    return results


# --- Ruby URI() acceptance and normalization (uri/rfc3986_parser.rb) -------
# A port of Ruby's RFC 3986 grammar to Python `re`: `\h` -> `[0-9A-Fa-f]`, `\g<name>` calls
# inlined, possessive quantifiers made greedy (same language), only the groups read below named.
_HEXDIG = "[0-9A-Fa-f]"
_PCT = "%" + _HEXDIG + _HEXDIG
_USERINFO = "(?:" + _PCT + "|[!$&-.0-9:;=A-Z_a-z~])*"
_SCHEME_CHARS = "[A-Za-z][+\\-.0-9A-Za-z]*"
_SEG = "(?:" + _PCT + "|[!$&-.0-9:;=@A-Z_a-z~/])"
_SEG_NC = "(?:" + _PCT + "|[!$&-.0-9;=@A-Z_a-z~])"
_FRAGMENT = "(?:" + _PCT + "|[!$&-.0-9:;=@A-Z_a-z~/?])*"

_DEC_OCTET = "(?:[1-9]\\d|1\\d{2}|2[0-4]\\d|25[0-5]|\\d)"
_IPV4ADDRESS = _DEC_OCTET + "\\." + _DEC_OCTET + "\\." + _DEC_OCTET + "\\." + _DEC_OCTET
_LS32 = "(?:" + _HEXDIG + "{1,4}:" + _HEXDIG + "{1,4}|" + _IPV4ADDRESS + ")"
_H4 = _HEXDIG + "{1,4}"
_IPV6ADDRESS = (
    "(?:"
    + ("(?:" + _H4 + ":){6}" + _LS32)
    + ("|::(?:" + _H4 + ":){5}" + _LS32)
    + ("|" + _H4 + "?::(?:" + _H4 + ":){4}" + _LS32)
    + ("|(?:(?:" + _H4 + ":)?" + _H4 + ")?::(?:" + _H4 + ":){3}" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,2}" + _H4 + ")?::(?:" + _H4 + ":){2}" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,3}" + _H4 + ")?::" + _H4 + ":" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,4}" + _H4 + ")?::" + _LS32)
    + ("|(?:(?:" + _H4 + ":){0,5}" + _H4 + ")?::" + _H4)
    + ("|(?:(?:" + _H4 + ":){0,6}" + _H4 + ")?::")
    + ")"
)
_IPVFUTURE = "v" + _HEXDIG + "+\\.[!$&-.0-9:;=A-Z_a-z~]+"
_IP_LITERAL = "\\[(?:" + _IPV6ADDRESS + "|" + _IPVFUTURE + ")\\]"
_REG_NAME = "(?:" + _PCT + "|[!$&-.0-9;=A-Z_a-z~])*"
_HOST = "(?:" + _IP_LITERAL + "|" + _IPV4ADDRESS + "|" + _REG_NAME + ")"

_AUTHORITY = "(?:" + _USERINFO + "@)?" + _HOST + "(?::(?P<port>\\d*))?"
_PATH_ABEMPTY = "(?:/" + _SEG + "*)?"
_QUERY = "(?P<query>[^#]*)"

_RFC3986_URI = re.compile(
    "(?P<scheme>"
    + _SCHEME_CHARS
    + "):"
    + "(?:"
    + "//"
    + _AUTHORITY
    + _PATH_ABEMPTY
    + "|/(?:(?!/)"
    + _SEG
    + "+)?"
    + "|(?!/)"
    + _SEG
    + "+"
    + "|"
    + ")"
    + "(?:\\?"
    + _QUERY
    + ")?"
    + "(?:#(?:"
    + _FRAGMENT
    + "))?"
)

_RFC3986_RELATIVE_REF = re.compile(
    "(?:"
    + "//"
    + _AUTHORITY
    + _PATH_ABEMPTY
    + "|/"
    + _SEG
    + "*"
    + "|"
    + _SEG_NC
    + "+(?:/"
    + _SEG
    + "*)?"
    + "|"
    + ")"
    + "(?:\\?"
    + _QUERY
    + ")?"
    + "(?:#(?:"
    + _FRAGMENT
    + "))?"
)

#: Default port dropped by ``URI#to_s`` when it exactly matches the
#: scheme's own default (as in Ruby 4.0.7; only the schemes
#: Puppet's own oracle exercises are covered).
_DEFAULT_PORTS = {
    "http": "80",
    "ws": "80",
    "https": "443",
    "wss": "443",
    "ftp": "21",
    "ldap": "389",
}


def _ruby_uri(text: str) -> str:
    """Validate ``text`` against Ruby's ``URI()`` grammar and reproduce its
    ``#to_s`` normalization (measured, not derivable from the grammar
    alone): lowercase the scheme; drop an empty port or one equal to the
    scheme's own default; percent-encode a literal space in the query as
    ``%20``. Everything else -- host case, path, fragment -- passes through
    verbatim. Raises :class:`~hyera.ConfigError` with Ruby's own message
    when neither the absolute nor the relative grammar matches.
    """
    match = _RFC3986_URI.fullmatch(text) or _RFC3986_RELATIVE_REF.fullmatch(text)
    if match is None:
        raise ConfigError("bad URI (is not URI?): " + _ruby_inspect_str(text))

    edits = []  # (start, end, replacement), applied right-to-left.
    scheme = match.groupdict().get("scheme")
    if scheme is not None:
        lowered = scheme.lower()
        if lowered != scheme:
            edits.append((match.start("scheme"), match.end("scheme"), lowered))
    port = match.groupdict().get("port")
    if port is not None:
        drop = port == "" or (
            scheme is not None and port == _DEFAULT_PORTS.get(scheme.lower())
        )
        if drop:
            # Drop the port digits and the ':' immediately before them.
            edits.append((match.start("port") - 1, match.end("port"), ""))
    query = match.groupdict().get("query")
    if query is not None and " " in query:
        edits.append(
            (match.start("query"), match.end("query"), query.replace(" ", "%20"))
        )

    result = text
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _expand_uris(declared, invocation):
    """``uri``/``uris`` (``location_resolver.rb:71-76``'s ``expand_uris``):
    each declared string interpolates like any other location (lenient, no
    method calls), is validated/normalized by :func:`_ruby_uri`, and always
    "exists" -- Puppet never fetches or stats a ``uri`` location; a provider
    decides what it means."""
    results = []
    for declared_uri in declared:
        interp = interpolate(declared_uri, invocation, allow_methods=False)
        normalized = _ruby_uri(interp)
        results.append(ResolvedLocation(declared_uri, normalized, True, True))
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
    interpolates (methods disallowed).

    The collection read (via ``invocation``, outside the loop) is recorded
    as usual -- a cache keyed on referenced variables must rebuild when the
    collection itself changes. Each item's own interpolation runs inside
    ``invocation.with_local_memory_eluding(item_var)``, so any reference to
    the per-item local variable it binds is dropped again once the item is
    done: the item is a derived, per-iteration value, never itself a
    variable the cache should key on (``location_resolver.rb:90``).
    """
    collection_var, item_var, template = level.locations
    collection = _scope_lookup(collection_var, invocation, "mapped_path[0]")
    items = _mapped_collection_items(collection, collection_var, level.name)

    results = []
    for item in items:
        with invocation.with_local_memory_eluding(item_var):
            child_scope = invocation.scope.with_local_scope({item_var: item})
            child_inv = Invocation(
                child_scope,
                _no_lookup,
                lenient=invocation.lenient,
                scope_interpolations=invocation.scope_interpolations,
                _fs_memo=invocation._fs_memo,
            )
            p = _interpolate_path(template, child_inv)
            loc = _pathname_plus(datadir, p)
            exists = child_inv._memo_probe(loc).kind != "absent"
            results.append(ResolvedLocation(template, _native(loc), False, exists))
    return results


def resolve_locations(
    level: "HieraLevel",
    base_path: "_ty.Union[str, os.PathLike[str]]",
    scope: "Scope",
    refs: "_ty.Optional[list]" = None,
    fs_memo: "_ty.Optional[dict]" = None,
) -> "_ty.Optional[_ty.List[ResolvedLocation]]":
    """The candidate :class:`ResolvedLocation` list for one hierarchy level
    in a bound :class:`~hyera.Scope` (``hiera_config.rb:664-687``).

    ``level.datadir`` interpolates first, under ``strict`` (no shield) --
    Puppet does this outside ``Puppet.override(avoid_hiera_interpolation_
    errors: true)``. Every other location interpolates leniently (an
    undefined variable becomes ``''`` plus the strict-mode warning, never an
    error) through the same engine as data values, with method-call syntax
    (``%{lookup(...)}`` etc.) rejected outright -- Puppet's own restriction
    on this context.

    Never called for a ``glob``/``globs`` level from the main lookup
    pipeline (see :func:`resolve_glob_specs`); still handles that kind
    itself (eagerly) for :meth:`~hyera._config.hiera_config.HieraLevel.paths`,
    which has no lazy materialization step of its own.

    ``refs``, when given, is a list every scope read made while resolving
    this level appends itself to (:meth:`~hyera._lookup.invocation.Invocation.
    remember_scope_lookup`), shared across every level of one hierarchy
    build by the caller (``_LocationStore.location_entry_for``) so the whole
    hierarchy's build is keyed on one combined reference set, matching
    Puppet's own single ``scope_interpolations_stable?`` check per rebuild.
    Omitted (``None``, the default), nothing is recorded -- used by
    :meth:`~hyera._config.hiera_config.HieraLevel.paths`, which has no cache to key.

    ``fs_memo``, when given, is the current top-level lookup's filesystem
    probe memo (:attr:`~hyera._lookup.invocation.Invocation._fs_memo`), shared so
    every location this level (and the rest of the same hierarchy build)
    probes is probed at most once for the whole lookup. Omitted, each
    location probed here gets its own, unshared one-entry memo.
    """
    strict_inv = Invocation(
        scope, _no_lookup, scope_interpolations=refs, _fs_memo=fs_memo
    )
    lenient_inv = Invocation(
        scope,
        _no_lookup,
        lenient=level.lenient_locations,
        scope_interpolations=refs,
        _fs_memo=fs_memo,
    )

    root = base_path if level.datadir_base is None else level.datadir_base
    config_root = Path(root).as_posix()
    base = _entry_datadir(level, config_root, strict_inv)
    datadir = _glob_datadir(base, config_root)

    key = level.location_key
    if key is None:
        # No location key at all: the caller (a function provider) calls
        # its function once, with no location -- distinct from a location
        # key that expands to zero candidates.
        return None
    if key in ("path", "paths"):
        extension = level.extension
        if extension:
            extension = interpolate(extension, strict_inv, allow_methods=False)
        return _resolve_paths(base, level.locations, lenient_inv, extension=extension)
    if key in ("glob", "globs"):
        return _expand_globs(config_root, datadir, level.locations, lenient_inv)
    if key == "mapped_paths":
        return _expand_mapped_paths(base, level, lenient_inv)
    # key in ("uri", "uris")
    return _expand_uris(level.locations, lenient_inv)
