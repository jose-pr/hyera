"""Exception hierarchy for hiera."""

import typing as _ty

__all__ = [
    "HieraError",
    "ConfigError",
    "BackendError",
    "BackendTimeoutError",
    "HieraLookupError",
    "InterpolationError",
    "MergeError",
    "KeyNotFoundError",
]


def _one_line(text: object) -> str:
    """Collapse ``text`` to a single line, normalizing internal whitespace."""
    return " ".join(str(text).split())


class HieraError(Exception):
    """Base class for all hiera errors.

    ``path``, when known, names the file the problem concerns.

    :param args: the positional arguments ``Exception`` itself takes
        (usually one message string).
    :param path: the file this problem concerns, when known.
    """

    def __init__(self, *args: object, path: _ty.Optional[str] = None) -> None:
        super().__init__(*args)
        self.path: _ty.Optional[str] = path


class ConfigError(HieraError):
    """The base hiera configuration (``hiera.yaml``) is missing or invalid.

    ``line``, when known, is the 1-based line in ``.path`` the problem was
    found at (e.g. a malformed hierarchy entry).

    :param args: the positional arguments ``Exception`` itself takes
        (usually one message string).
    :param path: the file this problem concerns, when known.
    :param line: the 1-based line in ``path`` this problem concerns, when
        known.
    """

    def __init__(
        self,
        *args: object,
        path: _ty.Optional[str] = None,
        line: _ty.Optional[int] = None,
    ) -> None:
        super().__init__(*args, path=path)
        self.line: _ty.Optional[int] = line


class BackendError(HieraError):
    """A data file could not be read or parsed. ``.path`` names it."""


class BackendTimeoutError(BackendError, TimeoutError):
    """An external program a backend or fact source runs (``sops``,
    ``facter``) did not finish within its time limit and was killed along
    with its child processes."""


class HieraLookupError(HieraError):
    """Puppet's ``LookupError``: a failure while resolving a key."""


class InterpolationError(HieraLookupError):
    """A ``%{...}`` interpolation or function call could not be resolved."""


class MergeError(HieraLookupError):
    """An unknown or invalid merge strategy was requested."""


def _issue_coded(exc: HieraLookupError) -> HieraLookupError:
    """Mark ``exc`` as one of Puppet's *issue-coded* lookup errors
    (``invocation.rb:100-103``): raised from the global layer's own data, it
    still survives as ``explain()``'s last line rather than escaping --
    unlike an ordinary code-less ``LookupError``, which a global-layer
    boundary (:func:`_escapes`) re-raises as a plain error instead. Wrap
    exactly the three raise sites Puppet gives an issue code: an unknown
    interpolation method, an ``alias`` not spanning the whole string, and
    method syntax used where it is disallowed."""
    exc._explain_issue = True
    return exc


def _escapes(exc: HieraLookupError) -> HieraLookupError:
    """Mark ``exc`` as one that always escapes ``explain()`` (never becomes
    its last line): a value-type assertion failure, or a ``HieraLookupError``/
    ``BackendError`` that leaves the *global* layer without an issue code
    (``lookup_adapter.rb:148-153``). Idempotent."""
    exc._explain_escape = True
    return exc


class KeyNotFoundError(HieraLookupError, KeyError):
    """Puppet's ``lookup()`` miss: no value was found for ``name``.

    Also a :class:`KeyError`, so an existing ``except KeyError`` keeps
    working. ``name`` is the key string, or list of key strings, that was
    tried; a list of one uses the singular message form. A name may also
    be a tuple key path: ``.name`` keeps it exactly as given, but
    the message text renders it as :func:`~hyera._lookup.navigation.join_key`'s
    dotted form, same as the equivalent quoted string would read.

    A bare tuple *path* is never passed here directly -- it would be
    misread as "several names" instead of one path with several segments.
    Every caller wraps it in a ``list`` first (even a list of one), the
    same way a single ``str`` name already is.

    :param name: the key(s) that were tried.
    """

    def __init__(self, name: _ty.Union[str, _ty.Sequence[str]]) -> None:
        # Deferred to avoid a module-load cycle (`_lookup.navigation` imports
        # `HieraLookupError` from this module).
        from ._lookup.navigation import join_key

        def _text(n):
            return join_key(n) if isinstance(n, tuple) else n

        if isinstance(name, (list, tuple)) and len(name) != 1:
            message = (
                "Function lookup() did not find a value for any of the "
                "names {!r}".format([_text(n) for n in name])
            )
        else:
            single = name[0] if isinstance(name, (list, tuple)) else name
            message = (
                "Function lookup() did not find a value for the name "
                "{!r}".format(_text(single))
            )
        super().__init__(message)
        self.name: _ty.Union[str, _ty.Sequence[str]] = name

    def __str__(self) -> str:
        return Exception.__str__(self)

    def __reduce__(
        self,
    ) -> "_ty.Tuple[type, _ty.Tuple[_ty.Union[str, _ty.Sequence[str]]], _ty.Dict[str, _ty.Any]]":
        return (type(self), (self.name,), dict(self.__dict__))
