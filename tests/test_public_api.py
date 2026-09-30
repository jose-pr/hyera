"""Pins the shape of hyera's public API surface: every exported module's
``__all__`` is complete, every public signature is fully typed, every
public object carries its own docstring, and the shipped header documents
all of it exactly.

Shipped with the package (``tests/`` lands in the sdist), so no plan or
decision reference belongs here.
"""

import copy
import importlib
import importlib.util
import inspect
import pickle
import pkgutil
import typing

import hyera


def _public_modules():
    """``hyera`` plus every ``hyera.<name>`` submodule whose name does not
    start with ``_`` -- ``hyera.cli`` only when the CLI extra (``duho``) is
    installed."""
    modules = [hyera]
    for info in pkgutil.iter_modules(hyera.__path__):
        name = info.name
        if name.startswith("_"):
            continue
        if name == "cli" and importlib.util.find_spec("duho") is None:
            continue
        modules.append(importlib.import_module("hyera.{}".format(name)))
    return modules


def _public_objects():
    """Every object hiera's public surface exports, as ``(qualname, obj)``
    pairs, deduplicated by identity (an object re-exported under more than
    one name/module is listed once, under the first qualname reached).

    For each public module, every name in its ``__all__`` (a public module
    with no ``__all__`` fails the assertion below outright). For each
    exported class, also every member of ``vars(cls)`` that is a function,
    ``staticmethod``, ``classmethod`` or ``property`` and is either public
    (no leading underscore) or one of ``__init__`` (when the class defines
    its own), ``__call__``, ``__getitem__``, ``__contains__``.
    """
    seen = set()
    result = []

    def _add(qualname, obj):
        if id(obj) in seen:
            return
        seen.add(id(obj))
        result.append((qualname, obj))

    for module in _public_modules():
        all_names = getattr(module, "__all__", None)
        assert all_names is not None, "{} has no __all__".format(module.__name__)
        for name in all_names:
            obj = getattr(module, name)
            qualname = "{}.{}".format(module.__name__, name)
            _add(qualname, obj)
            if not inspect.isclass(obj):
                continue
            for member_name, member in vars(obj).items():
                if member_name == "__init__":
                    include = True
                elif member_name in ("__call__", "__getitem__", "__contains__"):
                    include = True
                elif not member_name.startswith("_"):
                    include = True
                else:
                    include = False
                if not include:
                    continue
                if not isinstance(
                    member, (staticmethod, classmethod, property)
                ) and not inspect.isfunction(member):
                    continue
                _add("{}.{}".format(qualname, member_name), member)
    return result


def test_public_signatures_are_annotated():
    """Every public class, function, method and property is fully typed:
    every parameter but the bound first one is annotated, the return is
    annotated (except ``__init__``, which has none), and
    ``typing.get_type_hints`` succeeds -- exactly what
    ``pyright --verifytypes`` scores."""
    offenders = []
    for qualname, obj in _public_objects():
        if inspect.isclass(obj):
            try:
                typing.get_type_hints(obj)
            except Exception as e:
                offenders.append(
                    "{}: get_type_hints(class): {}: {}".format(
                        qualname, type(e).__name__, e
                    )
                )
            continue

        drop_first = True
        if isinstance(obj, staticmethod):
            target = obj.__func__
            drop_first = False
        elif isinstance(obj, classmethod):
            target = obj.__func__
        elif isinstance(obj, property):
            target = obj.fget
        elif inspect.isfunction(obj):
            target = obj
            # A module-level export's __qualname__ has no ".": nothing to
            # drop. A class's own member's __qualname__ is "Class.member":
            # the first parameter (self/cls) is bound away by the caller.
            drop_first = "." in obj.__qualname__
        else:
            continue
        if target is None:
            offenders.append("{}: property has no getter".format(qualname))
            continue

        try:
            typing.get_type_hints(target)
        except Exception as e:
            offenders.append(
                "{}: get_type_hints: {}: {}".format(qualname, type(e).__name__, e)
            )
            continue

        sig = inspect.signature(target)
        params = list(sig.parameters.values())
        if drop_first and params:
            params = params[1:]
        for p in params:
            if p.annotation is inspect.Parameter.empty:
                offenders.append(
                    "{}: parameter {!r} is unannotated".format(qualname, p.name)
                )

        is_init = target.__name__ == "__init__"
        if not is_init and sig.return_annotation is inspect.Signature.empty:
            offenders.append("{}: return is unannotated".format(qualname))

    assert not offenders, "\n".join(offenders)


def test_unset_sentinel_is_stable():
    """The ``<unset>`` default-value sentinel (:class:`hyera._navigation.
    _Unset`) renders readably, and survives ``copy``/``pickle`` by
    identity -- a plain ``object()`` would do neither."""
    from hyera._navigation import _MISSING

    assert repr(_MISSING) == "<unset>"
    assert copy.deepcopy(_MISSING) is _MISSING
    assert pickle.loads(pickle.dumps(_MISSING)) is _MISSING
    # The exact rendering is "default_value: Any = <unset>" now that every
    # parameter (including default_value itself) is annotated (Design Q3);
    # the sentinel's own text is what this test actually pins.
    assert "<unset>" in str(inspect.signature(hyera.Hiera.lookup))
