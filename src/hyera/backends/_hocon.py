"""HOCON (``hocon_data``) backend, via the optional ``pyhocon`` package.

Original code: Puppet's own ``hocon_data`` fidelity (the ``include``
directive rules and duration-as-text behaviour below) is measured against
the oracle, not translated from Puppet source.
"""

import contextvars
import importlib.util
import logging
import re
import threading
import typing as _ty

from ..exceptions import BackendError, _one_line
from . import Backend, _Names

_LOGGER = logging.getLogger(__name__)

__all__ = ["HOCONBackend", "has_hocon"]


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


def _refuse_hocon_includes(text: str) -> str:
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
    ever running.

    **This is no longer the default (2026-09-29).** It refuses more
    than Puppet's own ``hocon_data`` does (Puppet really does read
    ``file(...)`` and keeps a value-position directive as literal text --
    see :func:`_allow_hocon_includes`), so it is reachable only as the
    opt-in restriction (:class:`HOCONBackend`'s ``hocon_includes=False``).
    See ``AGENTS.md`` for the exact rule.

    A :func:`_install_hocon_include_guard`-installed backstop still applies
    even if this scanner has a gap: pyhocon's own include-resolution
    entry points raise unconditionally for the duration of
    :meth:`HOCONBackend.loads`, so a missed directive fails closed instead
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
            # and hyera (which always raises for value position) must not
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


def _allow_hocon_includes(text: str) -> str:
    """Scan HOCON ``include`` directives to match Puppet's own
    ``hocon_data`` (2026-09-29: the default; :func:`_refuse_hocon_includes`
    is the opt-in restriction). Oracle-measured against Ruby hocon 1.4.0
    (Puppet 8.10.0, WSL) for every form:

    - a plain quoted ``include "..."`` (relative, absolute, or a
      ``file://`` URL) always contributes nothing -- Puppet's own bare
      quoted-string form is never a real file/URL read, in either mode;
    - a plain quoted ``include "<scheme>://...">`` for any *other* scheme
      always raises -- Puppet's own ``include_url_without_fallback``/
      ``include_url`` methods are simply missing (measured: a real
      ``http://`` target is never even requested);
    - ``include file(...)`` (relative or absolute, also inside a nested
      object or after another key) is left untouched here, so pyhocon's
      own resolution -- which already reads the file relative to the
      process cwd or absolute, exactly as Ruby hocon does -- runs for
      real;
    - every other key-position directive (``url(...)``, ``classpath(...)``,
      ``required(...)`` whether or not its target exists, ``package(...)``,
      any other ``name(...)``, a space before the paren, or a bare
      ``include`` with nothing valid after it) raises, matching Puppet's
      own parse/method errors for those forms -- Ruby hocon implements
      none of them;
    - an occurrence in value position (including inside a ``[...]``
      array), of *any* spelling/case that looks like a directive, is
      defanged into an ordinary quoted token instead of raising or being
      resolved, so the concatenated result is the exact literal text
      Puppet keeps (measured: ``msg = please include file("x")`` gives
      the literal string ``"please include file(x)"``, quotes and all
      stripped exactly as HOCON string concatenation does for any other
      quoted segment) -- Puppet's ``include`` is a member-level statement
      only, never special in value position, in any spelling;
    - a key-position spelling/case other than the exact lowercase keyword
      ``include`` is never Puppet's own directive either (Ruby's keyword
      match is case-sensitive) and always raises when a directive-shaped
      argument follows -- refusing rather than letting pyhocon's own
      *caseless* keyword matching run a real file/URL read Puppet's own
      hocon_data never would.

    One measured, accepted divergence: Puppet's ``include file("*.conf")``
    never globs (documented "contributes nothing"); pyhocon's own
    resolution *does* glob and includes any match -- kept as a documented
    extension (hyera may do more than Puppet, never less), filed as
    ``hocon-file-include-globs-where-puppet-does-not``.

    A :func:`_install_hocon_include_guard`-installed backstop still
    applies for the forms this mode does not intend to resolve for real
    (``url``/``package`` resolution; see :meth:`HOCONBackend.loads`): a
    scanner gap in one of the forms above still fails closed instead of
    silently reaching the network. ``file(...)`` resolution is
    deliberately NOT guarded here -- letting it run for real is the
    entire point of this mode.

    Blanking and defanging both preserve the original text's length and
    line numbering exactly as :func:`_refuse_hocon_includes` does (see its
    docstring); a defanged value-position word grows by its two quote
    characters in its own first slot, offset by shrinking its remaining
    slots to the empty string, so nothing else in ``out`` shifts.
    """
    n = len(text)
    out = list(text)
    i = 0
    last_sig = None  # last significant (non-space/tab) char seen so far
    brackets = []  # stack of open '{'/'[' seen so far
    while i < n:
        c = text[i]
        if c in ('"', "#", "$") and _preceded_by_odd_backslashes(text, i):
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

            in_array = bool(brackets) and brackets[-1] == "["
            key_position = (not in_array) and last_sig in (None, "\n", "{", ",")
            line = text.count("\n", 0, i) + text.count("\r", 0, i) + 1
            line -= text.count("\r\n", 0, i)

            if not key_position:
                # Defang rather than raise or resolve: Puppet keeps this
                # as literal text, in any spelling/case, since `include`
                # is never special outside statement position to Ruby.
                # pyhocon disagrees (its own `include_expr` grammar is
                # accepted in value position too, caselessly), so quote
                # the bareword so pyhocon's tokenizer sees an ordinary
                # string instead of a keyword.
                if _hocon_directive_follows(text, j):
                    out[i] = '"' + word + '"'
                    for k in range(i + 1, j):
                        out[k] = ""
                last_sig = word[-1]
                i = j
                continue

            if word != "include":
                # Any spelling/case other than the exact lowercase
                # keyword is never Puppet's own directive (Ruby's match
                # is case-sensitive); refuse outright rather than let
                # pyhocon's caseless grammar run a real read Puppet
                # itself would never attempt. A non-directive
                # continuation (`INCLUDE = 1`) is left alone -- Puppet
                # parses that as an ordinary key.
                if _hocon_directive_follows(text, j):
                    raise BackendError(
                        "HOCON include directive is not supported here "
                        "(line {})".format(line)
                    )
                last_sig = word[-1]
                i = j
                continue

            after = _skip_hocon_blanks(text, j)
            if after < n and text[after] == '"':
                end, content = _find_hocon_string_end(text, after)
                scheme = _URL_SCHEME_RE.match(content)
                if scheme and scheme.group(0)[:-3].lower() != "file":
                    raise BackendError(
                        "HOCON include of a non-file URL is not "
                        "supported (line {})".format(line)
                    )
                # Puppet's own hocon_data never implements the bare
                # quoted-string form as a real file/URL read -- it
                # always contributes nothing, whatever the target.
                for k in range(i, end):
                    if out[k] not in ("\n", "\r"):
                        out[k] = " "
                i = end
                last_sig = " "
                continue

            if text[after : after + 5] == "file(":
                # Puppet's `include file(...)` really does read the file
                # (relative to the process cwd, or absolute) -- leave the
                # text untouched so pyhocon's own resolution (which
                # already does the same cwd-relative/absolute lookup)
                # runs for real.
                last_sig = word[-1]
                i = j
                continue

            raise BackendError(
                "HOCON include is only supported as a plain quoted "
                "string or file(...); url()/classpath()/required()/"
                "package() and similar forms are not supported "
                "(line {})".format(line)
            )

        last_sig = c
        i += 1

    return "".join(out)


#: Set (only for the duration of a `HOCONBackend.loads` call) to the labels
#: (see `_guarded_hocon_classmethod`) that must raise instead of running for
#: that call, so the `_install_hocon_include_guard`-wrapped pyhocon entry
#: points fail closed only for the forms the active mode does not intend to
#: resolve for real -- `"file include"` is a member only when
#: `HOCONBackend.hocon_includes` is False (the opt-in restriction);
#: `"URL include"`/`"package include"` are always members, in either mode,
#: since Puppet's own hocon_data can never resolve those either. Empty
#: outside a `loads()` call. Context-local (per thread/task), so a
#: concurrent `loads()` on another thread and every other pyhocon caller in
#: the process, at any point in time, are unaffected -- only the call(s)
#: that set it see it fire.
_HOCON_INCLUDE_GUARD: "contextvars.ContextVar[frozenset]" = contextvars.ContextVar(
    "_hocon_include_guard", default=frozenset()
)

_HOCON_GUARD_INSTALLED = False


def _guarded_hocon_classmethod(original, label):
    """Wrap a pyhocon include-resolution classmethod's underlying function
    (``original``, still taking ``cls`` first) so it raises
    :class:`BackendError` while ``label`` is a member of
    :data:`_HOCON_INCLUDE_GUARD`'s current value, and behaves exactly as
    pyhocon shipped it otherwise.
    """

    def _guarded(cls, *args, **kwargs):
        if label in _HOCON_INCLUDE_GUARD.get():
            raise BackendError(
                "HOCON include resolution ({}) ran despite the text "
                "scanner having sanitized the input first; refusing to "
                "read a file or fetch a URL".format(label)
            )
        return original(cls, *args, **kwargs)

    return classmethod(_guarded)


def _install_hocon_include_guard(module=None) -> None:
    """Make an include-resolving module's own entry points --
    ``ConfigFactory.parse_file``, ``ConfigFactory.parse_URL`` and
    ``ConfigParser.resolve_package_path``, the three methods its
    ``include`` machinery actually calls to read a file or fetch a URL --
    raise :class:`BackendError` while their label is a member of
    :data:`_HOCON_INCLUDE_GUARD`'s current value, and run exactly as
    pyhocon shipped them at every other time.

    ``module=None`` (the default) targets the **shared** ``pyhocon``
    package (``import pyhocon.config_parser``) -- the only place a caller
    who imports ``pyhocon`` directly (not through :class:`HOCONBackend`)
    reaches its include machinery from. Installed once: eagerly at import
    time if pyhocon is already importable (before any other code -- a
    test fixture included -- gets a chance to monkeypatch these same
    three methods first), and again, idempotently, from
    :meth:`HOCONBackend.loads` for the rarer case where pyhocon only
    becomes importable afterwards.

    A caller may also pass the **private** ``_hocon_parser()`` module
    copy explicitly. This matters: that copy's ``exec_module`` gives it
    its own, distinct ``ConfigFactory``/``ConfigParser`` classes -- not
    the shared module's (verified 2026-09-29:
    ``_hocon_parser().ConfigFactory is not
    pyhocon.config_parser.ConfigFactory``) -- and :meth:`HOCONBackend.loads`
    parses through that private copy, not the shared one. Patching only
    the shared module (as this function used to) left the
    fail-closed backstop below installed but inert for every real
    :class:`HOCONBackend` call: the wrapped shared-module methods were
    simply never on the call path. :func:`_hocon_parser` now calls this
    a second time, with its own ``mod``, to close that gap -- pre-existing
    (since the private copy was added), found and fixed
    while hardening the more permissive default, which makes the
    backstop's guarantee matter more, not less.

    The wrapping itself is a permanent, process-wide monkeypatch on
    whichever module is targeted -- there is no per-call hook to attach to
    instead -- but the raising it adds is gated by a
    :class:`contextvars.ContextVar`, which is context-local (per
    thread/task): only the ``loads()`` call that set the guard ever sees it
    fire, and every other pyhocon caller in the same process, at any point
    before, during or after that call, keeps pyhocon's normal behavior.

    This is a fail-closed backstop for :func:`_allow_hocon_includes`/
    :func:`_refuse_hocon_includes`: if that text scanner ever has a gap
    (misses a directive form pyhocon's own grammar accepts), the include
    still cannot read a file or reach the network for a form the active
    mode does not intend to resolve for real -- it raises instead.
    """
    global _HOCON_GUARD_INSTALLED
    if module is None:
        if _HOCON_GUARD_INSTALLED:
            return
        from pyhocon.config_parser import ConfigFactory, ConfigParser
    else:
        if getattr(module, "_hocon_include_guard_installed", False):
            return
        ConfigFactory, ConfigParser = module.ConfigFactory, module.ConfigParser

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

    if module is None:
        _HOCON_GUARD_INSTALLED = True
    else:
        module._hocon_include_guard_installed = True
    _HOCON_GUARD_INSTALLED = True


def _try_install_hocon_include_guard() -> None:
    """Best-effort wrapper around :func:`_install_hocon_include_guard` for
    module import time: pyhocon being present but broken in some way this
    guard's own probing cannot anticipate must never crash importing
    ``hyera`` itself -- :meth:`HOCONBackend.check_available` (and
    :meth:`HOCONBackend.loads`, which calls
    :func:`_install_hocon_include_guard` again, unguarded, once pyhocon has
    already been imported successfully) cover the missing/broken case for
    real.
    """
    try:
        _install_hocon_include_guard()
    except Exception:
        pass


# Installed eagerly, at import time, if pyhocon is already importable -- so
# the guard is in place before any test fixture (or other code) gets a
# chance to monkeypatch these same three methods for its own purposes.
_try_install_hocon_include_guard()


def _as_plain(obj):
    """Recursively convert pyhocon's ``ConfigTree``/``ConfigList`` (both
    ``dict``/``list`` subclasses) into plain ``dict``/``list``."""
    if isinstance(obj, dict):
        return {k: _as_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_as_plain(v) for v in obj]
    return obj


_HOCON_PARSER_LOCK = threading.Lock()
_HOCON_PARSER_MODULE = None


class _HoconFileCodecsShim:
    """Drop-in for the module-global ``codecs`` name our private
    ``config_parser`` copy resolves ``codecs.open(filename, 'r',
    encoding=encoding)`` against (``ConfigFactory.parse_file``, the one
    call the default mode lets run for real -- see :func:`_allow_hocon_includes`).

    ``codecs.open`` is deprecated since Python 3.13 and raises on 3.14+
    (``DeprecationWarning: codecs.open() is deprecated. Use open()
    instead.``); this project's ``filterwarnings = ["error"]`` turns that
    into a fatal exception the moment a real ``include file(...)``
    resolves. Builtin ``open`` in text mode with the same ``encoding``
    reads identically for pyhocon's own use here (a whole-file text read,
    no codec-specific behavior pyhocon relies on).
    """

    @staticmethod
    def open(filename, mode="r", encoding=None, **kwargs):
        return open(filename, mode, encoding=encoding, **kwargs)


class _HoconLoggerShim:
    """Drop-in for the module-global ``logger`` name our private
    ``config_parser`` copy calls ``.warn(...)`` through -- two call sites
    inside ``ConfigFactory.parse_file``/``parse_URL``, on the non-required,
    unreachable-target branch a real ``include file(...)``/``include
    url(...)`` (now reachable by default) can now actually reach.

    ``Logger.warn`` is deprecated (use ``.warning``) and raises under this
    project's ``filterwarnings = ["error"]`` the moment either branch
    runs. Delegates everything else to the real logger unchanged -- the
    real logger is shared (``logging.getLogger(__name__)`` inside our
    private copy resolves to the SAME registry entry as the shared
    module's, since both share the module name ``pyhocon.config_parser``),
    so this only intercepts the one deprecated spelling.
    """

    def __init__(self, real):
        self._real = real

    def warn(self, *args, **kwargs):
        self._real.warning(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _hocon_parser():
    """A private copy of the ``pyhocon.config_parser`` module, with its
    ``get_period_expr`` grammar replaced by one that never matches, so a
    HOCON duration (``10s``, ``5 minutes``) stays literal text -- matching
    real Ruby hocon 1.4.0, which has no duration type at all -- instead of
    becoming a ``datetime.timedelta`` (which crashes ``-o yaml``); and its
    ``codecs`` name replaced by :class:`_HoconFileCodecsShim` so a real
    ``include file(...)`` resolution (now the default) never hits the
    ``codecs.open`` deprecation above.

    Built once, under a lock, and cached. The *shared* ``pyhocon`` module
    (``import pyhocon; pyhocon.ConfigFactory...``) is never touched --
    every other caller of pyhocon in the process keeps the real duration
    behavior and the real ``codecs`` module. ``get_period_expr`` has
    existed since pyhocon 0.3.60; the ``hocon`` extra's floor is pinned
    there (or higher) for exactly this.
    """
    global _HOCON_PARSER_MODULE
    if _HOCON_PARSER_MODULE is not None:
        return _HOCON_PARSER_MODULE
    with _HOCON_PARSER_LOCK:
        if _HOCON_PARSER_MODULE is None:
            import pyparsing

            spec = importlib.util.find_spec("pyhocon.config_parser")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if not hasattr(mod, "get_period_expr"):
                raise BackendError(
                    "hocon_data requires pyhocon>=0.3.60 (get_period_expr, "
                    "used to keep durations as text): "
                    'pip install "hyera[hocon]"'
                )
            mod.get_period_expr = lambda: pyparsing.NoMatch()
            mod.codecs = _HoconFileCodecsShim()
            mod.logger = _HoconLoggerShim(mod.logger)
            _install_hocon_include_guard(mod)
            _HOCON_PARSER_MODULE = mod
    return _HOCON_PARSER_MODULE


_HOCON_KIND_NAMES = {"ConfigList": "LIST"}


def _hocon_root_kind(value) -> str:
    return _HOCON_KIND_NAMES.get(type(value).__name__, type(value).__name__.upper())


class HOCONBackend(Backend):
    """HOCON (``.conf``) data via the optional ``pyhocon`` package.

    Always registered: a missing/broken ``pyhocon`` fails at
    :meth:`check_available` (backend/level construction) and again in
    :meth:`loads`, both naming the ``hocon`` extra -- so the failure
    is always reachable instead of silently disappearing from
    :func:`default_backends`.

    ``include`` directives resolve exactly as Puppet's own ``hocon_data``
    does by default (2026-09-29 -- see :func:`_allow_hocon_includes`
    for the oracle-measured rule): a plain quoted include contributes
    nothing, ``include file(...)`` really reads the file (relative to the
    process cwd, or absolute), and every other form (``url(...)``,
    ``classpath(...)``, ``required(...)``, ``package(...)``, a
    case-mismatched keyword, a bare ``include`` with nothing valid after
    it) raises, matching Puppet's own parse/method errors for those forms.
    A value-position directive (including inside a ``[...]`` array) is
    kept as literal text, as Puppet keeps it.

    Passing ``hocon_includes=False`` (to the constructor directly, or via
    a ``hocon_includes: false`` key on the hierarchy entry/``defaults`` --
    hyera's own extension, not Puppet vocabulary) restores the
    pre-fidelity refusal instead (see :func:`_refuse_hocon_includes`): every
    directive form other than a plain quoted include raises, including
    ``file(...)``.

    In either mode, pyhocon's own include entry points are also wrapped
    (see :func:`_install_hocon_include_guard`) as a fail-closed backstop
    for the forms that mode does not intend to resolve for real, so a gap
    in the text scanner still fails closed instead of silently reading a
    file or reaching the network. Durations are parsed via a private
    module copy (see :func:`_hocon_parser`) so they stay text.

    :param conf: the hierarchy entry's/``defaults``'s own mapping.
    :param strict: overrides the call-time default.
    :param hocon_includes: ``None`` (the default) reads
        ``conf.get("hocon_includes", True)``.
    """

    NAMES: _ty.ClassVar[_Names] = {"function": ("hocon_data",), "format": ("hocon",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".conf",)

    _MISSING_DEP_MESSAGE = (
        "hocon_data requires the optional 'pyhocon' package: "
        'pip install "hyera[hocon]"'
    )

    def __init__(
        self,
        conf: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
        *,
        strict: _ty.Optional[str] = None,
        hocon_includes: _ty.Optional[bool] = None,
    ) -> None:
        super().__init__(conf, strict=strict)
        # No `Hiera(backend_options=...)` plumbing exists
        # yet, so the opt-in reads from the level's own `conf` (its
        # hiera.yaml hierarchy-entry/`defaults` mapping) when not passed
        # directly.
        if hocon_includes is None:
            hocon_includes = self.conf.get("hocon_includes", True)
        self.hocon_includes: bool = bool(hocon_includes)

    @classmethod
    def check_available(cls) -> None:
        """Raise :class:`BackendError` naming the ``hocon`` extra
        when ``pyhocon`` is not importable."""
        if not has_hocon():
            raise BackendError(cls._MISSING_DEP_MESSAGE)

    def loads(self, text: str) -> _ty.Any:
        """Parse HOCON the way Puppet's ``hocon_data`` does: ``include
        file(...)`` really reads the file, ``include url(...)``/
        ``classpath(...)``/``required(...)`` and durations raise/stay text
        (see the class docstring for the full fidelity rule).

        :param text: the HOCON text to parse.
        :returns: the parsed value.
        :raises BackendError: ``pyhocon`` is missing, or ``text`` is not
            valid HOCON (or an ``include``/duration form this rule rejects).
        """
        try:
            from pyhocon import ConfigTree
        except ImportError:
            raise BackendError(self._MISSING_DEP_MESSAGE) from None
        except Exception as e:
            raise BackendError(
                "hocon_data backend could not import 'pyhocon' ({}: {}); "
                'pip install "hyera[hocon]"'.format(type(e).__name__, e)
            ) from None
        _install_hocon_include_guard()
        if self.hocon_includes:
            text = _allow_hocon_includes(text)
            # `file(...)` is deliberately NOT guarded here -- letting it
            # run for real (matching Puppet) is the entire point of this
            # mode; `url`/`package` resolution stays backstopped since
            # Puppet's own hocon_data can never resolve those either.
            guarded = frozenset({"URL include", "package include"})
        else:
            text = _refuse_hocon_includes(text)
            guarded = frozenset({"file include", "URL include", "package include"})
        token = _HOCON_INCLUDE_GUARD.set(guarded)
        try:
            mod = _hocon_parser()
            parsed = mod.ConfigFactory.parse_string(text)
        except BackendError:
            raise
        except Exception as e:
            raise BackendError(_one_line(str(e))) from None
        finally:
            _HOCON_INCLUDE_GUARD.reset(token)
        if not isinstance(parsed, ConfigTree):
            raise BackendError(
                "hocon_data: has type {} rather than object at file "
                "root".format(_hocon_root_kind(parsed))
            )
        return _as_plain(parsed)


def has_hocon() -> bool:
    """True iff the optional ``pyhocon`` dependency imports without error.

    Any import-time exception (not just ``ImportError`` -- an installed but
    broken ``pyhocon`` can raise something else entirely, e.g.
    ``AttributeError`` against a too-new stdlib) is caught and logged at
    debug, so a broken optional dependency never breaks every ``Hiera()``.

    :returns: whether ``pyhocon`` is usable.
    """
    try:
        import pyhocon  # noqa: F401

        return True
    except Exception as e:
        _LOGGER.debug("pyhocon is not usable: %s: %s", type(e).__name__, e)
        return False
