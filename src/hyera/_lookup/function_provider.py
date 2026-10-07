# Ported from Puppet 8 lib/puppet/pops/lookup/{function_provider,
# data_hash_function_provider,lookup_key_function_provider,data_dig_function_provider,
# context,data_provider,configured_data_provider}.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr. See NOTICE.
"""Function providers: dispatching a hierarchy level's ``data_hash``,
``lookup_key`` or ``data_dig`` function per key and per location, with a
:class:`LookupContext` for the ``lookup_key``/``data_dig`` cases.

Follows Puppet's ``pops/lookup/function_provider.rb``,
``data_hash_function_provider.rb``, ``lookup_key_function_provider.rb``,
``data_dig_function_provider.rb``, ``context.rb``, ``data_provider.rb`` and
``configured_data_provider.rb``.
"""

from __future__ import annotations

import contextlib
import os
import typing as _ty

from .._output.explain_refs import _LocationRef
from .._scope.scope import Scope
from .interpolation import interpolate
from .invocation import Invocation
from .navigation import _MISSING, parse_lookup_key, sub_lookup
from ..exceptions import BackendError, ConfigError, HieraError

__all__ = ["LookupContext", "PROVIDER_CLASSES"]

# : The shared no-op context manager for a location-less entry (``locations is None``):
# : Puppet's function providers call their function with no location there, so none is
# pushed.
_NULL_CONTEXT = contextlib.nullcontext()


def _location_ref(location) -> _LocationRef:
    """A :class:`~hyera._output.explain_refs._LocationRef` for one resolved location
    (``locations._Location``/``_location_resolver.ResolvedLocation`` -- both
    ``(original, location, is_uri, exist)``-shaped). The path form always
    renders with ``/`` separators, matching what Ruby's ``Pathname`` prints on
    every OS, regardless of this interned string's own separator.
    """
    if location.is_uri:
        return _LocationRef(location.original, location.location, "uri")
    posix = str(location.location).replace(os.sep, "/")
    return _LocationRef(location.original, posix, "path")


def _recording_location(invocation, location):
    """``invocation.recording("location", ...)`` for a real location, else
    the shared no-op -- factored out since all three provider kinds need it
    identically."""
    if location is None or invocation.explainer is None:
        return _NULL_CONTEXT
    return invocation.recording("location", _location_ref(location))


def _kind_mismatch_text(backend, func_name: str, kind: str) -> str:
    """Puppet's function-arity/parameter-type text when a hierarchy level
    names a function that does not implement the kind it is used as
    (``lookup_key_function_provider.rb``/``data_dig_function_provider.rb``/
    ``data_hash_function_provider.rb``'s own dispatch by arity). Puppet
    raises this only when the function is actually invoked for a location
    that exists (or, for a location-less entry, whenever invoked) -- never
    at config-build time, so a kind-mismatched level whose location does
    not exist still lets every other level answer, matching Puppet rather
    than refusing the whole instance. Only called when ``not
    backend.implements(kind)``.
    """
    has_dh = backend.implements("data_hash")
    has_lk = backend.implements("lookup_key")
    has_dd = backend.implements("data_dig")
    if not (has_dh or has_lk or has_dd):
        return "'{}' implements none of data_hash, lookup_key or data_dig".format(
            func_name
        )
    if kind == "data_hash":
        return "'{}' expects 3 arguments, got 2".format(func_name)
    if has_dh:
        return "'{}' expects 2 arguments, got 3".format(func_name)
    # Only "data_dig" and "lookup_key" remain for `kind`, has_dh is false and one of
    # has_lk/has_dd is true, so exactly one is: whichever `kind` is not. Neither
    # branch's condition can be false here, so no fallback is needed.
    if kind == "data_dig":
        return "'{}' parameter 'key' expects a String value, got Tuple".format(
            func_name
        )
    return "'{}' parameter 'key_segments' expects an Array value, got String".format(
        func_name
    )


def _check_kind_implemented(backend, kind: str) -> None:
    """Raise the level's "Unable to find ... function" error if its function
    name did not resolve, else Puppet's kind-mismatch error
    (:func:`_kind_mismatch_text`) if the function does not implement
    ``kind``, the moment the function is actually invoked. Called from each
    provider's ``key_lookup``, after the
    per-location existence gate (or unconditionally for a location-less
    entry), so a location that does not exist never reaches this and never
    refuses the rest of the ``Hiera`` instance. ``backend.name`` is the
    function name as declared (``Backend.new`` sets it to the name actually
    asked for), not the hierarchy level's own ``name``."""
    unknown = getattr(backend, "unknown_function_error", None)
    if unknown is not None:
        raise ConfigError(*unknown.args, path=unknown.path, line=unknown.line) from None
    if not backend.implements(kind):
        raise ConfigError(_kind_mismatch_text(backend, backend.name, kind))


