"""Every function's annotations resolve at run time, on the oldest Python too."""

import importlib
import inspect
import pkgutil
import sys
import typing

import pytest

import hyera

_SKIPPED_MODULES = {"hyera.__main__", "hyera.cli.__main__"}


def _module_names():
    names = ["hyera"]
    for info in pkgutil.walk_packages(hyera.__path__, "hyera."):
        if info.name not in _SKIPPED_MODULES:
            names.append(info.name)
    return sorted(names)


def _functions(module):
    """Every function and method defined in ``module`` itself."""
    seen = set()

    def own(obj):
        return getattr(obj, "__module__", None) == module.__name__

    stack = list(vars(module).values())
    while stack:
        obj = stack.pop()
        if isinstance(obj, (staticmethod, classmethod)):
            obj = obj.__func__
        elif isinstance(obj, property):
            stack.extend(f for f in (obj.fget, obj.fset, obj.fdel) if f)
            continue
        if id(obj) in seen:
            continue
        if inspect.isfunction(obj) and own(obj):
            seen.add(id(obj))
            yield obj
        elif inspect.isclass(obj) and own(obj):
            seen.add(id(obj))
            stack.extend(vars(obj).values())


def _type_checking_names():
    """Every class a ``TYPE_CHECKING``-only import could name."""
    names = {}
    for name in _module_names():
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        for attr, value in vars(module).items():
            if inspect.isclass(value) and value.__module__.startswith("hyera"):
                names.setdefault(attr, value)
    return names


@pytest.mark.parametrize("name", _module_names())
def test_annotations_resolve(name):
    try:
        module = importlib.import_module(name)
    except ImportError as e:
        pytest.skip("optional dependency missing: {}".format(e))
    namespace = dict(_type_checking_names())
    namespace.update(vars(module))
    unresolved = []
    for func in _functions(module):
        try:
            typing.get_type_hints(func, globalns=namespace)
        except Exception as e:
            unresolved.append(
                "{}.{}: {}: {}".format(name, func.__qualname__, type(e).__name__, e)
            )
    assert not unresolved, "\n".join(unresolved)


def test_every_module_was_checked():
    assert len(_module_names()) > 30, sys.version
