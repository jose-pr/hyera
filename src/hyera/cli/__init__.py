"""Command-line interface for hyera, built on duho.

Accepts puppet lookup's own flag set (see README.md "Command line"):

hyera KEY --hiera_config hiera.yaml --facts facts.yaml --node N

Designed for unattended use: no interactive prompts, deterministic output,
and exit codes: 0 found (or --default printed), 1 key not found, 2 any
other error (one stderr line; -v or DUHO_TRACEBACK=1 adds the traceback),
130 interrupted. Output is rendered the way puppet lookup --render-as
s|json|yaml does (hyera._output.render), written as UTF-8 bytes with LF
line endings whatever the console/locale encoding.
"""

import logging as _logging
import sys as _sys
import typing as _ty

try:
    import duho
    import duho.logging as _duho_logging
except ModuleNotFoundError as _e:
    if _e.name != "duho":
        raise
    duho = None

from .. import __version__
from ..exceptions import BackendError, KeyNotFoundError
from ..backends import Backend
from ._argv import _free_text, _puppet_argv
from ._options import _merge_options
from ._run import _describe, _render, _resolve
from ._scope import (
    _PUPPET_VERSION,
    _UsageError,
    _build_scope,
    _parse_scope,
    _parse_scope_value,
)
from ._stdout import _emit, _silence_stdout

__all__ = ["main"]

_LOGGER = _logging.getLogger(__name__)

#: Printed (to stderr) when the cli extra (duho) is not installed.
# Double-quoted, not single-quoted: a single-quoted extras install fails
# when pasted into cmd.exe, where single quotes are literal and pip sees
# the quote characters themselves as part of the argument; double quotes
# work in cmd.exe, PowerShell and POSIX shells alike.
_NO_CLI_EXTRA_HINT = (
    "hyera: the command-line interface needs the cli extra: " 'pip install "hyera[cli]"'
)

