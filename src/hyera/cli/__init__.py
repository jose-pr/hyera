"""Command-line interface for hyera, built on duho.

Accepts puppet lookup's own flag set (see README.md "Command line"):

hyera KEY --hiera_config hiera.yaml --facts facts.yaml --node N

Designed for unattended use: no interactive prompts, deterministic output,
and meaningful exit codes: 0 found (or --default printed), 1
key not found, 2 any other error (one stderr line; -v or
DUHO_TRACEBACK=1 adds the traceback). Output is rendered the way
puppet lookup --render-as s|json|yaml does (hyera._output.render),
written as UTF-8 bytes with LF line endings whatever the console/locale
encoding.
"""

import logging as _logging
import os as _os
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
from ..exceptions import BackendError, KeyNotFoundError, _one_line, HieraError
from ..core import Hiera
from .._types.parser import parse_type
from ..backends import Backend
from ._argv import _puppet_argv, _unplaceholder
from ._options import _merge_options
from ._scope import (
    _PUPPET_VERSION,
    _ScopeError,
    _UsageError,
    _build_scope,
    _parse_scope,
    _parse_scope_value,
    _paths,
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


def _describe(e) -> str:
    """One-line description of an exception for the CLI's failure log."""
    if isinstance(e, (HieraError, OSError)):
        return _one_line(e)
    return "{}: {}".format(type(e).__name__, _one_line(e))


if duho is not None:
    from . import _args

    class Lookup(duho.LoggingArgs, duho.Cli):
        """Look up keys in Hiera data the way puppet lookup does.

        Exit status: 0 found (or --default printed), 1 no key found, 2
        any other error.
        """

        _version_ = __version__
        _mcp_ = True
        _parsername_ = "hyera"
        _logger_name_ = "hyera"

        keys: _args._KeysArg = None
        """Keys to look up; the first one found wins."""
        merge: _args._MergeArg = None
        """Merge strategy: first, unique, hash or deep. Overrides the
        data's lookup_options; omitted, lookup_options decide, else the
        first value found wins."""
        knock_out_prefix: _args._KnockOutPrefixArg = None
        """With --merge deep: a prefix that marks a value or key for removal."""
        sort_merged_arrays: _args._SortMergedArraysArg = False
        """With --merge deep: sort merged arrays."""
        merge_hash_arrays: _args._MergeHashArraysArg = False
        """With --merge deep: deep-merge hashes inside arrays by position."""
        value_type: _args._ValueTypeArg = None
        """Assert the value (and --default) has this Puppet type, e.g. Array[String]."""
        default: _args._DefaultArg = None
        """String printed when no key is found."""
        explain: _args._ExplainArg = False
        """Show how the value was found, instead of only its value."""
        explain_options: _args._ExplainOptionsArg = False
        """Show only how lookup_options was assembled."""

        facts: _args._FactsArg = None
        """Facts file: .json, .yaml or .yml; any other name is read as
        JSON, then YAML."""
        node: _args._NodeArg = None
        """Node name used in messages; sets no fact."""
        scope: _args._ScopeArg = None
        """Node parameter (top-scope variable); VALUE is YAML and a
        dotted NAME builds a hash."""

        hiera_config: _args._HieraConfigArg = None
        """Path to the base hiera.yaml (default: ./hiera.yaml if it
        exists, else Puppet's built-in default configuration)."""
        environment: _args._EnvironmentArg = None
        """Environment name."""
        environmentpath: _args._EnvironmentPathArg = None
        """Environment directories, separated by the OS path separator."""
        modulepath: _args._ModulepathArg = None
        """Module directories for the current environment, separated by
        the OS path separator (replaces the default modulepath)."""
        basemodulepath: _args._BasemodulepathArg = None
        """Module directories shared by every environment, separated by
        the OS path separator."""
        codedir: _args._CodedirArg = None
        """Puppet's $codedir (Puppet's own AIO default per platform if
        omitted); consulted only by a version 3 hiera.yaml's default
        per-backend datadir."""
        strict: _args._StrictArg = None
        """Strictness for undefined variables: off, warning (default) or error."""

        render_as: _args._RenderAsArg = None
        """Output format: s, json or yaml (default yaml; s when explaining)."""

        debug: _args._DebugArg = False
        """Log debug messages (same as -vv)."""

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

            The traceback is attached only when explicitly asked for
            (-v, -d/--debug or DUHO_TRACEBACK=1); an
            unattended caller gets a single line, not a stack.
            """
            kw = {}
            if self.verbose > 0 or self.debug or _duho_logging.traceback_enabled():
                kw = {"exc_info": True}
            _LOGGER.error("%s", text, **kw)
            return 2

        def __call__(self) -> int:
            """Run the lookup duho parsed into this instance's fields and
            return the process exit code (see the class docstring).

            :returns: the process exit code.
            """
            keys = list(self.keys or ()) + list(self._passthrough_ or ())
            merge = _unplaceholder(self.merge)
            knock_out_prefix = _unplaceholder(self.knock_out_prefix)
            value_type = _unplaceholder(self.value_type)
            default = _unplaceholder(self.default)
            facts_path = _unplaceholder(self.facts)
            node = _unplaceholder(self.node)
            hiera_config = _unplaceholder(self.hiera_config)
            environment = _unplaceholder(self.environment)
            environmentpath = _unplaceholder(self.environmentpath)
            modulepath = _unplaceholder(self.modulepath)
            basemodulepath = _unplaceholder(self.basemodulepath)
            codedir = _unplaceholder(self.codedir)
            strict = _unplaceholder(self.strict)
            render_as = _unplaceholder(self.render_as)
            scope_items = _unplaceholder(self.scope)

            try:
                merge_options = _merge_options(
                    merge,
                    knock_out_prefix,
                    self.sort_merged_arrays,
                    self.merge_hash_arrays,
                )
            except _UsageError as e:
                return self._fail(str(e))

            explaining = self.explain or self.explain_options
            only_options = self.explain_options and not self.explain
            if not keys:
                if only_options:
                    keys = ["__global__"]
                else:
                    return self._fail("No keys were given to lookup.")

            fmt = (render_as or ("s" if explaining else "yaml")).lower()
            if Backend.find(fmt, kind="render") is None:
                return self._fail("Unknown rendering format '{}'".format(fmt))

            joined_keys = ", ".join(keys)

            try:
                scope = _build_scope(
                    _parse_scope(scope_items), facts_path, node, environment, strict
                )
            except (_UsageError, BackendError, TypeError, ValueError) as e:
                return self._fail(str(e))

            config = hiera_config
            extra = {}
            if config is not None:
                config = _os.path.abspath(config)
            else:
                default_path = _os.path.abspath("hiera.yaml")
                if _os.path.exists(default_path):
                    config = default_path
                else:
                    extra["base_path"] = _os.getcwd()

            lookup_kwargs = {}
            if default is not None:
                lookup_kwargs["default_value"] = default

            names = keys[0] if len(keys) == 1 else keys

            try:
                if value_type is not None:
                    parse_type(value_type)  # syntax error exits 2, even on a miss
                hiera = Hiera(
                    config,
                    scope=scope,
                    environmentpath=_paths(environmentpath),
                    basemodulepath=_paths(basemodulepath) or (),
                    modulepath=_paths(modulepath),
                    codedir=codedir,
                    **extra,
                )
                if explaining:
                    result = hiera.explain(
                        names,
                        value_type,
                        merge_options,
                        explain_options=only_options,
                        **lookup_kwargs,
                    )
                else:
                    value = hiera.lookup(
                        names, value_type, merge_options, **lookup_kwargs
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
                if explaining:
                    text = (
                        result.text()
                        if fmt == "s"
                        else Backend.new(fmt, kind="render").dumps(result.to_hash())
                    )
                else:
                    text = Backend.new(fmt, kind="render").dumps(value)
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
        other error, or the CLI extra is not installed.
    """
    # duho.main sets up stderr logging (honoring -v/-q/--loglevel) and
    # dispatches to Lookup.__call__, whose int return becomes the exit code.
    if duho is None:
        print(_NO_CLI_EXTRA_HINT, file=_sys.stderr)
        return 2
    argv = _puppet_argv(list(_sys.argv[1:] if argv is None else argv))
    return duho.main(Lookup, argv)


if __name__ == "__main__":
    _sys.exit(main())
