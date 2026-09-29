# Derived from phiera/backends.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Data backends: load a hiera data file (YAML, JSON, sops-encrypted YAML)."""

import contextvars
import json
import logging
import os
import re
import shutil
import subprocess

import yaml

from .exceptions import BackendError, _one_line
from .util import LookupDict

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "Backend",
    "YAMLBackend",
    "SopsYAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "BackendError",
    "has_hocon",
    "default_backends",
]

#: How long (seconds) to wait for the ``sops`` subprocess before giving up.
#: Kept finite so an unattended lookup never hangs forever on a wedged sops.
SOPS_TIMEOUT = 30


class Backend:
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
        problem = None
        try:
            return yaml.load(stream, OrderedLoader)
        except yaml.YAMLError as e:
            problem = _yaml_problem(e)
        # Raised *outside* the except block on purpose: chaining "from e"
        # (or even a bare re-raise inside the handler) would leave
        # __cause__/__context__ holding PyYAML's own exception -- which
        # embeds a source snippet -- reachable from a caller that walks the
        # chain. `problem` alone carries no source text (see _yaml_problem).
        raise BackendError(problem)


def _refuse_batch_shim(exe: str) -> None:
    """Raise if *exe* is a ``.bat``/``.cmd`` shim, in any letter case."""
    if os.path.splitext(exe)[1].lower() in (".bat", ".cmd"):
        raise BackendError(
            "refusing to run sops batch shim {}: cmd.exe re-parses its own "
            "argument line, which is unsafe for a data-derived path".format(exe)
        )


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
        # `from None`, not `from e`: a TimeoutExpired carries the
        # subprocess's partial stdout (possibly partially-decrypted
        # plaintext) as an attribute, which chaining would keep reachable
        # via `__cause__.stdout` on the raised BackendError.
        raise BackendError(
            "sops timed out after {}s decrypting {}".format(SOPS_TIMEOUT, path)
        ) from None
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


#: Matches a PyYAML error's own quoted token, e.g. the alias/tag/anchor name
#: in "found undefined alias 'NAME'" or "found duplicate anchor 'NAME'".
#: ``_yaml_problem`` never echoes decrypted data, but three ``problem``/
#: ``context`` texts (undefined alias, unknown tag, duplicate anchor) quote a
#: single scalar from the source verbatim -- redact it rather than trusting
#: PyYAML's own message templates to never do this.
_YAML_QUOTED_TOKEN_RE = re.compile(r"'[^']*'")


def _yaml_problem(exc) -> str:
    """Summarize a YAML parse error with no plaintext, in Psych's shape:
    ``<problem> <context> at line L column C``. Never the decrypted data, a
    source snippet (``mark.get_snippet()``), ``str(exc)`` itself, or a
    quoted token embedded in the reason text.

    ``problem``/``context`` are PyYAML's own fixed phrases (tokens and tags
    at most, per Ruby Psych's ``[problem, context].compact.join(' ')``) --
    joining both, when present, matches Puppet's own message text. Position
    is the context mark when present, else the problem mark, both 1-based.
    """
    if isinstance(exc, yaml.MarkedYAMLError):
        parts = [p for p in (exc.problem, exc.context) if p]
        text = " ".join(parts)
        mark = exc.context_mark or exc.problem_mark
        if text:
            text = _YAML_QUOTED_TOKEN_RE.sub("'<redacted>'", text)
        if mark is not None:
            return "{} at line {} column {}".format(
                text, mark.line + 1, mark.column + 1
            ).strip()
        if text:
            return text
    elif isinstance(exc, yaml.reader.ReaderError):
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        return "{} at position {}".format(first_line, exc.position)
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


class JSONBackend(Backend):
    NAMES = ("json_data", "json")

    def load(self, data):
        try:
            return json.loads(data, object_pairs_hook=LookupDict)
        except json.JSONDecodeError as e:
            raise BackendError(
                "{} at line {} column {}".format(e.msg, e.lineno, e.colno)
            ) from e
        except UnicodeDecodeError as e:
            raise BackendError(str(e)) from e


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
        # HOCON's own triple-quoted string ends at the LAST quote of a
        # run of 3+ consecutive quotes, not the first matching triple --
        # ``"""x""""`` (4 trailing quotes) is the 1-character string "x"
        # followed by a stray closing quote that pyhocon folds into the
        # same terminator, not "x" followed by a bare `"` that starts a
        # new string. Extending over every extra trailing quote keeps our
        # notion of "end of string" in sync with pyhocon's, so scanning
        # resumes at the same place pyhocon would.
        end = close + 3
        while end < n and text[end] == '"':
            end += 1
        return end, text[content_start:close]
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


