# Derived from phiera/backends.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Data backends: load a hiera data file (YAML, JSON, sops-encrypted YAML)."""

import json
import os
import re
import shutil
import subprocess

import yaml

from .exceptions import BackendError
from .util import LookupDict

__all__ = [
    "Backend",
    "YAMLBackend",
    "SopsYAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "BackendError",
    "has_hocon",
]

#: How long (seconds) to wait for the ``sops`` subprocess before giving up.
#: Kept finite so an unattended lookup never hangs forever on a wedged sops.
SOPS_TIMEOUT = 30


class Backend(object):
    """Backends load data from files. Subclasses override ``.load``.

    A backend registers itself under one or more ``NAMES`` (the Hiera 5
    ``data_hash`` value used in a hierarchy level, e.g. ``yaml_data``).
    """

    #: data_hash name(s) this backend answers to.
    NAMES: "tuple[str, ...]" = ()

    def __init__(self, conf: dict = None):
        self.conf = conf or {}
        # Accept either ``datadir`` or Hiera-5's ``data_dir`` spelling; the
        # loader normalizes to one of these. Missing/None -> "" (relative).
        datadir = self.conf.get("datadir")
        if datadir is None:
            datadir = self.conf.get("data_dir")
        self.datadir: str = datadir or ""

    def read_file(self, path) -> bytes:
        return path.read_bytes()

    def load(self, data: bytes):
        raise NotImplementedError("Subclasses must implement .load")


class YAMLBackend(Backend):
    NAMES = ("yaml_data", "yaml")

    def load(self, data):
        return self.load_ordered(data)

    @staticmethod
    def load_ordered(stream, Loader=yaml.SafeLoader, object_pairs_hook=LookupDict):
        """Parse YAML, materializing mappings as :class:`LookupDict`.

        Uses ``SafeLoader`` by default: hiera data is untrusted config, and
        the full ``Loader`` can construct arbitrary Python objects.
        """

        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))

        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        try:
            return yaml.load(stream, OrderedLoader)
        except yaml.YAMLError as e:
            raise BackendError("Failed to parse YAML: {}".format(e)) from e


