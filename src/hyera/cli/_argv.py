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


def _puppet_argv(argv):
    """Join a long value-option with its following token
    (--opt value -> --opt=value) so argparse never sees a bare
    -- or a dash-leading value as the *next* option -- Puppet's own
    option parser consumes a value this way. A bare -- not
    immediately following a value option (Puppet's own "everything after
    this is a key" marker) is left untouched.
    """
    out = []
    i, n = 0, len(argv)
    while i < n:
        token = argv[i]
        if token in _VALUE_OPTIONS and i + 1 < n:
            out.append("{}={}".format(token, argv[i + 1]))
            i += 2
            continue
        out.append(token)
        i += 1
    return out


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
