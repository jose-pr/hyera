# Derived from phiera/phiera.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Location resolution: expanding hierarchy levels into candidate source paths.

Ports Puppet's ``location_resolver.rb``.
"""

import logging

from ._interpolation import _format_source

LOGGER = logging.getLogger(__name__)


def _resolve_level_paths(level, base_path, context):
    """Yield the candidate source paths for a level in a given context.

    Glob levels expand their patterns against the filesystem (sorted for
    determinism); mapped_paths bind each element of a context collection to
    the item var and format the template; literal levels format each source
    with the context. Sources referencing an absent context var are skipped.
    """
    try:
        datadir = _format_source(level.backend.datadir, context)
    except (KeyError, IndexError, TypeError, AttributeError):
        return
    root = base_path / datadir

    if level.mapped:
        collection_var, item_var, template = level.mapped
        collection = context.get(collection_var)
        if collection is None:
            return
        if isinstance(collection, dict):
            items = list(collection.values())
        elif isinstance(collection, (list, tuple, set)):
            items = list(collection)
        else:
            items = [collection]
        for item in items:
            mapped_ctx = dict(context)
            mapped_ctx[item_var] = item
            try:
                yield root / _format_source(template, mapped_ctx)
            except (KeyError, IndexError, TypeError, AttributeError):
                continue
        return

    for source in level.sources:
        try:
            rel = _format_source(source, context)
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        if level.glob:
            # A glob over a directory that doesn't exist matches nothing
            # (Puppet's expand_globs behavior), rather than raising from
            # the underlying Path.glob. Only check the literal prefix --
            # the /-separated segments before the first one containing a
            # glob metacharacter, and never the last segment (a
            # wildcard-free pattern names a file, not a directory).
            segments = rel.split("/")
            prefix = segments[:-1]
            for i, segment in enumerate(prefix):
                if any(c in segment for c in "*?["):
                    prefix = segments[:i]
                    break
            if not root.joinpath(*prefix).is_dir():
                LOGGER.debug(
                    "Skipping glob %r under %s: %s is not a directory",
                    rel,
                    root,
                    root.joinpath(*prefix),
                )
                continue
            try:
                matches = sorted(root.glob(rel))
            except (FileNotFoundError, NotADirectoryError):
                LOGGER.debug(
                    "Glob %r under %s matched nothing (directory vanished)",
                    rel,
                    root,
                )
                matches = []
            for match in matches:
                yield match
        else:
            yield root / rel
