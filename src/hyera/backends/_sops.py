"""``sops_data``/``sops``: decrypted on the fly via the ``sops`` CLI (the
one kept non-Puppet deviation), plus the ``dotenv`` format its writer emits.

Original code: hardened for unattended use (finite timeout, captured
stderr, an absolute never-a-batch-shim executable, and path/format
handling specific to the ``sops`` CLI's own behaviour).
"""

import os
import re
import shutil
import subprocess
import typing as _ty

from pathlib_next import Path

from ..exceptions import BackendError, ConfigError
from . import Backend, NamePattern, _Names

#: How long (seconds) to wait for the ``sops`` subprocess before giving up.
#: Kept finite so an unattended lookup never hangs forever on a wedged sops.
SOPS_TIMEOUT = 30

__all__ = ["DotenvBackend", "SopsBackend", "SOPS_TIMEOUT"]


class DotenvBackend(Backend):
    """dotenv, in exactly the shape the ``sops`` CLI's writer emits it
    (``stores/dotenv/store.go``) -- reachable only through
    :class:`SopsBackend`.
    """

    NAMES: _ty.ClassVar[_Names] = {"format": ("dotenv",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".env",)

    def loads(self, text: str) -> _ty.Dict[str, str]:
        """Parse dotenv the way sops's own writer emits it: ``KEY=value``
        lines, ``#`` comments, blank lines skipped, ``\\n`` unescaped.

        :param text: the dotenv text to parse.
        :returns: the parsed key/value pairs.
        :raises BackendError: a non-blank, non-comment line has no ``=``.
        """
        result: _ty.Dict[str, str] = {}
        for lineno, line in enumerate(text.split("\n"), start=1):
            if line == "" or line.startswith("#"):
                continue
            if "=" not in line:
                raise BackendError("invalid dotenv line {}".format(lineno))
            key, _sep, value = line.partition("=")
            result[key] = value.replace("\\n", "\n")
        return result


def _refuse_batch_shim(exe: str) -> None:
    """Raise if *exe* is a ``.bat``/``.cmd`` shim, in any letter case."""
    if os.path.splitext(exe)[1].lower() in (".bat", ".cmd"):
        raise BackendError(
            "refusing to run sops batch shim {}: cmd.exe re-parses its own "
            "argument line, which is unsafe for a data-derived path".format(exe)
        )


def _run_sops(path, input_type: str, output_type: str = None) -> bytes:
    """Run ``sops -d`` on ``path`` and return its decrypted stdout.

    Hardened for unattended use: the resolved executable is run by its
    absolute path (never a bare name re-resolved by the child), a batch
    shim (``.bat``/``.cmd``) is refused outright (``cmd.exe`` re-parses its
    own argument line, which a data path containing shell metacharacters
    could abuse), and the data path is always passed absolute and after a
    literal ``--`` so a path/scope value starting with ``-`` can never be
    read as a sops option.

    ``output_type`` defaults to ``input_type`` (the rule for
    yaml/json/dotenv, keeping YAML on the Psych-compatible loader).
    :class:`SopsBackend` passes a different value only for ``ini``:
    sops's own INI *writer* is ambiguous, so INI is always decrypted as
    ``--output-type=json`` and parsed as JSON instead.
    """
    if output_type is None:
        output_type = input_type
    exe = shutil.which("sops")
    if exe is None:
        raise BackendError(
            "sops executable not found on PATH; cannot decrypt {}".format(path)
        )
    # The batch-shim refusal runs before the relative-path check (it holds
    # whatever the path looks like, and a Windows-style path is never
    # absolute on POSIX) and again after abspath, which normalizes forms
    # such as ``sops.bat.`` into ``.bat``.
    _refuse_batch_shim(exe)
    if not (os.path.isabs(exe) or exe.startswith(("/", "\\"))):
        # Python's ``shutil.which`` does not consistently honour the
        # Windows implicit-current-directory opt-out (NoDefaultCurrentDirectoryInExePath):
        # on 3.9 it can still return a path relative to the cwd (or a
        # relative PATH entry) even when the caller has opted out of that
        # behavior. Running whatever that happens to resolve to would be
        # exactly the implicit-cwd exposure the absolute-path handling here
        # is meant to close, so refuse it outright instead of silently
        # trusting a relative result. A path that is merely drive-less but
        # still rooted (``/usr/bin/sops``, e.g. a POSIX-style test double)
        # is not this exposure -- Windows resolves it against the current
        # drive's root, never against an attacker-influenced cwd -- so only
        # a path with no leading separator at all (genuinely relative) is
        # refused here.
        raise BackendError(
            "refusing to run sops resolved to a relative path {!r} (from "
            "the current directory or a relative PATH entry); put an "
            "absolute sops on PATH instead".format(exe)
        )
    exe = os.path.abspath(exe)
    _refuse_batch_shim(exe)
    abs_path = os.path.abspath(os.fspath(path))
    timed_out = False
    try:
        proc = subprocess.run(
            [
                exe,
                "--input-type={}".format(input_type),
                "--output-type={}".format(output_type),
                "-d",
                "--",
                abs_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SOPS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Recorded, not re-raised, inside the except (R4b): a
        # TimeoutExpired carries the subprocess's partial stdout (possibly
        # partially-decrypted plaintext) as an attribute, and raising
        # *inside* an active except block sets it as `__context__` even
        # under `from None` -- `from None` only sets
        # `__suppress_context__`, so the attribute stays reachable via
        # `__context__.stdout`/`.output` on the raised BackendError. The
        # BackendError is raised below instead, once this except block has
        # finished and Python has cleared the handled exception, so
        # `__context__` is genuinely `None`, not just suppressed.
        timed_out = True
    except OSError as e:
        raise BackendError("Failed to run sops on {}: {}".format(path, e)) from e

    if timed_out:
        raise BackendError(
            "sops timed out after {}s decrypting {}".format(SOPS_TIMEOUT, path)
        ) from None

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise BackendError(
            "sops failed (exit {}) decrypting {}: {}".format(
                proc.returncode, path, detail or "<no stderr>"
            )
        )
    return proc.stdout


#: sops's own ``FormatForPath`` rule (``cmd/sops/formats/formats.go``,
#: verified against the real v3.13.3 binary and source, 2026-09-29):
#: case-sensitive ``strings.HasSuffix``, checked in this order. Anything
#: else is binary to sops -- not a data hash.
_SOPS_SUFFIXES = (
    (".yaml", "yaml"),
    (".yml", "yaml"),
    (".json", "json"),
    (".env", "dotenv"),
    (".ini", "ini"),
)


def _sops_format(path_str: str):
    for suffix, fmt in _SOPS_SUFFIXES:
        if path_str.endswith(suffix):
            return fmt
    return None


#: S7: fixed prefixes of the two ``_psych`` messages that quote the
#: offending scalar verbatim (``invalid value for Float()/Integer():
#: "<data>"``). Matched as a plain prefix, never against the tail: the
#: quoted scalar can itself embed a literal newline (a ``!!float |\n
#: HUNTER2`` block scalar), which would otherwise make a naive
#: ``.*$``-style regex fail to match the whole message and leak it.
_SOPS_YAML_QUOTED_PREFIXES = (
    "invalid value for Float(): ",
    "invalid value for Integer(): ",
)

#: The third leaking shape's fixed prefix, ``Tried to load unspecified
#: class: <name>`` (``_psych._disallowed``); ``<name>`` is
#: attacker-controlled text for every ``!ruby/...`` tag except the fixed
#: names below.
_SOPS_YAML_CLASS_PREFIX = "Tried to load unspecified class: "

#: Names ``_psych`` itself raises unconditionally for a known YAML
#: shape (an implicit timestamp/date, `!!set`, a bare/nameless
#: ``!ruby/object``) -- never text lifted from the decrypted document, so
#: these stay visible. Everything else after "unspecified class: " comes
#: from the tag's own (attacker-controlled) suffix text.
_SOPS_YAML_CLASS_ALLOW = frozenset({"Time", "Date", "Object", "Psych::Set"})


def _sops_redact_yaml_problem(problem: str) -> str:
    """On the sops decrypt path only, blank a YAML loader message's
    quoted scalar or attacker-suppliable class name. Plain ``yaml_data``
    (no sops involved) keeps Puppet's full text -- :meth:`Backend.load`
    never calls this. A decrypted value shaped like ``!!float HUNTER2`` or
    ``!ruby/object:HUNTER2 {}`` would otherwise echo ``HUNTER2`` verbatim
    into the raised error, the log, and an MCP result (the CLI's optional
    MCP server serves this same error text over ``tools/call``).
    """
    for prefix in _SOPS_YAML_QUOTED_PREFIXES:
        if problem.startswith(prefix):
            return prefix + "<redacted>"
    if problem.startswith(_SOPS_YAML_CLASS_PREFIX):
        name = problem[len(_SOPS_YAML_CLASS_PREFIX) :]
        if name not in _SOPS_YAML_CLASS_ALLOW:
            return _SOPS_YAML_CLASS_PREFIX + "<redacted>"
    return problem


class SopsBackend(Backend):
    """Decrypted on the fly via the ``sops`` CLI (``sops_data``; the
    one kept non-Puppet deviation).

    Hardened for unattended use: the subprocess has a finite timeout, its
    stderr is captured and surfaced, a missing ``sops`` binary raises a
    clear :class:`BackendError` instead of an opaque ``FileNotFoundError``,
    and a decrypted file that fails to parse reports only the problem and
    its line/column -- never the decrypted plaintext.

    ``sops_data`` and ``sops`` infer the format from the file extension
    with sops's own rule (:data:`_SOPS_SUFFIXES`); the ``sops_<format>``
    name (a :class:`NamePattern`, added in a later commit alongside the
    ``sops`` alias) forces one regardless of extension via the ``format``
    constructor keyword.

    :param conf: the hierarchy entry's/``defaults``'s own mapping.
    :param strict: overrides the call-time default.
    :param format: forces the decrypted plaintext's format
        (``yaml``/``json``/``ini``/``dotenv``) regardless of extension.
    """

    NAMES: _ty.ClassVar[_Names] = {
        "function": (
            "sops_data",
            "sops",
            NamePattern(
                "sops_<yaml|json|ini|dotenv>",
                re.compile(r"sops_(?P<format>yaml|json|ini|dotenv)"),
            ),
        )
    }

    def __init__(
        self,
        conf: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        *,
        strict: _ty.Optional[str] = None,
        format: _ty.Optional[str] = None,
    ) -> None:
        super().__init__(conf, strict=strict)
        self._format = format

    def data_hash(
        self, path: "Path", options: _ty.Mapping[str, _ty.Any]
    ) -> _ty.Dict[str, _ty.Any]:
        """Decrypt ``path`` with the ``sops`` CLI and parse the plaintext
        in the format sops itself reports for it (or the ``format``
        constructor keyword, when given).

        :param path: the encrypted file's location.
        :param options: the hierarchy entry's ``options``.
        :returns: the decrypted, parsed data.
        :raises ConfigError: ``path`` has no recognized suffix and no
            ``format`` was given, or ``options`` carries anything besides
            ``path``.
        :raises BackendError: ``sops`` is missing, times out, exits
            non-zero, or the decrypted plaintext could not be parsed.
        """
        self._require_path_only(path, options)
        fmt = self._format or _sops_format(str(path))
        if fmt is None:
            raise ConfigError(
                "sops_data: '{}' has no .yaml/.yml/.json/.env/.ini suffix, "
                "so sops reads it as binary, which is not a data hash; use "
                "data_hash: sops_<yaml|json|ini|dotenv> to choose a format"
                "".format(path)
            )
        # sops's own INI *writer* is ambiguous -- a decrypted value
        # containing `"""` plus a newline can inject a key or replace a
        # whole other section, and no INI parser (including hyera's former
        # one) can tell those bytes apart from a genuine file. INI is
        # therefore always decrypted as sops's own JSON view instead
        # (confirmed against real sops 3.13.3: it is exactly
        # `{"DEFAULT": {...}, section: {...}}`) and parsed with
        # JSONBackend; yaml/json/dotenv keep output-type equal to
        # input-type.
        parse_fmt = "json" if fmt == "ini" else fmt
        raw = _run_sops(path, fmt, output_type=parse_fmt)
        format_backend = Backend.new(parse_fmt, kind="format", strict=self.strict)
        problem = None
        text = None
        try:
            text = raw.decode("utf-8")
            parsed = format_backend.loads(text)
        except UnicodeDecodeError as e:
            # Optional hardening: the byte offset only, never the
            # offending byte value or the surrounding text the stock
            # codec message quotes.
            problem = "invalid UTF-8 at byte offset {}".format(e.start)
        except BackendError as e:
            problem = _sops_redact_yaml_problem(str(e))
        else:
            return format_backend._as_data_hash(parsed, path)
        finally:
            # Optional hardening: drop the plaintext locals before the
            # raise below, so a frame-capturing error reporter (e.g.
            # Sentry's default) does not also collect them.
            del raw, text
        raise BackendError(
            "Unable to parse ({}): {}".format(path, problem), path=str(path)
        )
