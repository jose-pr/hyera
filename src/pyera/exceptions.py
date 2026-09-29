"""Exception hierarchy for hiera."""


def _one_line(text) -> str:
    """Collapse ``text`` to a single line, normalizing internal whitespace."""
    return " ".join(str(text).split())


class HieraError(Exception):
    """Base class for all hiera errors.

    ``path``, when known, names the file the problem concerns.
    """

    def __init__(self, *args, path=None):
        super().__init__(*args)
        self.path = path


class ConfigError(HieraError):
    """The base hiera configuration (``hiera.yaml``) is missing or invalid."""


class BackendError(HieraError):
    """A data file could not be read or parsed. ``.path`` names it."""


class HieraLookupError(HieraError):
    """Puppet's ``LookupError``: a failure while resolving a key."""


class InterpolationError(HieraLookupError):
    """A ``%{...}`` interpolation or function call could not be resolved."""


class MergeError(HieraLookupError):
    """An unknown or invalid merge strategy was requested."""


class KeyNotFoundError(HieraLookupError, KeyError):
    """Puppet's ``lookup()`` miss: no value was found for ``name``.

    Also a :class:`KeyError`, so an existing ``except KeyError`` keeps
    working. ``name`` is the key string, or list of key strings, that was
    tried; a list of one uses the singular message form.
    """

    def __init__(self, name):
        if isinstance(name, (list, tuple)) and len(name) != 1:
            message = (
                "Function lookup() did not find a value for any of the "
                "names {!r}".format(list(name))
            )
        else:
            single = name[0] if isinstance(name, (list, tuple)) else name
            message = (
                "Function lookup() did not find a value for the name "
                "{!r}".format(single)
            )
        super().__init__(message)
        self.name = name

    def __str__(self):
        return Exception.__str__(self)

    def __reduce__(self):
        return (type(self), (self.name,), dict(self.__dict__))
