"""Command-line interface for hyera, built on duho.

Accepts puppet lookup's own flag set (see README.md "Command line"):

hyera KEY --hiera_config hiera.yaml --facts facts.yaml --node N

Designed for unattended use: no interactive prompts, deterministic output,
and meaningful exit codes: 0 found (or --default printed), 1
key not found, 2 any other error (one stderr line; -v or
DUHO_TRACEBACK=1 adds the traceback). Output is rendered the way
puppet lookup --render-as s|json|yaml does (hyera._render),
written as UTF-8 bytes with LF line endings whatever the console/locale
encoding.
"""

import io as _io
import logging as _logging
import os as _os
import socket as _socket
import sys as _sys
import typing as _ty

try:
    import duho
    import duho.logging as _duho_logging
except ModuleNotFoundError as _e:
    if _e.name != "duho":
        raise
    duho = None

from . import __version__
from .exceptions import BackendError, HieraError, KeyNotFoundError, _one_line
from .core import Hiera
from ._facts import load_facts
from ._scope import Scope
from ._type_parser import parse_type
from .backends import Backend

_LOGGER = _logging.getLogger(__name__)

#: The four merge strategy names puppet lookup --merge accepts; there
#: are no array/set aliases.
_MERGE_STRATEGIES = ("first", "unique", "hash", "deep")

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

#: Printed (to stderr) when the cli extra (duho) is not installed.
# Double-quoted, not single-quoted: pip install 'hyera[cli]' fails when
# pasted into cmd.exe, where single quotes are literal (pip then sees the
# argv "'hyera[cli]'" verbatim and rejects it); double quotes work in
# cmd.exe, PowerShell and POSIX shells alike.
_NO_CLI_EXTRA_HINT = (
    "hyera: the command-line interface needs the cli extra: " 'pip install "hyera[cli]"'
)

#: The puppet lookup release this CLI's flags and $server_facts
#: mirror. Not read from anywhere else -- there is no local Puppet to ask.
_PUPPET_VERSION = "8.10.0"


class _UsageError(ValueError):
    """Puppet's Could not run: ... text: reported bare, no key prefix."""


class _ScopeError(_UsageError):
    """A --scope entry was not in key=value form, or its name is invalid."""

    def __init__(self, item: str, reason: str = "expected key=value"):
        super().__init__("invalid --scope {!r} ({})".format(item, reason))


def _parse_scope_value(text: str):
    """A --scope value is YAML; an empty value is None (not the
    yaml_data loader's own empty-document False)."""
    if text == "":
        return None
    return Backend.new("yaml", kind="format").loads(text)


def _parse_scope(items: "_ty.Iterable[str]") -> dict:
    """Parse NAME=VALUE scope entries into a node-parameters dict.

    NAME may be dotted (os.family=RedHat) to build a nested hash;
    later items win, and a segment landing under an already-scalar value
    raises. An empty NAME, or an item with no = at all, raises
    _ScopeError.
    """
    result: dict = {}
    for item in items or ():
        if not item:
            continue
        if "=" not in item:
            raise _ScopeError(item)
        name, _eq, value = item.partition("=")
        if not name:
            raise _ScopeError(item)
        parsed = _parse_scope_value(value)
        segments = name.split(".")
        target = result
        prefix: list = []
        for seg in segments[:-1]:
            prefix.append(seg)
            existing = target.get(seg)
            if existing is None:
                existing = {}
                target[seg] = existing
            elif not isinstance(existing, dict):
                raise _ScopeError(item, "{!r} is not a hash".format(".".join(prefix)))
            target = existing
        target[segments[-1]] = parsed
    return result


def _paths(value):
    """Split a Puppet path-list setting (os.pathsep-joined) into
    absolute paths; None stays None (the flag was not given)."""
    if value is None:
        return None
    return [_os.path.abspath(p) for p in value.split(_os.pathsep) if p]