class _NotFound(BaseException):
    """Puppet's ``throw :no_such_key`` (``context.rb``'s ``not_found``): a
    control-flow signal, not an application error. Deriving from
    :class:`BaseException` instead of :class:`Exception` means a backend's
    own ``except Exception`` (or bare ``except:`` written as one) does not
    accidentally swallow it, mirroring Ruby ``throw``/``catch``, which a
    plain ``rescue => e`` never catches either.
    """


def _puppet_type_label(value) -> str:
    """The Puppet type name ``data_provider.rb``'s Hash check would report
    for a non-Hash ``data_hash`` result (Puppet 8.10.0, ``--strict warning``,
    on JSON's seven possible top-level shapes; the same labels apply to any
    backend's non-dict result)."""
    if isinstance(value, bool):  # bool before int: bool is an int subclass.
        return "Boolean"
    if value is None:
        return "Undef"
    if isinstance(value, str):
        return "String"
    if isinstance(value, int):
        return "Integer"
    if isinstance(value, float):
        return "Float"
    if isinstance(value, list):
        return "Tuple" if value else "Array"
    return type(value).__name__


def _validate_data_hash(data, name, location) -> None:
    """Puppet's Hash check on a ``data_hash`` result
    (``data_hash_function_provider.rb:56-76`` + ``data_provider.rb:76-91``),
    applied here so every backend -- third-party ones included -- gets it.
    ``location`` is ``None`` for a location-less entry."""
    if isinstance(data, dict):
        return
    if location is None:
        raise BackendError(
            "Value returned from data_hash function '{}' has wrong type, "
            "expects a Hash value, got {}".format(name, _puppet_type_label(data))
        )
    raise BackendError(
        "Value returned from data_hash function '{}', when using location "
        "'{}', has wrong type, expects a Hash value, got {}".format(
            name, location, _puppet_type_label(data)
        ),
        path=str(location),
    )


def _data_hash_not_found(name, location) -> BackendError:
    """The error for a ``data_hash`` hook that called ``context.not_found()``,
    which only ``lookup_key`` and ``data_dig`` hooks may do. ``location`` is
    ``None`` for a location-less entry."""
    text = (
        "data_hash function '{}'{} called context.not_found(); only a lookup_key "
        "or data_dig function may signal a miss".format(
            name,
            "" if location is None else ", when using location '{}',".format(location),
        )
    )
    return BackendError(text, path=None if location is None else str(location))


def _hook_error(exc, hook, name, location) -> BackendError:
    """The ``BackendError`` for an exception of a class outside the package
    raised inside a backend's ``hook``. The message carries the class name
    only: the text may hold document content. ``location`` is ``None`` for a
    location-less entry."""
    text = "{} function '{}'{} raised {}".format(
        hook,
        name,
        "" if location is None else ", when using location '{}',".format(location),
        type(exc).__name__,
    )
    return BackendError(text, path=None if location is None else str(location))


def _tuples_to_lists(value):
    """``value`` with every tuple, at any depth, replaced by a list; a value
    holding no tuple is returned as is."""
    if isinstance(value, tuple):
        return [_tuples_to_lists(item) for item in value]
    if isinstance(value, list):
        items = [_tuples_to_lists(item) for item in value]
        return value if all(a is b for a, b in zip(items, value)) else items
    if isinstance(value, dict):
        items = {k: _tuples_to_lists(v) for k, v in value.items()}
        return value if all(items[k] is v for k, v in value.items()) else items
    return value


def _validate_provider_value(value, kind, name, location):
    """The ``lookup_key``/``data_dig`` value check
    (``{lookup_key,data_dig}_function_provider.rb``'s own ``assert_value_type``,
    the same RichData rule as :func:`~hyera._lookup.lookup_adapter.validate_data_value`
    but worded for a scalar return rather than a hash entry). Returns the
    value with any tuple read as a list."""
    from .lookup_adapter import _lookup_value_type, value_type_label

    t = _lookup_value_type()
    if t.instance(value):
        return _tuples_to_lists(value)
    if location is None:
        raise BackendError(
            "Value returned from {} function '{}' has wrong type, expects "
            "Puppet::LookupValue, got {}".format(kind, name, value_type_label(value))
        )
    raise BackendError(
        "Value returned from {} function '{}', when using location '{}', "
        "has wrong type, expects Puppet::LookupValue, got {}".format(
            kind, name, location, value_type_label(value)
        ),
        path=str(location),
    )


