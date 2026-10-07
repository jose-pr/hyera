"""HOCON (``hocon_data``) backend, via the optional ``pyhocon`` package.

Original code: Puppet's own ``hocon_data`` fidelity (the ``include``
directive rules and duration-as-text behaviour below) follows Puppet 8.10.0's
behaviour, not a translation of Puppet source.
"""

from __future__ import annotations

import contextvars
import importlib.util
import io
import logging
import os
import re
import threading
import typing as _ty

from .._config.confinement import check_include
from ..exceptions import BackendError, ConfigError, _one_line
from ._base import Backend, _Names
from ._hocon_includes import _allow_hocon_includes, _refuse_hocon_includes
from ._hocon_limits import install_substitution_bound

_LOGGER = logging.getLogger(__name__)

__all__ = ["HOCONBackend", "has_hocon"]


#: Labels (see `_guarded_hocon_classmethod`) that must raise instead of running during
#: `HOCONBackend.loads`: "file include" only when `hocon_includes` is False, "URL
#: include" and "package include" always. Context-local.
_HOCON_INCLUDE_GUARD: "contextvars.ContextVar[frozenset]" = contextvars.ContextVar(
    "_hocon_include_guard", default=frozenset()
)


#: Whether a substitution the document does not define may be taken from the
#: process environment, while a document is parsed. Context-local.
_HOCON_ENV: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "_hocon_env", default=True
)


class _HoconOsShim:
    """Drop-in for the module-global ``os`` name our private ``config_parser``
    copy reads ``os.environ`` through: an empty mapping while
    :data:`_HOCON_ENV` is false, the real one otherwise. Everything else is
    the real :mod:`os`."""

    def __getattr__(self, name):
        if name == "environ" and not _HOCON_ENV.get():
            return {}
        return getattr(os, name)


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


def _install_hocon_include_guard(module) -> None:
    """Make a parser module copy's own include entry points --
    ``ConfigFactory.parse_file``, ``ConfigFactory.parse_URL`` and
    ``ConfigParser.resolve_package_path``, the three methods its
    ``include`` machinery calls to read a file or fetch a URL -- raise
    :class:`BackendError` while their label is a member of
    :data:`_HOCON_INCLUDE_GUARD`'s current value, and run exactly as
    pyhocon shipped them at every other time. Idempotent per module.

    Only the private copy built by :func:`_hocon_parser` is ever passed
    here: it has its own ``ConfigFactory``/``ConfigParser`` classes, distinct
    from the shared ``pyhocon.config_parser``'s, and is the only module
    :meth:`HOCONBackend.loads` parses through. The shared module is never
    touched.

    The raising is gated by a :class:`contextvars.ContextVar`, which is
    context-local (per thread/task): only the ``loads()`` call that set it
    sees it fire.

    This is a fail-closed backstop for :func:`_allow_hocon_includes`/
    :func:`_refuse_hocon_includes`: if that text scanner ever has a gap
    (misses a directive form pyhocon's own grammar accepts), the include
    still cannot read a file or reach the network for a form the active
    mode does not intend to resolve for real -- it raises instead.
    """
    if getattr(module, "_hocon_include_guard_installed", False):
        return
    ConfigFactory, ConfigParser = module.ConfigFactory, module.ConfigParser

    for cls, attr, label in (
        (ConfigFactory, "parse_file", "file include"),
        (ConfigFactory, "parse_URL", "URL include"),
        (ConfigParser, "resolve_package_path", "package include"),
    ):
        current = cls.__dict__.get(attr)
        # Ordinarily a `classmethod` object; anything else (a test's monkeypatch
        # installed ahead of this call) is wrapped as-is, so installation never crashes
        # on already-patched state.
        original = current.__func__ if hasattr(current, "__func__") else current
        setattr(cls, attr, _guarded_hocon_classmethod(original, label))

    module._hocon_include_guard_installed = True


def _unquote_key(key):
    if isinstance(key, str) and len(key) > 2 and key[0] == key[-1] == '"':
        return key[1:-1]
    return key