def _describe(e) -> str:
    """One-line description of an exception for the CLI's failure log."""
    if isinstance(e, (HieraError, OSError)):
        return _one_line(e)
    return "{}: {}".format(type(e).__name__, _one_line(e))


def _emit(text: str) -> None:
    """Write text to stdout the way Ruby's puts does: a trailing
    newline is appended only if text does not already end with one.

    Always UTF-8 bytes with LF line endings, regardless of the console or
    locale encoding: written to sys.stdout.buffer when one exists (a
    real console or pipe), else (a StringIO under
    contextlib.redirect_stdout, as the conformance harness uses)
    sys.stdout.write directly. Never sys.stdout.reconfigure --
    that would change the caller's own stream when main() runs
    in-process.
    """
    if not text.endswith("\n"):
        text += "\n"
    buffer = getattr(_sys.stdout, "buffer", None)
    if buffer is not None:
        _sys.stdout.flush()
        buffer.write(text.encode("utf-8"))
        buffer.flush()
    else:
        _sys.stdout.write(text)
        _sys.stdout.flush()


def _silence_stdout() -> None:
    """Redirect the stdout file descriptor to the null device.

    Called after a BrokenPipeError: the reader is already gone,
    so nothing further should try to write to (or complain about) the
    broken pipe, including whatever the interpreter does with stdout at
    exit. Best-effort: a stream with no real file descriptor (a
    StringIO) just leaves this a no-op.
    """
    try:
        _os.dup2(_os.open(_os.devnull, _os.O_WRONLY), _sys.stdout.fileno())
    except (OSError, ValueError, _io.UnsupportedOperation):
        pass


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