class _EnvironmentContext:
    """Per-``Hiera``-instance file cache, shared by every view
    (``context.rb:48-58``'s ``cached_file_data``): revalidated by
    ``(inode, mtime_ns, size)``, not by content.
    """

    def __init__(self) -> None:
        self._cache: _ty.Dict[str, _ty.Tuple[_ty.Any, _ty.Any]] = {}

    def clear(self) -> None:
        """Drop every cached file (``Hiera.clear_cache()``)."""
        self._cache.clear()

    def cached_file_data(
        self,
        path: _ty.Union[str, "os.PathLike[str]"],
        parse: _ty.Optional[_ty.Callable[[str], _ty.Any]] = None,
    ) -> _ty.Any:
        """The cached result of ``parse(text)`` (or the raw text when
        ``parse`` is ``None``) for the file at ``path``, revalidated by
        ``(inode, mtime_ns, size)``, not by content."""
        return self.cached_file_stamped(path, parse)[1]

    def cached_file_stamped(
        self,
        path: _ty.Union[str, "os.PathLike[str]"],
        parse: _ty.Optional[_ty.Callable[[str], _ty.Any]] = None,
    ) -> _ty.Tuple[_ty.Tuple[int, int, int], _ty.Any]:
        """:meth:`cached_file_data`'s value together with the
        ``(inode, mtime_ns, size)`` stamp it was validated against."""
        path = os.fspath(path)
        try:
            st = os.stat(path)
        except OSError as e:
            raise BackendError(
                "Unable to read ({}): {}".format(path, e.strerror or e), path=path
            ) from e
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == stamp:
            return stamp, cached[1]
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            # Same shape as the stat branch above (a directory at `path`, a permission
            # error): Puppet leaves this one raw, but the sibling branch wraps its
            # failures, so this stays consistent with it.
            raise BackendError(
                "Unable to read ({}): {}".format(path, e.strerror or e), path=path
            ) from e
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise BackendError(
                "Unable to parse ({}): {}".format(path, e), path=path
            ) from e
        try:
            value = parse(text) if parse is not None else text
        except BackendError as e:
            if e.path is None:
                raise BackendError(
                    "Unable to parse ({}): {}".format(path, e), path=path
                ) from e
            raise
        self._cache[path] = (stamp, value)
        return stamp, value


class _Result(_ty.NamedTuple):
    """One ``lookup_key``/``data_dig`` result the engine kept: ``value`` is
    what the hook returned; ``deps`` is ``(path, stamp)`` for each file the
    hook read through ``cached_file_data`` while producing it; ``serial``
    identifies the top-level lookup that produced it."""

    value: _ty.Any
    deps: tuple
    serial: int


class _FunctionContext:
    """Puppet's per-hierarchy-entry ``FunctionContext``
    (``context.rb:61-117``), scoped to one scope-binding object (one
    ``Hiera``/``h.scoped(...)`` view) and one location within its level."""

    def __init__(
        self,
        environment_context: _EnvironmentContext,
        environment_name: _ty.Optional[str],
        module_name: _ty.Optional[str] = None,
    ) -> None:
        self.environment_context = environment_context
        self.environment_name = environment_name
        self.module_name = module_name
        #: Filled once per location by a ``data_hash`` provider.
        self.data_hash: _ty.Optional[_ty.Dict[str, _ty.Any]] = None
        #: The location label a ``data_hash`` provider validated ``data_hash``
        #: with -- ``None`` for a location-less entry, else ``str(location)``.
        self.label: _ty.Optional[str] = None
        #: What the hook stored through ``LookupContext.cache``; the engine
        #: never reads or writes it.
        self._cache: _ty.Dict[_ty.Any, _ty.Any] = {}
        #: The engine's own per-key results (:class:`_Result`), apart from
        #: the hook's cache.
        self.results: _ty.Dict[_ty.Any, _Result] = {}


