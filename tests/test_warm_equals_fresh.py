"""A long-lived ``Hiera`` answers like one built now, after any change on disk
(file added, edited, deleted, swapped for a directory, made unparsable), across
location layouts, layers, backends and cache bounds.
"""

import copy
import itertools
import os
import shutil

import pytest
import yaml

from hyera import Hiera, Scope
from hyera.backends import Backend, YAMLBackend, default_backends

_CLOCK = itertools.count(1)
_BASE_NS = 1_700_000_000 * 10**9
_FUNCTION = "warm_fresh_lookup_key"

# name -> (hierarchy entry fields, files that exist before the target, target)
_LAYOUTS = {
    "path": ({"path": "target.yaml"}, {}, "target.yaml"),
    "paths": ({"paths": ["first.yaml", "target.yaml"]}, {}, "target.yaml"),
    "mapped_paths": (
        {"mapped_paths": ["roles", "r", "roles/%{r}.yaml"]},
        {},
        "roles/target.yaml",
    ),
    "glob-dir": (
        {"glob": "conf.d/*.yaml"},
        {"conf.d/zz.yaml": "other: 1\n"},
        "conf.d/aa.yaml",
    ),
    "glob-dir-absent": ({"glob": "conf.d/*.yaml"}, {}, "conf.d/aa.yaml"),
    "glob-literal-subdir": (
        {"glob": "a/b/*.yaml"},
        {"a/keep.txt": "x\n"},
        "a/b/aa.yaml",
    ),
    "glob-wildcard-dir": (
        {"glob": "nodes/*/over.yaml"},
        {"nodes/n1/other.yaml": "o: 1\n"},
        "nodes/n1/over.yaml",
    ),
    "glob-new-wildcard-dir": (
        {"glob": "nodes/*/over.yaml"},
        {"nodes/n1/other.yaml": "o: 1\n"},
        "nodes/n2/over.yaml",
    ),
    "glob-literal-file": (
        {"glob": "conf.d/exact.yaml"},
        {"conf.d/zz.yaml": "other: 1\n"},
        "conf.d/exact.yaml",
    ),
    "glob-recursive": (
        {"glob": "tree/**/*.yaml"},
        {"tree/x/y/zz.yaml": "o: 1\n"},
        "tree/x/y/aa.yaml",
    ),
    "glob-recursive-new-dir": (
        {"glob": "tree/**/*.yaml"},
        {"tree/x/y/zz.yaml": "o: 1\n"},
        "tree/x/new/aa.yaml",
    ),
    "glob-braces": (
        {"glob": "{one,two}/*.yaml"},
        {"one/zz.yaml": "o: 1\n"},
        "two/aa.yaml",
    ),
    "glob-datadir-absent": ({"glob": "*.yml"}, {}, "aa.yml"),
}

# name -> (target present at the start, steps)
_MUTATIONS = {
    "add": (False, [("write", "added")]),
    "edit": (True, [("write", "edited-and-longer")]),
    "delete": (True, [("delete", None)]),
    "dir-and-back": (True, [("dir", None), ("write", "a-file-again")]),
    "unparsable-and-back": (True, [("write", "[unterminated"), ("write", "fixed")]),
}

_LAYERS = {
    "global": ("k", "data"),
    "environment": ("k", "envs/production/data"),
    "module": ("m::k", "modules/m/data"),
}


def _tick():
    return _BASE_NS + next(_CLOCK) * 10**9


def _stamp(path):
    t = _tick()
    os.utime(path, ns=(t, t))


def _gained(path):
    """The deepest directory above ``path`` that exists now."""
    parent = os.path.dirname(path)
    while not os.path.isdir(parent):
        parent = os.path.dirname(parent)
    return parent


def _write(path, text):
    if os.path.isdir(path):
        shutil.rmtree(path)
    gained = _gained(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))
    _stamp(path)
    _stamp(gained)


