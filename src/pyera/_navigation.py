"""Navigation: context lookups and sentinel values.

Ports Puppet's ``sub_lookup.rb``.
"""

#: Sentinel for "no such context reference" (``None`` is a legitimate value).
_MISSING = object()


def _ctx_lookup(context, name, default=_MISSING):
    """Resolve a possibly-dotted context reference against nested containers.

    Hiera 5 defines ``%{trusted.certname}`` as *nested key access*, so a
    dotted name walks into nested dicts (and indexes lists with numeric
    segments), mirroring :meth:`LookupDict.lookup`.

    A flat key that literally contains dots wins over the nested walk, so an
    explicit ``{"a.b": 1}`` context entry keeps working.
    """
    if not isinstance(context, dict):
        return default
    if name in context:
        return context[name]
    if "." not in name:
        return default
    obj = context
    for segment in name.split("."):
        if isinstance(obj, dict):
            if segment not in obj:
                return default
            obj = obj[segment]
        elif isinstance(obj, (list, tuple)):
            try:
                obj = obj[int(segment)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return obj