def _as_plain(obj):
    """Recursively convert pyhocon's ``ConfigTree``/``ConfigList`` (both
    ``dict``/``list`` subclasses) into plain ``dict``/``list``, dropping the
    quote characters pyhocon keeps around a quoted key such as
    ``"ntp::servers"``."""
    if isinstance(obj, dict):
        return {_unquote_key(k): _as_plain(v) for k, v in obj.items()}
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

    The text read is the included file's, so it goes through
    :func:`_allow_hocon_includes` before pyhocon parses it: the include
    rules hold in a file at any depth, not only in the top-level text.
    """

    @staticmethod
    def open(filename, mode="r", encoding=None, **kwargs):
        check_include(filename)
        with open(filename, mode, encoding=encoding, **kwargs) as fd:
            text = fd.read()
        return io.StringIO(_allow_hocon_includes(text))


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


# Blanks after a ``null`` keyword that a following value token continues; pyhocon
# drops them, which would turn ``null x`` into ``nullx``.
_NULL_TRAILING_BLANKS_RE = re.compile(r"([ \t]*)(?![\s#,\]}]|//|\Z)")


def _make_null_text(none_value, replace_with):
    """Return ``(NullText, replace_with)`` for the private parser copy:
    ``NullText`` subclasses pyhocon's ``NoneValue`` and renders as ``null`` plus
    the blanks that followed it when a concatenation (``k = null x``) turns it
    into text; the returned ``replace_with`` builds it for the ``null`` keyword
    and defers to ``replace_with`` for the other keywords.
    """

    class NullText(none_value):
        def __init__(self, blanks=""):
            self.blanks = blanks

        def __str__(self):
            return "null" + self.blanks

    def null_aware_replace_with(value):
        if not isinstance(value, NullText):
            return replace_with(value)

        def action(instring, loc, tokens):
            match = _NULL_TRAILING_BLANKS_RE.match(instring, loc + len(tokens[0]))
            return [NullText(match.group(1) if match else "")]

        return action

    return NullText, null_aware_replace_with


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
            mod.NoneValue, mod.replace_with = _make_null_text(
                mod.NoneValue, mod.replace_with
            )
            mod.codecs = _HoconFileCodecsShim()
            mod.os = _HoconOsShim()
            mod.logger = _HoconLoggerShim(mod.logger)
            _install_hocon_include_guard(mod)
            install_substitution_bound(mod)
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
    does by default (see :func:`_allow_hocon_includes` for the
    rule): a plain quoted include contributes
    nothing, ``include file(...)`` really reads the file (relative to the
    process cwd, or absolute), and every other form (``url(...)``,
    ``classpath(...)``, ``required(...)``, ``package(...)``, a
    case-mismatched keyword, a bare ``include`` with nothing valid after
    it) raises, matching Puppet's own parse/method errors for those forms.
    A value-position directive (including inside a ``[...]`` array) is
    kept as literal text, as Puppet keeps it.

    Passing ``hocon_includes=False`` (to the constructor directly, or via
    a ``hocon_includes: false`` key on the hierarchy entry/``defaults`` --
    hyera's own extension, not Puppet vocabulary) restricts it instead
    (see :func:`_refuse_hocon_includes`): every
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
    :param hocon_env: ``None`` (the default) reads ``conf.get("hocon_env",
        True)``. When false, a substitution the document does not define is
        never taken from the process environment: ``${?VAR}`` is absent and
        ``${VAR}`` fails to resolve. Hyera's own extension.
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
        hocon_env: _ty.Optional[bool] = None,
    ) -> None:
        super().__init__(conf, strict=strict)
        if hocon_env is None:
            hocon_env = self.conf.get("hocon_env", True)
        self.hocon_env: bool = bool(hocon_env)
        # No `Hiera(backend_options=...)` plumbing exists, so the opt-in reads from the
        # level's own `conf` (its hiera.yaml hierarchy-entry/`defaults` mapping) when
        # not passed directly.
        if hocon_includes is None:
            hocon_includes = self.conf.get("hocon_includes", True)
        self.hocon_includes: bool = bool(hocon_includes)

    def data_hash(
        self,
        path: _ty.Any,
        options: _ty.Mapping[str, _ty.Any],
    ) -> _ty.Dict[str, _ty.Any]:
        """The ``data_hash`` hook. Besides ``path``, the hierarchy options
        accepted are ``hocon_includes`` (a Boolean), which selects the include
        mode for this level, and ``hocon_env`` (a Boolean), which allows or
        stops reading the process environment; any other option raises as
        for every built-in file function.

        :param path: the location's file path.
        :param options: the hierarchy entry's ``options``.
        :returns: the parsed data.
        :raises ConfigError: ``hocon_includes`` or ``hocon_env`` is not a Boolean, or
            ``options`` carries anything else besides ``path``.
        :raises BackendError: the file could not be read or parsed.
        """
        rest = dict(options)
        chosen = {}
        for name in ("hocon_includes", "hocon_env"):
            if name not in rest:
                continue
            value = rest.pop(name)
            if not isinstance(value, bool):
                raise ConfigError(
                    "'hocon_data' option '{}' must be a Boolean, not {}".format(
                        name, type(value).__name__
                    )
                )
            chosen[name] = value
        backend = self
        if any(getattr(self, name) != value for name, value in chosen.items()):
            settings = {
                "hocon_includes": self.hocon_includes,
                "hocon_env": self.hocon_env,
            }
            settings.update(chosen)
            backend = type(self)(self.conf, strict=self._strict, **settings)
        return super(HOCONBackend, backend).data_hash(path, rest)

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
        if self.hocon_includes:
            text = _allow_hocon_includes(text)
            # `file(...)` is deliberately NOT guarded here: running it for real matches
            # Puppet. `url`/`package` stay backstopped, as Puppet's hocon_data cannot
            # resolve those either.
            guarded = frozenset({"URL include", "package include"})
        else:
            text = _refuse_hocon_includes(text)
            guarded = frozenset({"file include", "URL include", "package include"})
        token = _HOCON_INCLUDE_GUARD.set(guarded)
        env_token = _HOCON_ENV.set(self.hocon_env)
        failure = None
        try:
            mod = _hocon_parser()
            parsed = mod.ConfigFactory.parse_string(text)
        except BackendError:
            raise
        except Exception as e:
            failure = _one_line(str(e))
        finally:
            _HOCON_ENV.reset(env_token)
            _HOCON_INCLUDE_GUARD.reset(token)
        if failure is not None:
            # Raised outside the handler: pyhocon's exception carries the
            # whole document and must not stay reachable from this one.
            raise BackendError(failure) from None
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
