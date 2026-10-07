"""Helpers for testing a :class:`~hyera.backends.Backend`: a hook runs the way
the engine runs it, from a unit test and without a hiera.yaml.

Nothing here imports pytest or an optional dependency.
"""

from __future__ import annotations

import os
import typing as _ty

from ._lookup.function_provider import (
    LookupContext,
    _check_kind_implemented,
    _data_hash_not_found,
    _hook_error,
    _NotFound,
    _tuples_to_lists,
    _validate_data_hash,
    _validate_provider_value,
)
from ._lookup.lookup_adapter import _lookup_value_type, validate_data_value
from .core import Hiera
from .backends._base import Backend, default_backends
from .exceptions import BackendError, HieraError

__all__ = ["NOT_FOUND", "BackendContract", "data_dig", "data_hash", "lookup_key"]


class _NotFoundType:
    """The type of :data:`NOT_FOUND`."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "hyera.testing.NOT_FOUND"


#: What :func:`lookup_key` and :func:`data_dig` return when the hook called
#: ``context.not_found()``. Compare with ``is``.
NOT_FOUND = _NotFoundType()


def _instance(backend: _ty.Union[Backend, _ty.Type[Backend]]) -> Backend:
    return backend() if isinstance(backend, type) else backend


def lookup_key(
    backend: _ty.Union[Backend, _ty.Type[Backend]],
    key: str,
    options: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    *,
    context: _ty.Optional[LookupContext] = None,
) -> _ty.Any:
    """Call ``backend``'s ``lookup_key`` hook as the engine does.

    The hook must be implemented and its value must be one Hiera accepts; the
    value comes back with tuples read as lists.

    :param backend: a backend instance, or a class to instantiate with no
        arguments.
    :param key: the key the hook is asked for.
    :param options: the hierarchy entry's ``options``; none when omitted.
    :param context: the context passed to the hook;
        :meth:`LookupContext.for_testing() <hyera.LookupContext.for_testing>`
        when omitted.
    :returns: the hook's value, or :data:`NOT_FOUND` when it called
        ``context.not_found()``.
    :raises ConfigError: the backend does not implement ``lookup_key``.
    :raises BackendError: the hook returned a value outside Puppet's data types.
    """
    backend = _instance(backend)
    _check_kind_implemented(backend, "lookup_key")
    if context is None:
        context = LookupContext.for_testing()
    try:
        value = backend.lookup_key(key, dict(options or {}), context)
    except _NotFound:
        return NOT_FOUND
    except HieraError:
        raise
    except Exception as e:
        raise _hook_error(e, "lookup_key", backend.name, None) from e
    return _validate_provider_value(value, "lookup_key", backend.name, None)


def data_dig(
    backend: _ty.Union[Backend, _ty.Type[Backend]],
    segments: _ty.Sequence[str],
    options: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    *,
    context: _ty.Optional[LookupContext] = None,
) -> _ty.Any:
    """Call ``backend``'s ``data_dig`` hook as the engine does.

    :param backend: a backend instance, or a class to instantiate with no
        arguments.
    :param segments: the already-split key the hook is asked for.
    :param options: the hierarchy entry's ``options``; none when omitted.
    :param context: the context passed to the hook;
        :meth:`LookupContext.for_testing() <hyera.LookupContext.for_testing>`
        when omitted.
    :returns: the hook's value, or :data:`NOT_FOUND` when it called
        ``context.not_found()``.
    :raises ConfigError: the backend does not implement ``data_dig``.
    :raises BackendError: the hook returned a value outside Puppet's data types.
    """
    backend = _instance(backend)
    _check_kind_implemented(backend, "data_dig")
    if context is None:
        context = LookupContext.for_testing()
    try:
        value = backend.data_dig(list(segments), dict(options or {}), context)
    except _NotFound:
        return NOT_FOUND
    except HieraError:
        raise
    except Exception as e:
        raise _hook_error(e, "data_dig", backend.name, None) from e
    return _validate_provider_value(value, "data_dig", backend.name, None)


def data_hash(
    backend: _ty.Union[Backend, _ty.Type[Backend]],
    path: _ty.Optional[str] = None,
    options: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    *,
    context: _ty.Optional[LookupContext] = None,
) -> _ty.Dict[str, _ty.Any]:
    """Call ``backend``'s ``data_hash`` hook as the engine does.

    The hook must return a hash whose values are Puppet data.

    :param backend: a backend instance, or a class to instantiate with no
        arguments.
    :param path: the location's file path, or ``None`` for a location-less
        entry.
    :param options: the hierarchy entry's ``options``; none when omitted. The
        hook receives them with ``path`` added, as the engine does for a located
        entry.
    :param context: the context passed to the hook;
        :meth:`LookupContext.for_testing() <hyera.LookupContext.for_testing>`
        when omitted.
    :returns: the hash, with tuples read as lists.
    :raises ConfigError: the backend does not implement ``data_hash``.
    :raises BackendError: the hook returned something other than a hash, or
        called ``context.not_found()``.
    :raises HieraLookupError: a value of the hash is outside Puppet's data
        types.
    """
    backend = _instance(backend)
    _check_kind_implemented(backend, "data_hash")
    if context is None:
        context = LookupContext.for_testing()
    label = None if path is None else str(path)
    merged = dict(options or {})
    if label is not None:
        merged["path"] = label
    try:
        data = backend.data_hash(path, merged, context)
    except _NotFound:
        raise _data_hash_not_found(backend.name, label) from None
    except HieraError:
        raise
    except Exception as e:
        raise _hook_error(e, "data_hash", backend.name, label) from e
    _validate_data_hash(data, backend.name, label)
    for key, value in data.items():
        validate_data_value(value, backend.name, label, key)
    return {key: _tuples_to_lists(value) for key, value in data.items()}


#: Bytes no text format reads: not UTF-8, and not any document.
_MALFORMED = b"\xff\xfe\x00\xd8 not a document \x80"

_MISSING_KEY = "hyera-contract-missing-key"

_HOOKS = ("data_hash", "lookup_key", "data_dig")


class _Miss:
    """A key the hook did not find."""


def _call_hook(
    backend: Backend,
    kind: str,
    key: str,
    options: _ty.Mapping[str, _ty.Any],
    path: _ty.Optional[str],
) -> _ty.Any:
    """What ``backend``'s ``kind`` hook returns for ``key`` with no engine check
    applied: the hash for ``data_hash``, a value or :class:`_Miss` otherwise."""
    context = LookupContext.for_testing()
    try:
        if kind == "data_hash":
            return backend.data_hash(path, dict(options), context)
        if kind == "lookup_key":
            return backend.lookup_key(key, dict(options), context)
        return backend.data_dig([key], dict(options), context)
    except _NotFound:
        return _Miss


class BackendContract:
    """Checks every backend must pass, as plain ``test_*`` methods.

    A test module subclasses it, names the class under test in :attr:`backend` and
    implements :meth:`write_source`; a runner such as pytest then collects the
    subclass. Nothing here imports pytest: a check asks for the ``tmp_path``
    fixture by parameter name and fails with ``assert``. Each check runs for every
    hook kind the backend implements (``data_hash``, ``lookup_key``, ``data_dig``);
    a kind the backend does not implement is left out.
    """

    #: The backend class under test.
    backend: _ty.ClassVar[_ty.Type[Backend]]

    #: The data a generated source holds, keyed by the keys the checks ask for.
    #: A backend whose format cannot hold some of these types overrides it.
    data: _ty.ClassVar[_ty.Mapping[str, _ty.Any]] = {
        "contract_text": "value",
        "contract_number": 7,
        "contract_flag": True,
        "contract_list": ["a", "b"],
        "contract_nested": {"key": "v"},
    }

    def write_source(
        self, directory: _ty.Any, data: _ty.Mapping[str, _ty.Any]
    ) -> _ty.Mapping[str, _ty.Any]:
        """Write a source holding ``data`` under ``directory``.

        :param directory: the directory to write into; it exists.
        :param data: the key/value pairs the source must hold.
        :returns: the level options for the source. A ``path`` is relative to
            ``directory``; the checks make it absolute, as the engine does.
        :raises NotImplementedError: the subclass did not implement it.
        """
        raise NotImplementedError(
            "{} must implement write_source()".format(type(self).__name__)
        )

    def write_malformed_source(
        self, directory: _ty.Any
    ) -> _ty.Optional[_ty.Mapping[str, _ty.Any]]:
        """Write a source the backend cannot read.

        The default writes bytes that are not a document in any text format over
        the file :meth:`write_source` made. A backend whose source is not a file
        the default can spoil overrides it.

        :param directory: the directory to write into.
        :returns: the level options for the source, or ``None`` when the source is
            not a file.
        """
        options = dict(self._write(directory, self.data))
        if "path" not in options:
            return None
        with open(os.path.join(str(directory), options["path"]), "wb") as fh:
            fh.write(_MALFORMED)
        return options

    # -- helpers -------------------------------------------------------

    def _write(
        self, directory: _ty.Any, data: _ty.Mapping[str, _ty.Any]
    ) -> _ty.Mapping[str, _ty.Any]:
        os.makedirs(str(directory), exist_ok=True)
        return self.write_source(directory, data)

    def _kinds(self) -> _ty.List[str]:
        return [kind for kind in _HOOKS if self.backend.implements(kind)]

    def _source(
        self, directory: _ty.Any, data: _ty.Mapping[str, _ty.Any]
    ) -> _ty.Tuple[_ty.Dict[str, _ty.Any], _ty.Optional[str]]:
        options = dict(self._write(directory, data))
        path = None
        if "path" in options:
            path = os.path.join(str(directory), options["path"])
            options["path"] = path
        return options, path

    def _values(
        self, kind: str, backend: Backend, options: _ty.Any, path: _ty.Any
    ) -> _ty.Dict[str, _ty.Any]:
        """The value each key of :attr:`data` gets from the hook, raw."""
        if kind == "data_hash":
            return dict(_call_hook(backend, kind, "", options, path))
        return {key: _call_hook(backend, kind, key, options, path) for key in self.data}

    def _expect_backend_error(self, kind: str, options: _ty.Any, path: _ty.Any) -> None:
        try:
            self._values(kind, self.backend(), options, path)
        except BackendError:
            return
        except Exception as e:
            raise AssertionError(
                "{}: raised {}, not BackendError".format(kind, type(e).__name__)
            ) from e
        raise AssertionError("{}: raised nothing".format(kind))

    # -- the checks ------------------------------------------------------

    def test_every_name_resolves_to_the_class(self) -> None:
        """Every plain name in ``NAMES`` is lowercase and the registry resolves it
        to the class.

        :raises AssertionError: a name is not lowercase or resolves elsewhere.
        """
        for kind, names in self.backend.NAMES.items():
            for name in names:
                if isinstance(name, str):
                    assert name == name.lower(), "{!r} is not lowercase".format(name)
                    assert (
                        Backend.find(name, kind) is self.backend
                    ), "{!r} ({} kind) does not resolve to {}".format(
                        name, kind, self.backend.__name__
                    )

    def test_implements_agrees_with_what_the_class_overrides(self) -> None:
        """``implements()`` answers from the methods the class overrides, and at
        least one hook kind is implemented.

        :raises AssertionError: ``implements()`` disagrees with the overrides, or
            no hook kind is implemented.
        """
        cls = self.backend
        expected = {
            "lookup_key": cls.lookup_key is not Backend.lookup_key,
            "data_dig": cls.data_dig is not Backend.data_dig,
            "data_hash": cls.data_hash is not Backend.data_hash
            or cls.loads is not Backend.loads,
        }
        for kind, overridden in expected.items():
            assert (
                cls.implements(kind) == overridden
            ), "implements({!r}) is {} but the class {} it".format(
                kind,
                cls.implements(kind),
                "overrides" if overridden else "does not override",
            )
        assert any(expected.values()), "no hook kind is implemented"

    def test_check_available_returns_or_raises_backend_error(self) -> None:
        """``check_available()`` returns, or raises :class:`~hyera.BackendError`.

        :raises AssertionError: it raised another type.
        """
        try:
            self.backend.check_available()
        except BackendError:
            pass

    def test_a_hook_returns_the_values_of_present_keys(self, tmp_path: _ty.Any) -> None:
        """Each hook returns the source's value for every key it holds.

        :param tmp_path: a fresh directory.
        :raises AssertionError: a value differs.
        """
        for kind in self._kinds():
            options, path = self._source(tmp_path / kind, self.data)
            got = self._values(kind, self.backend(), options, path)
            for key, value in self.data.items():
                assert got.get(key) == value, "{}: {!r} gave {!r}, not {!r}".format(
                    kind, key, got.get(key), value
                )

    def test_a_missing_key_is_not_found(self, tmp_path: _ty.Any) -> None:
        """A key the source does not hold is absent from a ``data_hash`` result,
        and a ``lookup_key`` or ``data_dig`` hook calls ``context.not_found()``
        for it.

        :param tmp_path: a fresh directory.
        :raises AssertionError: a missing key is found.
        """
        for kind in self._kinds():
            options, path = self._source(tmp_path / kind, self.data)
            backend = self.backend()
            if kind == "data_hash":
                found = _MISSING_KEY in _call_hook(backend, kind, "", options, path)
            else:
                got = _call_hook(backend, kind, _MISSING_KEY, options, path)
                found = got is not _Miss
            assert not found, "{}: a missing key was found".format(kind)

    def test_every_returned_value_is_puppet_data(self, tmp_path: _ty.Any) -> None:
        """Whatever a hook returns for the source is of a type Puppet data allows
        (a tuple counts as a list), the engine's own rule.

        :param tmp_path: a fresh directory.
        :raises AssertionError: a value is outside Puppet's data types.
        """
        accepts = _lookup_value_type().instance
        for kind in self._kinds():
            options, path = self._source(tmp_path / kind, self.data)
            got = self._values(kind, self.backend(), options, path)
            for key, value in got.items():
                assert value is _Miss or accepts(value), "{}: {!r} gave a {}".format(
                    kind, key, type(value).__name__
                )

    def test_a_malformed_source_raises_backend_error(self, tmp_path: _ty.Any) -> None:
        """A source the backend cannot read raises :class:`~hyera.BackendError`
        and no other exception type.

        :param tmp_path: a fresh directory.
        :raises AssertionError: another exception type, or none, was raised.
        """
        for kind in self._kinds():
            options = self.write_malformed_source(tmp_path / kind)
            if options is None:
                continue
            options = dict(options)
            path = os.path.join(str(tmp_path / kind), options["path"])
            options["path"] = path
            self._expect_backend_error(kind, options, path)

    def test_an_unreadable_source_raises_backend_error(self, tmp_path: _ty.Any) -> None:
        """A source that has been removed raises :class:`~hyera.BackendError` and
        no other exception type.

        :param tmp_path: a fresh directory.
        :raises AssertionError: another exception type, or none, was raised.
        """
        for kind in self._kinds():
            options, path = self._source(tmp_path / kind, self.data)
            if path is None:
                continue
            os.remove(path)
            self._expect_backend_error(kind, options, path)

    def test_a_lookup_through_hiera_returns_the_same_values(
        self, tmp_path: _ty.Any
    ) -> None:
        """A :class:`~hyera.Hiera` whose hierarchy names the backend's function
        returns the source's values. Applies to a backend that registers a plain
        ``function`` name.

        :param tmp_path: a fresh directory.
        :raises AssertionError: a lookup differs from the source.
        """
        function_names = self.backend.NAMES.get("function", ())
        names = [name for name in function_names if isinstance(name, str)]
        if not names:
            return
        for kind in self._kinds():
            root = tmp_path / kind
            options = dict(self._write(root / "data", self.data))
            level: _ty.Dict[str, _ty.Any] = {"name": "contract", kind: names[0]}
            if "path" in options:
                level["path"] = options.pop("path")
            if options:
                level["options"] = options
            config = {
                "version": 5,
                "defaults": {"datadir": "data"},
                "hierarchy": [level],
            }
            h = Hiera(config, base_path=str(root), backends=default_backends())
            for key, value in self.data.items():
                assert h.lookup(key) == value, "{}: lookup of {!r} differs".format(
                    kind, key
                )