if duho is not None:
    from . import _args

    class Lookup(duho.LoggingArgs, duho.Cli):
        """Look up keys in Hiera data the way puppet lookup does.

        Needs --facts. Exit status: 0 found (or --default printed), 1 no
        key found, 2 any other error, 130 interrupted.
        """

        _version_ = __version__
        _mcp_ = True
        _utf8_stdio_ = False
        _parsername_ = "hyera"
        _logger_name_ = "hyera"
        _exit_codes_ = _args._EXIT_CODES
        _examples_ = _args._EXAMPLES

        keys: _args._KeysArg = None
        """Keys to look up, the first one found wins; omit only with --explain-options."""
        merge: _args._MergeArg = None
        """Merge strategy first, unique, hash or deep; omitted, the key's lookup_options decide, else first."""
        knock_out_prefix: _args._KnockOutPrefixArg = None
        """With --merge deep: a regular expression; matching array elements are removed and matching strings blanked; omitted, nothing is removed."""
        sort_merged_arrays: _args._SortMergedArraysArg = False
        """With --merge deep: sort merged arrays; omitted, they keep their order."""
        merge_hash_arrays: _args._MergeHashArraysArg = False
        """With --merge deep: merge hashes inside arrays by position; omitted, array elements are united."""
        value_type: _args._ValueTypeArg = None
        """Puppet type the value (and --default) must have, e.g. Array[String]; omitted, any value."""
        default: _args._DefaultArg = None
        """Text printed when no key is found; omitted, a miss exits 1 and prints nothing."""
        explain: _args._ExplainArg = False
        """Print how the value was found instead of the value; omitted, print the value."""
        explain_options: _args._ExplainOptionsArg = False
        """Print only how lookup_options was assembled; omitted, print the value."""

        facts: _args._FactsArg = None
        """REQUIRED: facts file, .json, .yaml or .yml (another name is read as JSON, then YAML); without it every lookup fails."""
        node: _args._NodeArg = None
        """Node name used in messages only; omitted, the local host name."""
        scope: _args._ScopeArg = None
        """Repeatable NAME=VALUE node parameter, VALUE is YAML and a dotted NAME builds a hash; omitted, none."""

        hiera_config: _args._HieraConfigArg = None
        """Path to the base hiera.yaml; omitted, ./hiera.yaml if it exists, else Puppet's built-in configuration."""
        environment: _args._EnvironmentArg = None
        """Environment name; omitted, production."""
        environmentpath: _args._EnvironmentPathArg = None
        """Environment directories separated by the OS path separator; omitted, no environment layer."""
        modulepath: _args._ModulepathArg = None
        """Module directories of the environment, separated by the OS path separator; omitted, the environment's own."""
        basemodulepath: _args._BasemodulepathArg = None
        """Module directories shared by every environment, separated by the OS path separator; omitted, none."""
        codedir: _args._CodedirArg = None
        """Puppet's $codedir; omitted, Puppet's own default for the platform."""
        strict: _args._StrictArg = None
        """Undefined-variable handling: off, warning or error; omitted, warning."""

        render_as: _args._RenderAsArg = None
        """Output format s, json or yaml; omitted, yaml (s when explaining)."""

        debug: _args._DebugArg = False
        """Log debug messages, same as -vv."""

        def _verbose_loglevel_(self) -> int:
            """Puppet's own scheme from a WARNING base: none warning, -v
            info, -vv or -d/--debug debug, -vvv trace, -q error, -qq
            critical."""
            levels = list(_duho_logging.VERBOSE_LEVELS.keys())
            base = levels.index(_logging.WARNING)
            index = base + self.verbose - self.quiet
            index = max(0, min(index, len(levels) - 1))
            level = levels[index]
            if self.debug:
                level = min(level, _logging.DEBUG)
            return level

        def _fail(self, text) -> int:
            """Log one ERROR-level line and return exit code 2.

            The traceback is attached only when asked for (-v, -d/--debug
            or DUHO_TRACEBACK=1) and an exception is being handled.
            """
            wanted = self.verbose > 0 or self.debug or _duho_logging.traceback_enabled()
            handling = _sys.exc_info()[0] is not None
            _LOGGER.error("%s", text, exc_info=True if wanted and handling else None)
            return 2

        def __call__(self) -> int:
            """Run the lookup duho parsed into this instance's fields and
            return the process exit code (see the class docstring).

            :returns: the process exit code.
            """
            opts = _free_text(self)
            keys = list(self.keys or ()) + list(self._passthrough_ or ())
            try:
                merge_options = _merge_options(
                    opts["merge"],
                    opts["knock_out_prefix"],
                    self.sort_merged_arrays,
                    self.merge_hash_arrays,
                )
            except _UsageError as e:
                return self._fail(str(e))

            explaining = self.explain or self.explain_options
            only_options = self.explain_options and not self.explain
            if not keys:
                if not only_options:
                    return self._fail("No keys were given to lookup.")
                keys = ["__global__"]

            render_as = opts["render_as"]
            fmt = (render_as if render_as is not None else "yaml").lower()
            if render_as is None and explaining:
                fmt = "s"
            if Backend.find(fmt, kind="render") is None:
                return self._fail("Unknown rendering format '{}'".format(fmt))

            joined_keys = ", ".join(keys)
            try:
                scope = _build_scope(
                    _parse_scope(opts["scope"]),
                    opts["facts"],
                    opts["node"],
                    opts["environment"],
                    opts["strict"],
                )
            except (_UsageError, BackendError, TypeError, ValueError) as e:
                return self._fail(str(e))

            try:
                outcome = _resolve(
                    opts, scope, keys, merge_options, explaining, only_options
                )
            except KeyNotFoundError as e:
                # Puppet's own miss prints nothing and exits 1, with or
                # without -v; only -d/--debug (or -vv, or --loglevel) shows
                # this DEBUG line.
                _LOGGER.debug("%s", e)
                return 1
            except Exception as e:  # HieraError, OSError, anything unexpected
                return self._fail(
                    "Lookup of key '{}' failed: {}".format(joined_keys, _describe(e))
                )

            try:
                text = _render(fmt, outcome, explaining)
            except Exception as e:
                return self._fail(
                    "Cannot render the value of key '{}': {}".format(
                        joined_keys, _describe(e)
                    )
                )
            try:
                _emit(text)
            except OSError:
                # The reader is gone or the device is full: nothing can be
                # reported on stdout, and one more line on stderr would be
                # noise for a caller that stopped listening.
                _silence_stdout()
                return 2
            return 0

    __all__.append("Lookup")


def main(argv: _ty.Optional[_ty.Sequence[str]] = None) -> int:
    """The ``hyera`` console script and ``python -m hyera`` entry point.

    :param argv: the argument vector, excluding the program name; defaults
        to ``sys.argv[1:]``.
    :returns: 0 found (or ``--default`` printed), 1 no key found, 2 any
        other error, or the CLI extra is not installed, 130 interrupted.
    """
    # duho.main sets up stderr logging (honoring -v/-q/--loglevel) and
    # dispatches to Lookup.__call__, whose int return becomes the exit code.
    if duho is None:
        print(_NO_CLI_EXTRA_HINT, file=_sys.stderr)
        return 2
    argv = _puppet_argv(list(_sys.argv[1:] if argv is None else argv))
    try:
        return duho.main(Lookup, argv)
    except KeyboardInterrupt:
        return 130