def _preceded_by_odd_backslashes(text: str, i: int) -> bool:
    """True if an odd number of consecutive ``\\`` immediately precede
    ``text[i]`` -- i.e. ``text[i]`` is itself escaped (``\\"``: escaped,
    ``\\\\"``: not, the first backslash is what's escaped). Mirrors
    pyhocon's own unquoted-value regex (``(?:[^...]|\\.)+``), which accepts
    a backslash-escaped ``"``, ``#``/``//`` or ``${`` as an ordinary
    character rather than the start of a string/comment/substitution.
    """
    count = 0
    j = i - 1
    while j >= 0 and text[j] == "\\":
        count += 1
        j -= 1
    return count % 2 == 1


def _strip_hocon_includes(text: str) -> str:
    """Blank the plain HOCON ``include "..."`` directives Puppet ignores,
    and raise :class:`BackendError` for every other include form --
    ``include file(...)``, ``url(...)``, ``classpath(...)``,
    ``required(...)``, ``package(...)``, any other ``name(...)``, a
    directive in value position (including inside a ``[...]`` array), a
    case-mismatched or code-point-mismatched keyword (pyhocon's own
    ``Keyword("include", caseless=True)`` matches by ``.upper()``, which
    also folds a dotless-i ``ınclude`` to ``INCLUDE``), or a bare
    ``include`` with nothing valid after it -- before pyhocon ever parses
    the text. This keeps pyhocon's own include machinery (which reads
    files off the process cwd and fetches ``http(s)``/``file`` URLs) from
    ever running. See ``AGENTS.md`` for the exact rule and its two
    deliberate divergences from Puppet (``file()``, value position).

    A :func:`_install_hocon_include_guard`-installed backstop still applies
    even if this scanner has a gap: pyhocon's own include-resolution
    entry points raise unconditionally for the duration of
    :meth:`HOCONBackend.load`, so a missed directive fails closed instead
    of silently reading a file or reaching the network.

    Blanked spans replace every non-newline character with a space, so
    line/column numbers in any later pyhocon parse error still line up
    with the original file.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    brackets = []  # stack of open '{'/'[' seen so far
    while i < n:
        c = text[i]
        if c in ('"', "#", "$") and _preceded_by_odd_backslashes(text, i):
            # An escaped quote/hash/substitution-start in unquoted text
            # (``x\"``, ``x\#``, ``x\${``) is an ordinary character to
            # pyhocon, not the start of a string, comment or substitution.
            # (``"``, ``#`` and ``$`` are all excluded from pyhocon's
            # unquoted-value character class, so unescaped they are always
            # significant, unlike a lone ``/`` below.)
            last_sig = c
            i += 1
            continue
        if c == '"':
            end, _content = _find_hocon_string_end(text, i)
            i = end
            last_sig = '"'
            continue
        if c == "#":
            j = i
            while j < n and text[j] not in "\r\n":
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            # Unlike ``#``, a lone ``/`` is NOT excluded from pyhocon's
            # unquoted-value character class, so ``//`` only starts a
            # comment at a token boundary (start of text, whitespace, or a
            # structural character) -- ``http://h`` and ``x//y`` are
            # ordinary unquoted text, since the value token already
            # in progress simply continues through the slashes and pyhocon
            # never gets a chance to try matching a comment there. An
            # escaped ``\//`` (odd backslashes) is never a comment either.
            # NOTE: ``:`` is deliberately not a boundary character here --
            # unlike every other separator below, a lone ``:`` is NOT
            # excluded from pyhocon's unquoted-value character class, so it
            # can appear literally inside a continuous token (``http://h``)
            # as well as as a key/value separator; treating it as always a
            # boundary would make ``//`` in ``http://h`` a comment again.
            prev = text[i - 1] if i > 0 else None
            at_boundary = prev is None or prev in ' \t\r\n{}[],="'
            if at_boundary and not _preceded_by_odd_backslashes(text, i):
                j = i
                while j < n and text[j] not in "\r\n":
                    j += 1
                i = j
                continue
            last_sig = c
            i += 1
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "{":
            j = text.find("}", i + 2)
            i = (j + 1) if j != -1 else n
            last_sig = "}"
            continue
        if c in " \t":
            i += 1
            continue
        if c == "\n" or c == "\r":
            # HOCON's own line-ending token is any run of ``\n``/``\r``
            # (pyhocon: ``eol = Word('\n\r')``) -- a lone ``\r`` (no ``\n``)
            # ends a ``#``/``//`` comment and starts a new key-position
            # line exactly as a real newline would.
            last_sig = "\n"
            i += 1
            continue
        if c in "{[":
            brackets.append(c)
            last_sig = c
            i += 1
            continue
        if c in "}]":
            if brackets:
                brackets.pop()
            last_sig = c
            i += 1
            continue
        if c.isalpha():
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word.upper() != "INCLUDE":
                last_sig = word[-1]
                i = j
                continue

            # Inside a `[...]` array, every position is a value, never a
            # key -- Puppet keeps a value-position include as literal text,
            # and pyera (which always raises for value position) must not
            # blank it away into an empty/short array instead.
            in_array = bool(brackets) and brackets[-1] == "["
            key_position = (not in_array) and last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + text.count("\r", 0, i) + 1
            # A lone `\r` and a `\n` from the same CRLF pair would both be
            # counted above; CRLF is normalized to a single logical
            # newline everywhere else in this scanner, so undo the double
            # count for every CRLF pair before this position.
            line -= text.count("\r\n", 0, i)

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
                        if out[k] not in ("\n", "\r"):
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


#: Set (only for the duration of a `HOCONBackend.load` call) so the
#: `_install_hocon_include_guard`-wrapped pyhocon entry points raise instead
#: of running. Context-local (per thread/task), so a concurrent `load()` on
#: another thread and every other pyhocon caller in the process, at any
#: point in time, are unaffected -- only the call(s) that set it see it fire.
_HOCON_INCLUDE_GUARD: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "_hocon_include_guard", default=False
)

_HOCON_GUARD_INSTALLED = False


def _guarded_hocon_classmethod(original, label):
    """Wrap a pyhocon include-resolution classmethod's underlying function
    (``original``, still taking ``cls`` first) so it raises
    :class:`BackendError` while :data:`_HOCON_INCLUDE_GUARD` is set, and
    behaves exactly as pyhocon shipped it otherwise.
    """

    def _guarded(cls, *args, **kwargs):
        if _HOCON_INCLUDE_GUARD.get():
            raise BackendError(
                "HOCON include resolution ({}) ran despite the text "
                "scanner having sanitized the input first; refusing to "
                "read a file or fetch a URL".format(label)
            )
        return original(cls, *args, **kwargs)

    return classmethod(_guarded)


def _install_hocon_include_guard() -> None:
    """Make pyhocon's own include-resolution entry points --
    ``ConfigFactory.parse_file``, ``ConfigFactory.parse_URL`` and
    ``ConfigParser.resolve_package_path``, the three methods its
    ``include`` machinery actually calls to read a file or fetch a URL --
    raise :class:`BackendError` for the duration of a
    :meth:`HOCONBackend.load` call, and run exactly as pyhocon shipped them
    at every other time.

    Installed once: eagerly at import time if pyhocon is already
    importable (before any other code -- a test fixture included -- gets a
    chance to monkeypatch these same three methods first), and again,
    idempotently, from :meth:`HOCONBackend.load` for the rarer case where
    pyhocon only becomes importable afterwards. The wrapping itself is a
    permanent, process-wide monkeypatch -- there is no per-call hook to
    attach to instead -- but the raising it adds is gated by a
    :class:`contextvars.ContextVar`, which is context-local (per
    thread/task): only the ``load()`` call that set the guard ever sees it
    fire, and every other pyhocon caller in the same process, at any point
    before, during or after that call, keeps pyhocon's normal behavior.

    This is a fail-closed backstop for :func:`_strip_hocon_includes`: if
    that text scanner ever has a gap (misses a directive form pyhocon's own
    grammar accepts), the include still cannot read a file or reach the
    network -- it raises instead.
    """
    global _HOCON_GUARD_INSTALLED
    if _HOCON_GUARD_INSTALLED:
        return
    from pyhocon.config_parser import ConfigFactory, ConfigParser

    for cls, attr, label in (
        (ConfigFactory, "parse_file", "file include"),
        (ConfigFactory, "parse_URL", "URL include"),
        (ConfigParser, "resolve_package_path", "package include"),
    ):
        current = cls.__dict__.get(attr)
        # Ordinarily a `classmethod` object (pyhocon's own definition);
        # tolerate anything else (e.g. a test's own monkeypatch installed
        # ahead of this call) by wrapping it as-is instead of unwrapping a
        # `.__func__` that may not exist, so installation never crashes on
        # already-patched state -- it just guards whatever is there.
        original = current.__func__ if hasattr(current, "__func__") else current
        setattr(cls, attr, _guarded_hocon_classmethod(original, label))
    _HOCON_GUARD_INSTALLED = True


# Installed eagerly, at import time, if pyhocon is already importable -- so
# the guard is in place before any test fixture (or other code) gets a
# chance to monkeypatch these same three methods for its own purposes.
# Harmless no-op if pyhocon is missing or broken: HOCONBackend.load's own
# import-time error handling covers that case, and load() also calls this
# (idempotent) for the rarer case where pyhocon becomes importable only
# after this module was first imported.
try:
    _install_hocon_include_guard()
except Exception:  # pragma: no cover - optional dependency, best-effort
    pass


class HOCONBackend(Backend):
    """HOCON (``.conf``) data via the optional ``pyhocon`` package.

    ``include`` directives are sanitized before pyhocon ever sees the text
    (see :func:`_strip_hocon_includes`): pyhocon's own include machinery
    (file reads relative to the process cwd, ``http(s)``/``file`` URL
    fetches) never runs. As a fail-closed backstop, pyhocon's own include
    entry points are also wrapped (see :func:`_install_hocon_include_guard`)
    to raise if the scanner ever has a gap.
    """

    NAMES = ("hocon_data", "hocon")

    def load(self, data):
        try:
            from pyhocon import ConfigFactory
        except ImportError as e:
            raise BackendError(
                "hocon_data backend requires the 'pyhocon' package "
                '(pip install "pyera[hocon]")'
            ) from e
        except Exception as e:
            raise BackendError(
                "hocon_data backend could not import 'pyhocon' ({}: {}); "
                'pip install "pyera[hocon]"'.format(type(e).__name__, e)
            ) from e
        _install_hocon_include_guard()
        try:
            if isinstance(data, bytes):
                data = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BackendError(_one_line(str(e))) from e
        text = _strip_hocon_includes(data)
        token = _HOCON_INCLUDE_GUARD.set(True)
        try:
            parsed = ConfigFactory.parse_string(text)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(_one_line(str(e))) from e
        finally:
            _HOCON_INCLUDE_GUARD.reset(token)
        return _as_lookupdict(parsed)


def _as_lookupdict(obj):
    """Recursively convert a parsed mapping into :class:`LookupDict`."""
    if isinstance(obj, dict):
        return LookupDict((k, _as_lookupdict(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_as_lookupdict(v) for v in obj]
    return obj


def default_backends():
    """The default backend list: YAML, sops-YAML, JSON, and HOCON if available."""
    backends = [YAMLBackend, SopsYAMLBackend, JSONBackend]
    if has_hocon():
        backends.append(HOCONBackend)
    return backends


def has_hocon() -> bool:
    """True iff the optional ``pyhocon`` dependency imports without error.

    Any import-time exception (not just ``ImportError`` -- an installed but
    broken ``pyhocon`` can raise something else entirely, e.g.
    ``AttributeError`` against a too-new stdlib) is caught and logged at
    debug, so a broken optional dependency never breaks every ``Hiera()``.
    """
    try:
        import pyhocon  # noqa: F401

        return True
    except Exception as e:
        _LOGGER.debug("pyhocon is not usable: %s: %s", type(e).__name__, e)
        return False
