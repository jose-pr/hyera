"""Pins the shape of hyera's public API surface: every exported module's
``__all__`` is complete, every public signature is fully typed, every
public object carries its own docstring, and the shipped header documents
all of it exactly.

Shipped with the package (``tests/`` lands in the sdist), so no plan or
decision reference belongs here.
"""

import ast
import builtins
import copy
import importlib
import importlib.util
import inspect
import os
import pickle
import pkgutil
import re
import typing
from pathlib import Path

import hyera


def _target_and_drop_first(obj):
    """``(target, drop_first)`` for one ``_public_objects()`` entry:
    ``target`` is the plain function to introspect (a ``staticmethod``/
    ``classmethod``/``property`` unwrapped), ``drop_first`` is whether its
    first parameter is a bound ``self``/``cls`` to skip. ``(None, False)``
    for anything that is not a callable member (a class, a type alias, a
    constant, a property with no getter)."""
    if isinstance(obj, staticmethod):
        return obj.__func__, False
    if isinstance(obj, classmethod):
        return obj.__func__, True
    if isinstance(obj, property):
        return obj.fget, True
    if inspect.isfunction(obj):
        # A module-level export's __qualname__ has no ".": nothing to
        # drop. A class's own member's __qualname__ is "Class.member": the
        # first parameter (self/cls) is bound away by the caller.
        return obj, "." in obj.__qualname__
    return None, False


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

        target, drop_first = _target_and_drop_first(obj)
        if target is None:
            if isinstance(obj, property):
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
    """The ``<unset>`` default-value sentinel (:class:`hyera._lookup.navigation.
    _Unset`) renders readably, and survives ``copy``/``pickle`` by
    identity -- a plain ``object()`` would do neither."""
    from hyera._lookup.navigation import _MISSING

    assert repr(_MISSING) == "<unset>"
    assert copy.deepcopy(_MISSING) is _MISSING
    assert pickle.loads(pickle.dumps(_MISSING)) is _MISSING
    # The exact rendering is "default_value: Any = <unset>" now that every
    # parameter (including default_value itself) is annotated; the
    # sentinel's own text is what this test actually pins.
    assert "<unset>" in str(inspect.signature(hyera.Hiera.lookup))