def _run_sops(path, input_type: str) -> bytes:
    """Run ``sops -d`` on ``path`` and return its decrypted stdout.

    Hardened for unattended use: the resolved executable is run by its
    absolute path (never a bare name re-resolved by the child), a batch
    shim (``.bat``/``.cmd``) is refused outright (``cmd.exe`` re-parses its
    own argument line, which a data path containing shell metacharacters
    could abuse), and the data path is always passed absolute and after a
    literal ``--`` so a path/scope value starting with ``-`` can never be
    read as a sops option.
    """
    exe = shutil.which("sops")
    if exe is None:
        raise BackendError(
            "sops executable not found on PATH; cannot decrypt {}".format(path)
        )
    exe = os.path.abspath(exe)
    if os.path.splitext(exe)[1].lower() in (".bat", ".cmd"):
        raise BackendError(
            "refusing to run sops batch shim {}: cmd.exe re-parses its own "
            "argument line, which is unsafe for a data-derived path".format(exe)
        )
    abs_path = os.path.abspath(os.fspath(path))
    try:
        proc = subprocess.run(
            [
                exe,
                "--input-type={}".format(input_type),
                "--output-type={}".format(input_type),
                "-d",
                "--",
                abs_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SOPS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise BackendError(
            "sops timed out after {}s decrypting {}".format(SOPS_TIMEOUT, path)
        ) from e
    except OSError as e:
        raise BackendError("Failed to run sops on {}: {}".format(path, e)) from e

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise BackendError(
            "sops failed (exit {}) decrypting {}: {}".format(
                proc.returncode, path, detail or "<no stderr>"
            )
        )
    return proc.stdout


def _yaml_problem(exc) -> str:
    """Summarize a YAML parse error with no plaintext: never the decrypted
    data, a source snippet (``mark.get_snippet()``), or ``str(exc)`` itself
    -- only a short reason and a 1-based line/column when available.

    PyYAML's ``context`` (e.g. "while scanning a quoted scalar") together
    with ``context_mark`` pinpoints where the broken construct *starts*,
    which is more useful than ``problem``/``problem_mark`` (often just
    "found unexpected end of stream" at EOF); prefer it when present.
    """
    if isinstance(exc, yaml.MarkedYAMLError):
        if exc.context is not None and exc.context_mark is not None:
            text, mark = exc.context, exc.context_mark
        elif exc.problem is not None and exc.problem_mark is not None:
            text, mark = exc.problem, exc.problem_mark
        else:
            text = mark = None
        if mark is not None:
            return "{} (line {}, column {})".format(
                text, mark.line + 1, mark.column + 1
            )
    return type(exc).__name__


class SopsYAMLBackend(YAMLBackend):
    """YAML decrypted on the fly via the ``sops`` CLI.

    Hardened for unattended use: the subprocess has a finite timeout, its
    stderr is captured and surfaced, a missing ``sops`` binary raises a
    clear :class:`BackendError` instead of an opaque ``FileNotFoundError``,
    and a decrypted file that fails to parse reports only the problem and
    its line/column -- never the decrypted plaintext.
    """

    NAMES = ("yaml.enc", "sops")

    def read_file(self, path) -> bytes:
        return _run_sops(path, "yaml")

    def load(self, data):
        try:
            return self.load_ordered(data)
        except BackendError as e:
            reason = _yaml_problem(e.__cause__)
        raise BackendError("sops-decrypted YAML does not parse: " + reason)


class JSONBackend(Backend):
    NAMES = ("json_data", "json")

    def load(self, data):
        try:
            return json.loads(data, object_pairs_hook=LookupDict)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BackendError("Failed to parse JSON: {}".format(e)) from e


_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def _find_hocon_string_end(text: str, start: int) -> "tuple[int, str]":
    """``text[start]`` is a double quote. Return ``(end, content)`` for a
    plain or triple-quoted HOCON string: ``end`` is the index just past the
    closing quote(s), ``content`` is the (still escaped) text between them.
    """
    n = len(text)
    if text.startswith('"""', start):
        content_start = start + 3
        close = text.find('"""', content_start)
        if close == -1:
            return n, text[content_start:n]
        return close + 3, text[content_start:close]
    i = start + 1
    while i < n and text[i] != '"':
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        i += 1
    content = text[start + 1 : i]
    return (i + 1 if i < n else n), content


def _skip_hocon_blanks(text: str, i: int) -> int:
    """Advance past spaces/tabs only (not newlines) from ``i``."""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    return i


def _hocon_directive_follows(text: str, i: int) -> bool:
    """True if, starting at ``i``, the text looks like an attempted include
    argument -- a quoted string, or an identifier possibly followed by
    spaces/tabs and then ``(`` -- valid or not.
    """
    n = len(text)
    j = _skip_hocon_blanks(text, i)
    if j < n and text[j] == '"':
        return True
    if j < n and text[j].isalpha():
        k = j
        while k < n and (text[k].isalnum() or text[k] == "_"):
            k += 1
        k = _skip_hocon_blanks(text, k)
        return k < n and text[k] == "("
    return False


def _strip_hocon_includes(text: str) -> str:
    """Blank the plain HOCON ``include "..."`` directives Puppet ignores,
    and raise :class:`BackendError` for every other include form --
    ``include file(...)``, ``url(...)``, ``classpath(...)``,
    ``required(...)``, ``package(...)``, any other ``name(...)``, a
    directive in value position, a case-mismatched keyword, or a bare
    ``include`` with nothing valid after it -- before pyhocon ever parses
    the text. This keeps pyhocon's own include machinery (which reads
    files off the process cwd and fetches ``http(s)``/``file`` URLs) from
    ever running. See ``AGENTS.md`` for the exact rule and its two
    deliberate divergences from Puppet (``file()``, value position).

    Blanked spans replace every non-newline character with a space, so
    line/column numbers in any later pyhocon parse error still line up
    with the original file.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    while i < n:
        c = text[i]
        if c == '"':
            end, _content = _find_hocon_string_end(text, i)
            i = end
            last_sig = '"'
            continue
        if c == "#" or (c == "/" and i + 1 < n and text[i + 1] == "/"):
            j = text.find("\n", i)
            i = j if j != -1 else n
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}", i + 2)
            i = (j + 1) if j != -1 else n
            last_sig = "}"
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\n":
            last_sig = "\n"
            i += 1
            continue
        if c.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.lower() != "include":
                last_sig = word[-1]
                i = j
                continue

            key_position = last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + 1

            if key_position and word == "include":
                after = _skip_hocon_blanks(text, j)
                if after < n and text[after] == '"':
                    end, content = _find_hocon_string_end(text, after)
                    scheme = _URL_SCHEME_RE.match(content)
                    if scheme and scheme.group(0)[:-3].lower() != "file":
                        raise BackendError(
                            "HOCON include of a non-file URL is not "
                            "supported (line {})".format(line)
                        )
                    for k in range(i, end):
                        if out[k] != "\n":
                            out[k] = " "
                    i = end
                    last_sig = " "
                    continue
                raise BackendError(
                    "HOCON include is only supported as a plain quoted "
                    "string; file()/url()/classpath()/required()/package() "
                    "and similar forms are not supported (line {})".format(line)
                )

            if _hocon_directive_follows(text, j):
                raise BackendError(
                    "HOCON include directive is not supported here "
                    "(line {})".format(line)
                )

            last_sig = word[-1]
            i = j
            continue

        last_sig = c
        i += 1

    return "".join(out)


class HOCONBackend(Backend):
    """HOCON (``.conf``) data via the optional ``pyhocon`` package.

    ``include`` directives are sanitized before pyhocon ever sees the text
    (see :func:`_strip_hocon_includes`): pyhocon's own include machinery
    (file reads relative to the process cwd, ``http(s)``/``file`` URL
    fetches) never runs.
    """

    NAMES = ("hocon_data", "hocon")

    def load(self, data):
        try:
            from pyhocon import ConfigFactory
        except ImportError as e:  # pragma: no cover - optional dependency
            raise BackendError(
                "hocon_data backend requires the 'pyhocon' package "
                "(pip install pyera[hocon])"
            ) from e
        try:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BackendError("Failed to parse HOCON: {}".format(e)) from e
        text = _strip_hocon_includes(data)
        try:
            parsed = ConfigFactory.parse_string(text)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError("Failed to parse HOCON: {}".format(e)) from e
        return _as_lookupdict(parsed)


def _as_lookupdict(obj):
    """Recursively convert a parsed mapping into :class:`LookupDict`."""
    if isinstance(obj, dict):
        return LookupDict((k, _as_lookupdict(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_as_lookupdict(v) for v in obj]
    return obj


def has_hocon() -> bool:
    """True if the optional ``pyhocon`` dependency is importable."""
    try:
        import pyhocon  # noqa: F401

        return True
    except ImportError:
        return False