class LookupContext:
    """The ``context`` argument handed to a ``lookup_key``/``data_dig``
    backend hook (Puppet's public ``Context`` API, ``context.rb:126-206``).

    Built by the engine for each call; a backend never constructs one
    itself. A test builds one with :meth:`for_testing`.

    :param function_context: the per-location state to read/write through.
    :param invocation: the current lookup's per-lookup state.
    """

    def __init__(
        self, function_context: _FunctionContext, invocation: Invocation
    ) -> None:
        self._fc = function_context
        self._invocation = invocation
        #: ``(path, stamp)`` of every file read through
        #: :meth:`cached_file_data` by this call.
        self._deps: _ty.List[_ty.Tuple[str, _ty.Any]] = []

    @classmethod
    def for_testing(
        cls,
        *,
        scope: _ty.Optional[Scope] = None,
        module_name: _ty.Optional[str] = None,
        data: _ty.Optional[_ty.Mapping[str, _ty.Any]] = None,
    ) -> "LookupContext":
        """A context to call a hook with outside a lookup.

        :meth:`cache`, :meth:`cached_file_data` and :meth:`explain` behave as
        in a lookup, except that :meth:`explain` never calls its producer.

        :param scope: what :meth:`interpolate` reads variables from, and the
            source of :attr:`environment_name`; an empty :class:`~hyera.Scope`
            when omitted.
        :param module_name: the value of :attr:`module_name`.
        :param data: the keys ``lookup()``, ``alias()`` and ``hiera()`` resolve
            in :meth:`interpolate`, by dotted navigation; a key absent from it
            is a miss, as in a lookup. No keys when omitted.
        :returns: the new context.
        """
        scope = Scope() if scope is None else scope
        keys = {} if data is None else data

        def lookup(key, invocation):
            root, segments = parse_lookup_key(key)
            if root not in keys:
                return _MISSING
            if not segments:
                return keys[root]
            return sub_lookup(key, segments, keys[root])

        function_context = _FunctionContext(
            _EnvironmentContext(), scope.environment, module_name
        )
        return cls(function_context, Invocation(scope, lookup))

    def interpolate(self, value: _ty.Any) -> _ty.Any:
        """Interpolate ``value`` (methods allowed) against the current
        lookup's scope -- a backend calls this itself; the engine never
        interpolates a ``lookup_key``/``data_dig`` result on its own.

        :param value: the value (or nested structure) to interpolate.
        :returns: the interpolated result.
        """
        return interpolate(value, self._invocation, allow_methods=True)

    def not_found(self) -> "_ty.NoReturn":
        """Signal a miss for this location -- Puppet's ``throw :no_such_key``.

        :raises Exception: always; the raised object is an internal
            control-flow signal, not a documented public exception.
        """
        raise _NotFound()

    def explain(self, producer: _ty.Callable[[], str]) -> None:
        """Add ``producer``'s text to this lookup's ``explain()`` report.

        :param producer: a zero-argument callable producing the text.
        """
        self._invocation.report_text(producer)

    def cache(self, key: _ty.Any, value: _ty.Any) -> _ty.Any:
        """Cache ``value`` under ``key`` for this location, for the life of
        the owning ``Hiera``/``h.scoped(...)`` view. Returns ``value``.

        :param key: the cache key.
        :param value: the value to cache.
        :returns: ``value``, unchanged.
        """
        self._fc._cache[key] = value
        return value

    def cache_all(self, mapping: _ty.Mapping[_ty.Any, _ty.Any]) -> None:
        """:meth:`cache` every key/value pair of ``mapping``.

        :param mapping: the key/value pairs to cache.
        """
        self._fc._cache.update(mapping)

    def cache_has_key(self, key: _ty.Any) -> bool:
        """Whether ``key`` was already :meth:`cache`\\ d for this location.

        :param key: the cache key.
        :returns: whether ``key`` is cached.
        """
        return key in self._fc._cache

    def cached_value(self, key: _ty.Any) -> _ty.Any:
        """The value :meth:`cache`\\ d under ``key``, or ``None``.

        :param key: the cache key.
        :returns: the cached value, or ``None``.
        """
        return self._fc._cache.get(key)

    def cached_entries(self) -> "_ty.Iterator[_ty.Tuple[_ty.Any, _ty.Any]]":
        """An iterator over every ``(key, value)`` pair :meth:`cache`\\ d
        for this location.

        :returns: an iterator of ``(key, value)`` pairs.
        """
        return iter(list(self._fc._cache.items()))

    def cached_file_data(
        self,
        path: _ty.Union[str, "os.PathLike[str]"],
        parse: _ty.Optional[_ty.Callable[[str], _ty.Any]] = None,
    ) -> _ty.Any:
        """The cached result of ``parse(text)`` (or the raw text when
        ``parse`` is ``None``) for the file at ``path``, shared with every
        other location of this ``Hiera`` instance (see
        :class:`_EnvironmentContext`).

        :param path: the file to read.
        :param parse: applied to the file's text; identity when omitted.
        :returns: the (cached) parsed result, or raw text.
        :raises BackendError: ``path`` could not be read, decoded as UTF-8,
            or ``parse`` raised a :class:`BackendError` of its own.
        """
        stamp, value = self._fc.environment_context.cached_file_stamped(path, parse)
        self._deps.append((os.fspath(path), stamp))
        return value

    @property
    def environment_name(self) -> _ty.Optional[str]:
        """The current lookup's environment name, or ``None``."""
        return self._fc.environment_name

    @property
    def module_name(self) -> _ty.Optional[str]:
        """The current hierarchy entry's module name, or ``None`` at the
        global/environment layer."""
        return self._fc.module_name