def _delete(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    _stamp(_gained(path))


def _make_dir(path):
    _delete(path)
    gained = _gained(path)
    os.mkdir(path)
    _stamp(gained)


def _hiera_yaml(entry, kind):
    entry = dict(entry, name="level")
    if kind == "lookup_key":
        entry["lookup_key"] = _FUNCTION
    common = {"name": "common", "path": "common.yaml"}
    return yaml.safe_dump(
        {
            "version": 5,
            "defaults": {"datadir": "data", "data_hash": "yaml_data"},
            "hierarchy": [entry, common],
        },
        sort_keys=False,
    )


def _outcome(h, key):
    try:
        return ("ok", h.lookup(key))
    except Exception as e:  # compared as data: warm and fresh must agree
        return ("err", type(e).__name__, str(e))


@pytest.fixture
def lookup_key_backend(monkeypatch):
    monkeypatch.setattr(Backend, "_REGISTRY", copy.deepcopy(Backend._REGISTRY))

    class FileKeyBackend(Backend):
        NAMES = {"function": (_FUNCTION,)}

        def lookup_key(self, key, options, context):
            data = context.cached_file_data(options["path"], YAMLBackend().loads)
            if not isinstance(data, dict) or key not in data:
                context.not_found()
            return data[key]

    return FileKeyBackend


def _scenario(tmp_path, layer, layout, kind, mutation, cache_size):
    key, datadir = _LAYERS[layer]
    entry, before, target = _LAYOUTS[layout]
    present, steps = _MUTATIONS[mutation]
    root = str(tmp_path)

    def data(rel):
        return os.path.join(root, datadir, *rel.split("/"))

    layer_dir = os.path.dirname(data("x"))
    hiera_text = _hiera_yaml(entry, kind)
    if layer == "global":
        _write(os.path.join(root, "hiera.yaml"), hiera_text)
        kwargs = {}
        config = os.path.join(root, "hiera.yaml")
    else:
        _write(
            os.path.join(root, "hiera.yaml"),
            _hiera_yaml({"path": "absent.yaml"}, "data_hash"),
        )
        config = os.path.join(root, "hiera.yaml")
        if layer == "environment":
            _write(os.path.join(root, "envs", "production", "hiera.yaml"), hiera_text)
            kwargs = {"environmentpath": os.path.join(root, "envs")}
        else:
            _write(os.path.join(root, "modules", "m", "hiera.yaml"), hiera_text)
            kwargs = {"basemodulepath": os.path.join(root, "modules")}
    del layer_dir
    _write(data("common.yaml"), "{}: common\n".format(key))
    for rel, text in before.items():
        _write(data(rel), text)
    if present:
        _write(data(target), "{}: original\n".format(key))
    scope = Scope(variables={"roles": ["target"]})

    def build():
        return Hiera(config, scope=scope, cache_size=cache_size, **kwargs)

    def apply(step):
        action, text = step
        if action == "write":
            _write(data(target), "{}: {}\n".format(key, text))
        elif action == "delete":
            _delete(data(target))
        else:
            _make_dir(data(target))

    return key, build, steps, apply


def _check(key, warm, view, build):
    fresh = _outcome(build(), key)
    for _ in range(2):
        assert _outcome(warm, key) == fresh
        assert _outcome(view, key) == fresh


def _run(tmp_path, layer, layout, kind, mutation, cache_size):
    key, build, steps, apply = _scenario(
        tmp_path, layer, layout, kind, mutation, cache_size
    )
    warm = build()
    view = warm.scoped(variables={"unrelated": 1})
    _check(key, warm, view, build)
    for step in steps:
        apply(step)
        _check(key, warm, view, build)


def _cases():
    out = []
    kinds = ("data_hash", "lookup_key")
    for layout, mutation, kind in itertools.product(_LAYOUTS, _MUTATIONS, kinds):
        out.append(("global", layout, kind, mutation, 256))
    for layer, layout, mutation, kind in itertools.product(
        ("environment", "module"),
        ("path", "glob-dir-absent", "glob-wildcard-dir"),
        _MUTATIONS,
        kinds,
    ):
        out.append((layer, layout, kind, mutation, 256))
    for size, layout, mutation in itertools.product(
        (1, 0, None), ("path", "glob-dir-absent"), _MUTATIONS
    ):
        out.append(("global", layout, "data_hash", mutation, size))
    return out


@pytest.mark.parametrize(
    "layer, layout, kind, mutation, cache_size",
    _cases(),
    ids=lambda v: str(v),
)
def test_warm_instance_answers_like_a_fresh_one(
    tmp_path, lookup_key_backend, layer, layout, kind, mutation, cache_size
):
    assert lookup_key_backend in default_backends()
    _run(tmp_path, layer, layout, kind, mutation, cache_size)