if duho is not None:

    class Lookup(duho.LoggingArgs, duho.Cli):
        """Look up keys in Hiera data the way puppet lookup does.

        Exit status: 0 found (or --default printed), 1 no key found, 2
        any other error.
        """

        _version_ = __version__
        _mcp_ = True
        _parsername_ = "hyera"
        _logger_name_ = "hyera"

        keys: "duho.Arg[_ty.List[str], duho.NS(flags=['keys'], metavar='KEY')]" = None
        """Keys to look up; the first one found wins."""
        merge: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--merge'])]" = None
        """Merge strategy: first, unique, hash or deep. Overrides the
        data's lookup_options; omitted, lookup_options decide, else the
        first value found wins."""
        knock_out_prefix: (
            "duho.Arg[_ty.Optional[str], duho.NS(flags=['--knock-out-prefix'])]"
        ) = None
        """With --merge deep: a prefix that marks a value or key for removal."""
        sort_merged_arrays: (
            "duho.Arg[bool, duho.NS(flags=['--sort-merged-arrays'])]"
        ) = False
        """With --merge deep: sort merged arrays."""
        merge_hash_arrays: "duho.Arg[bool, duho.NS(flags=['--merge-hash-arrays'])]" = (
            False
        )
        """With --merge deep: deep-merge hashes inside arrays by position."""
        value_type: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--type'])]" = None
        """Assert the value (and --default) has this Puppet type, e.g. Array[String]."""
        default: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--default'])]" = None
        """String printed when no key is found."""
        explain: "duho.Arg[bool, duho.NS(flags=['--explain'])]" = False
        """Show how the value was found, instead of only its value."""
        explain_options: "duho.Arg[bool, duho.NS(flags=['--explain-options'])]" = False
        """Show only how lookup_options was assembled."""

        facts: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--facts'])]" = None
        """Facts file: .json, .yaml or .yml; any other name is read as JSON, then YAML."""
        node: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--node'])]" = None
        """Node name used in messages; sets no fact."""
        scope: (
            "duho.Arg[_ty.List[str], duho.NS(flags=['--scope', '-s']), duho.Append()]"
        ) = None
        """Node parameter (top-scope variable); VALUE is YAML and a
        dotted NAME builds a hash."""

        hiera_config: (
            "duho.Arg[_ty.Optional[str], duho.NS(flags=['--hiera_config'])]"
        ) = None
        """Path to the base hiera.yaml (default: ./hiera.yaml if it
        exists, else Puppet's built-in default configuration)."""
        environment: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--environment'])]" = (
            None
        )
        """Environment name."""
        environmentpath: (
            "duho.Arg[_ty.Optional[str], duho.NS(flags=['--environmentpath'])]"
        ) = None
        """Environment directories, separated by the OS path separator."""
        modulepath: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--modulepath'])]" = (
            None
        )
        """Module directories for the current environment, separated by
        the OS path separator (replaces the default modulepath)."""
        basemodulepath: (
            "duho.Arg[_ty.Optional[str], duho.NS(flags=['--basemodulepath'])]"
        ) = None
        """Module directories shared by every environment, separated by
        the OS path separator."""
        codedir: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--codedir'])]" = None
        """Puppet's $codedir (Puppet's own AIO default per platform if
        omitted); consulted only by a version 3 hiera.yaml's default
        per-backend datadir."""
        strict: (
            "duho.Arg[_ty.Optional[str], duho.Choice('off', 'warning', 'error'), "
            "duho.NS(flags=['--strict'])]"
        ) = None
        """Strictness for undefined variables: off, warning (default) or error."""

        render_as: "duho.Arg[_ty.Optional[str], duho.NS(flags=['--render-as'], metavar='FORMAT')]" = (None)
        """Output format: s, json or yaml (default yaml; s when explaining)."""

        debug: "duho.Arg[bool, duho.NS(flags=['--debug', '-d'])]" = False
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

            if (
                knock_out_prefix is not None
                or self.sort_merged_arrays
                or self.merge_hash_arrays
            ) and merge != "deep":
                return self._fail(
                    "The options --knock-out-prefix, --sort-merged-arrays, "
                    "and --merge-hash-arrays are only available with "
                    "'--merge deep'"
                )

            if merge is not None and merge not in _MERGE_STRATEGIES:
                return self._fail(
                    "The --merge option only accepts 'first', 'hash', "
                    "'unique', or 'deep'"
                )

            if merge == "deep":
                merge_options = {
                    "strategy": "deep",
                    "sort_merged_arrays": bool(self.sort_merged_arrays),
                    "merge_hash_arrays": bool(self.merge_hash_arrays),
                }
                if knock_out_prefix is not None:
                    merge_options["knockout_prefix"] = knock_out_prefix
            elif merge is not None:
                merge_options = {"strategy": merge}
            else:
                merge_options = None

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
                variables = _parse_scope(self.scope)
                facts = load_facts(facts_path) if facts_path is not None else {}
                if not facts:
                    # socket.getfqdn() is a live reverse-DNS lookup and can
                    # be slow (measured: 60s+ hangs on macOS CI runners) --
                    # compute it only for this message, never on the
                    # (overwhelmingly common) path where facts are present.
                    node_name = node or _socket.getfqdn().lower()
                    raise _UsageError(
                        "No facts available for target node: {}".format(node_name)
                    )
                scope = Scope(
                    variables=variables,
                    facts=facts,
                    server_facts={"serverversion": _PUPPET_VERSION},
                    environment=environment,
                    strict=strict or "warning",
                    node_name=node,
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
                _emit(text)
            except BrokenPipeError:
                _silence_stdout()
                return 2
            except Exception as e:
                return self._fail(
                    "Cannot render the value of key '{}': {}".format(
                        joined_keys, _describe(e)
                    )
                )
            return 0


def main(argv=None) -> int:
    # duho.main sets up stderr logging (honoring -v/-q/--loglevel) and
    # dispatches to Lookup.__call__, whose int return becomes the exit code.
    if duho is None:
        print(_NO_CLI_EXTRA_HINT, file=_sys.stderr)
        return 2
    argv = _puppet_argv(list(_sys.argv[1:] if argv is None else argv))
    return duho.main(Lookup, argv)


if __name__ == "__main__":
    _sys.exit(main())