def test_every_module_has_a_docstring():
    """Every ``*.py`` in the installed package -- public or private --
    has a module docstring, read with ``ast`` so a syntax problem never
    masquerades as a missing one."""
    offenders = []
    package_dir = Path(hyera.__file__).parent
    for path in sorted(package_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if not ast.get_docstring(ast.parse(text, filename=str(path))):
            offenders.append(path.name)
    assert not offenders, offenders


def _is_documentable_member(obj):
    """Whether a ``_public_objects()`` entry is a class, function, method
    or property -- as opposed to a type alias or a constant, which carry
    no docstring of their own and are skipped by the docstring tests."""
    return (
        inspect.isclass(obj)
        or inspect.isfunction(obj)
        or isinstance(obj, (staticmethod, classmethod, property))
    )


def test_public_objects_have_docstrings():
    """Every public class, function, method and property has its own,
    non-blank docstring (never one merely inherited from a base class) --
    what ``help()`` and the API reference both read. ``__init__`` is
    exempt: its owning class's docstring documents the constructor
    instead, and that class is checked in its own right."""
    offenders = []
    for qualname, obj in _public_objects():
        if not _is_documentable_member(obj):
            continue
        if inspect.isclass(obj):
            doc = obj.__doc__
        elif isinstance(obj, property):
            doc = obj.__doc__ or (obj.fget.__doc__ if obj.fget else None)
        else:
            target, _drop_first = _target_and_drop_first(obj)
            if target is not None and target.__name__ == "__init__":
                continue
            doc = target.__doc__ if target is not None else None
        if not doc or not doc.strip():
            offenders.append(qualname)
    assert not offenders, offenders


#: A ``:param name:``/``:raises Name:`` Sphinx field line. ``:returns:``
#: takes no name. Matches the one field style this project uses: no
#: ``:type:``/``:rtype:`` fields, since the annotations already carry
#: the types.
_PARAM_RE = re.compile(r"^\s*:param\s+([A-Za-z_][A-Za-z0-9_]*):", re.MULTILINE)
_RETURNS_RE = re.compile(r"^\s*:returns:", re.MULTILINE)
_RAISES_RE = re.compile(r"^\s*:raises\s+([A-Za-z_][A-Za-z0-9_.]*):", re.MULTILINE)


def test_docstring_fields_match_signatures():
    """Every ``:param:`` name matches a real parameter, every documented
    return matches a real return annotation, and every ``:raises:`` name
    resolves to a real exception class importable from ``hyera`` or
    ``builtins`` -- a docstring that drifts from the signature it
    describes is worse than none."""
    offenders = []
    for qualname, obj in _public_objects():
        if isinstance(obj, property):
            continue  # a property's docstring is one line, no fields.

        if inspect.isclass(obj):
            if "__init__" not in vars(obj):
                continue  # nothing of its own to match a signature against.
            doc = obj.__doc__ or ""
            sig = inspect.signature(obj)
            param_names = {p.name for p in sig.parameters.values()}
            has_returns = False  # a constructor returns the instance implicitly.
        elif _is_documentable_member(obj):
            target, drop_first = _target_and_drop_first(obj)
            if target is None:
                continue
            if target.__name__ == "__init__":
                # __init__ needs no docstring of its own -- the owning
                # class's docstring documents the constructor (already
                # checked above, when the class itself was the
                # _public_objects() entry).
                continue
            doc = target.__doc__ or ""
            sig = inspect.signature(target)
            params = list(sig.parameters.values())
            if drop_first and params:
                params = params[1:]
            param_names = {p.name for p in params}
            has_returns = (
                target.__name__ != "__init__"
                and sig.return_annotation is not None
                and sig.return_annotation is not inspect.Signature.empty
                # A function that never returns (-> NoReturn) has no
                # return value to document.
                and "NoReturn" not in str(sig.return_annotation)
            )
        else:
            continue

        documented_params = set(_PARAM_RE.findall(doc))
        if documented_params != param_names:
            offenders.append(
                "{}: :param: {} != parameters {}".format(
                    qualname, sorted(documented_params), sorted(param_names)
                )
            )

        documents_returns = bool(_RETURNS_RE.search(doc))
        if documents_returns != has_returns:
            offenders.append(
                "{}: :returns: present={}, expected={}".format(
                    qualname, documents_returns, has_returns
                )
            )

        for name in _RAISES_RE.findall(doc):
            resolved = getattr(hyera, name, None) or getattr(builtins, name, None)
            if not (inspect.isclass(resolved) and issubclass(resolved, BaseException)):
                offenders.append(
                    "{}: :raises {}: does not resolve".format(qualname, name)
                )

    assert not offenders, "\n".join(offenders)


def _header_text():
    return Path(hyera.__file__).with_name("AGENTS.md").read_text(encoding="utf-8")


def _render(obj, drop_first=False):
    """``inspect.signature(obj)`` with every annotation (parameter and
    return) dropped, and the bound first parameter removed when
    ``drop_first``."""
    sig = inspect.signature(obj)
    params = list(sig.parameters.values())
    if drop_first and params:
        params = params[1:]
    params = [p.replace(annotation=inspect.Parameter.empty) for p in params]
    sig = sig.replace(parameters=params, return_annotation=inspect.Signature.empty)
    return str(sig)


def test_header_documents_every_export():
    """The shipped header (``src/hyera/AGENTS.md``) documents every
    exported name's exact signature (or, for a non-callable export, its
    bare name), every exported class's own public members, and every
    registered backend name -- so a consuming agent can skip the source."""
    header = _header_text()
    offenders = []

    for module in _public_modules():
        for name in module.__all__:
            obj = getattr(module, name)
            if inspect.isclass(obj):
                if "__init__" in vars(obj):
                    rendered = name + _render(obj)
                    if rendered not in header:
                        offenders.append(
                            "class {}: missing {!r}".format(name, rendered)
                        )
                elif "`{}`".format(name) not in header:
                    offenders.append("class {}: missing `{}`".format(name, name))
            elif inspect.isfunction(obj):
                rendered = name + _render(obj)
                if rendered not in header:
                    offenders.append("function {}: missing {!r}".format(name, rendered))
            elif "`{}`".format(name) not in header:
                offenders.append("{}: missing `{}`".format(name, name))

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
                if not include or member_name == "__init__":
                    continue
                if isinstance(member, property):
                    needle = ".{}".format(member_name)
                elif isinstance(member, (staticmethod, classmethod)):
                    needle = ".{}{}".format(
                        member_name, _render(getattr(obj, member_name))
                    )
                elif inspect.isfunction(member):
                    needle = ".{}{}".format(
                        member_name, _render(member, drop_first=True)
                    )
                else:
                    continue
                if needle not in header:
                    offenders.append(
                        "{}.{}: missing {!r}".format(name, member_name, needle)
                    )

    from hyera.backends import Backend

    for kind in Backend.KINDS:
        for backend_name in Backend.names(kind):
            needle = "`{}`".format(backend_name)
            if needle not in header:
                offenders.append(
                    "Backend.names({!r}): missing {!r}".format(kind, needle)
                )

    assert not offenders, "\n".join(offenders)


def test_header_lists_every_marker():
    """Every ``deviation``/``divergence`` id a conformance case carries has
    its own line in the header's "Differences from Puppet" section -- a
    later plan that adds or removes a marker must edit the header."""
    import yaml

    cases_dir = (
        Path(hyera.__file__).resolve().parents[2] / "tests" / "conformance" / "cases"
    )
    deviation_ids = set()
    gap_ids = set()

    def _ids(value):
        if isinstance(value, str):
            return {value}
        if isinstance(value, dict) and "id" in value:
            return {value["id"]}
        if isinstance(value, list):
            out = set()
            for item in value:
                out |= _ids(item)
            return out
        return set()

    for case_file in sorted(cases_dir.glob("*/case.yaml")):
        data = yaml.safe_load(case_file.read_text(encoding="utf-8"))
        if not data:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if "deviation" in node:
                    deviation_ids |= _ids(node["deviation"])
                if "divergence" in node:
                    gap_ids |= _ids(node["divergence"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    header = _header_text()
    header_deviations = set(
        re.findall(r"^- \*\*deviation\*\* `([^`]+)`", header, re.MULTILINE)
    )
    header_gaps = set(re.findall(r"^- \*\*gap\*\* `([^`]+)`", header, re.MULTILINE))

    assert header_deviations == deviation_ids
    assert header_gaps == gap_ids


def test_header_lists_env_vars():
    """Every literal environment-variable name ``hyera``'s own source reads
    (``os.environ``/``os.getenv``) appears in the header's "Environment"
    section."""
    pattern = re.compile(
        r"""(?:environ(?:\.get)?\(|environ\[|getenv\()\s*["']([A-Z][A-Z0-9_]*)["']"""
    )
    package_dir = Path(hyera.__file__).parent
    names = set()
    for path in package_dir.glob("*.py"):
        names |= set(pattern.findall(path.read_text(encoding="utf-8")))

    header = _header_text()
    env_start = header.index("## Environment")
    env_end = header.index("## Differences from Puppet")
    env_section = header[env_start:env_end]

    missing = [n for n in names if n not in env_section]
    assert not missing, missing


def test_header_is_self_contained():
    """The header ships inside the wheel, where an installed consumer has
    no repo: no relative Markdown link, no private-working-tree path, no
    bare decision id, no ``src/`` reference."""
    header = _header_text()
    offenders = []

    for target in re.findall(r"\]\(([^)]+)\)", header):
        if not target.startswith("http"):
            offenders.append("relative link: {!r}".format(target))

    agents_needle = "." + "agents"
    if agents_needle in header:
        offenders.append("contains a " + agents_needle + " path")

    if re.search(r"\bD\d{2}\b", header):
        offenders.append("contains a bare decision id")

    if "src/" in header:
        offenders.append("contains a src/ reference")

    assert not offenders, offenders


def test_hiera_level_new_is_usable_with_a_minimal_entry():
    level = hyera.HieraLevel.new({"name": "n", "path": "x.yaml"}, hyera.YAMLBackend())
    assert level.datadir == "data"
    assert level.locations == ("x.yaml",)
    assert level.kind == "data_hash"


def test_hiera_level_hashes_with_options_set():
    entry = {"name": "n", "path": "x.yaml", "options": {"a": [1, {"b": 2}], "c": "d"}}
    backend = hyera.YAMLBackend()
    one = hyera.HieraLevel.new(entry, backend)
    two = hyera.HieraLevel.new(copy.deepcopy(entry), backend)
    assert one == two
    assert hash(one) == hash(two)
    assert len({one, two}) == 1


def test_hiera_level_paths_are_strings(tmp_path):
    level = hyera.HieraLevel.new(
        {"name": "n", "datadir": "d", "paths": ["a.yaml", "b.yaml"]},
        hyera.YAMLBackend(),
    )
    paths = level.paths(tmp_path, hyera.Scope())
    assert [type(p) for p in paths] == [str, str]
    assert [os.path.basename(p) for p in paths] == ["a.yaml", "b.yaml"]
