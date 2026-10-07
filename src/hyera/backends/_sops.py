"""``sops_data``/``sops``: decrypted on the fly via the ``sops`` CLI (the
one kept non-Puppet deviation), plus the ``dotenv`` format its writer emits.

Original code: hardened for unattended use (finite timeout, captured
stderr, an absolute never-a-batch-shim executable, and path/format
handling specific to the ``sops`` CLI's own behaviour).
"""

from __future__ import annotations

import os
import re
import typing as _ty

from pathlib_next import Path

from ..exceptions import BackendError, ConfigError
from .._subprocess import run as _run
from ._base import Backend, NamePattern, _Names

__all__ = ["DotenvBackend", "SopsBackend"]


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


def _run_sops(
    path, input_type: str, output_type: _ty.Optional[str] = None, timeout=None
) -> bytes:
    """Run ``sops -d`` on ``path`` and return its decrypted stdout.

    The data path is always passed absolute and after a literal ``--`` so a
    path or scope value starting with ``-`` can never be read as a sops
    option. ``timeout`` defaults to ``hyera.backends.SOPS_TIMEOUT``, read at
    call time.

    ``output_type`` defaults to ``input_type`` (the rule for
    yaml/json/dotenv, keeping YAML on the Psych-compatible loader).
    :class:`SopsBackend` passes a different value only for ``ini``:
    sops's own INI *writer* is ambiguous, so INI is always decrypted as
    ``--output-type=json`` and parsed as JSON instead.
    """
    if output_type is None:
        output_type = input_type
    if timeout is None:
        from . import SOPS_TIMEOUT

        timeout = SOPS_TIMEOUT
    abs_path = os.path.abspath(os.fspath(path))
    try:
        return _run(
            "sops",
            [
                "--input-type={}".format(input_type),
                "--output-type={}".format(output_type),
                "-d",
                "--",
                abs_path,
            ],
            timeout=timeout,
            refuse_batch=True,
            context="decrypting {}".format(path),
        )
    except BackendError as e:
        e.path = str(path)
        raise


# : sops's ``FormatForPath`` rule (``cmd/sops/formats/formats.go``, v3.13.3):
# case-sensitive : ``strings.HasSuffix``, in this order. Anything else is binary to
# sops, not a data hash.
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


#: Prefixes of the two ``_psych`` messages that quote the scalar verbatim (``invalid
#: value for Float()/Integer(): "<data>"``), matched as a plain prefix, never the tail:
#: the scalar can embed a newline, which defeats a ``.*$`` regex and leaks it.
_SOPS_YAML_QUOTED_PREFIXES = (
    "invalid value for Float(): ",
    "invalid value for Integer(): ",
)

# : The third leaking shape's fixed prefix, ``Tried to load unspecified class: <name>``
# : (``_psych._disallowed``); ``<name>`` is attacker-controlled for every ``!ruby/...``
# tag except the names below.
_SOPS_YAML_CLASS_PREFIX = "Tried to load unspecified class: "

#: Names ``_psych`` raises unconditionally for a known YAML shape (implicit
#: timestamp/date, `!!set`, nameless ``!ruby/object``): never decrypted text, so they
#: stay visible. Anything after "unspecified class: " is the attacker-controlled tag.
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

    Hardened for unattended use: the subprocess has a finite timeout and
    its whole process group is killed when it expires (a
    :class:`BackendTimeoutError`), its stdin is the null device, its
    stderr is captured and surfaced (the last 2,000 characters), a missing
    ``sops`` binary raises a clear :class:`BackendError`,
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
    :param timeout: seconds to wait for ``sops``; ``None`` reads
        ``hyera.backends.SOPS_TIMEOUT`` at each call.
    :raises ConfigError: ``timeout`` is not a positive number.
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
        timeout: _ty.Optional[float] = None,
    ) -> None:
        super().__init__(conf, strict=strict)
        self._format = format
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not timeout > 0
        ):
            raise ConfigError(
                "sops timeout must be a positive number of seconds, not "
                "{!r}".format(timeout)
            )
        self._timeout = timeout

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
        # sops's INI *writer* is ambiguous: a decrypted value with `"""` and a newline
        # can inject a key or section undetectably. INI is decrypted as sops's JSON view
        # (`{"DEFAULT": {...}, section: {...}}`) and parsed with JSONBackend.
        parse_fmt = "json" if fmt == "ini" else fmt
        raw = _run_sops(path, fmt, output_type=parse_fmt, timeout=self._timeout)
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
