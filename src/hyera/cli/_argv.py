"""Argument-vector preparation: the way Puppet's option parser consumes values."""

#: Long-spelling value options _puppet_argv joins with their following
#: token (--opt value -> --opt=value), so a value that itself looks
#: like an option (--knock-out-prefix --, --default -x) reaches
#: argparse the way Puppet's own option parser would consume it.
_VALUE_OPTIONS = (
    "--merge",
    "--knock-out-prefix",
    "--type",
    "--default",
    "--facts",
    "--node",
    "--scope",
    "--hiera_config",
    "--environment",
    "--environmentpath",
    "--modulepath",
    "--basemodulepath",
    "--codedir",
    "--strict",
    "--render-as",
    "--loglevel",
)

#: A placeholder substituted for a value-option's argument when that
#: argument is exactly "--" (see _puppet_argv), and translated back
#: by _unplaceholder. Not a valid Puppet scope/type/path value, so it
#: never collides with a real one.
_DOUBLE_DASH_PLACEHOLDER = "\x00hyera-literal-double-dash\x00"


def _puppet_argv(argv):
    """Join a long value-option with its following token
    (--opt value -> --opt=value) so argparse never sees a bare
    -- or a dash-leading value as the *next* option -- Puppet's own
    option parser consumes a value this way. A bare -- not
    immediately following a value option (Puppet's own "everything after
    this is a key" marker) is left untouched.

    A value that is exactly "--" (Puppet's own knockout-prefix example)
    is routed through _DOUBLE_DASH_PLACEHOLDER instead of being
    joined as-is: CPython's own argparse.Action._get_values (through
    3.12) unconditionally removes one literal "--" from an action's
    collected argument strings before dispatching on nargs, even when
    that "--" arrived as an option's *value* (via --opt=--) rather
    than the "end of options" separator -- silently turning a single-value
    option's own value into an empty list instead of the string "--".
    Fixed in Python 3.13; this project's floor is 3.9. __call__ calls
    _unplaceholder on every field that went through this join.
    """
    out = []
    i, n = 0, len(argv)
    while i < n:
        token = argv[i]
        if token in _VALUE_OPTIONS and i + 1 < n:
            value = argv[i + 1]
            if value == "--":
                value = _DOUBLE_DASH_PLACEHOLDER
            out.append("{}={}".format(token, value))
            i += 2
            continue
        out.append(token)
        i += 1
    return out


def _unplaceholder(value):
    """Translate _DOUBLE_DASH_PLACEHOLDER back to "--"; any
    other value (None included) passes through unchanged."""
    return "--" if value == _DOUBLE_DASH_PLACEHOLDER else value
