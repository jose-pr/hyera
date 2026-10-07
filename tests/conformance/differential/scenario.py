"""Scenario trees: the unit the differential tool generates, drives and compares.

A scenario is a directory (``hiera.yaml``, a data tree, ``facts.yaml``, optional
``environments/`` and ``modules/``) plus the queries to run against it. Builders
register under their area (the module name under ``scenarios/``) and yield
:class:`Scn` objects; nothing here reads the clock, the locale or the
filesystem's listing order, so a seed and a count give the same trees anywhere.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
import textwrap
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional

#: area name -> builder functions, in registration order.
REGISTRY: Dict[str, List[Callable]] = {}

DEFAULT_FACTS = """\
a: alpha
role: web
n: 42
b: true
f: 1.5
empty: ""
arr: [x, y, {k: v}]
os:
  family: RedHat
  name: Rocky
  release:
    major: "9"
weird: "a b/c"
dotdot: ".."
slash: "sub/inner"
star: "*"
hsh: {one: 1, two: "2"}
"""

V5 = """\
version: 5
defaults:
  datadir: data
  data_hash: yaml_data
hierarchy:
"""


def d(text: str) -> str:
    """``textwrap.dedent`` under a short name for the builders' templates."""
    return textwrap.dedent(text)


def jdump(obj) -> str:
    """JSON text, which is also unambiguous YAML flow style."""
    return json.dumps(obj, ensure_ascii=False, indent=1) + "\n"


class Ctx:
    """What a builder is given: the seed and a per-area budget.

    :param seed: the run's seed.
    :param count: the most scenarios the area keeps; a builder that invents
        data sizes its own output from it.
    """

    def __init__(self, seed: int, count: int, area: str):
        self.seed = seed
        self.count = count
        self.area = area

    def rng(self, *parts) -> random.Random:
        """A generator private to one builder (and one index), independent of
        every other draw; string seeding hashes with SHA-512, never ``hash()``."""
        label = ":".join([str(self.seed), self.area] + [str(p) for p in parts])
        return random.Random(label)


class Scn:
    """One scenario: a file tree and its queries.

    :param name: unique within a run and safe as a directory name on every
        platform (no case-only differences between names).
    :param family: a coarse label the report groups by.
    :param desc: one line saying what the scenario probes.
    """

    def __init__(self, name: str, family: str, desc: str = ""):
        self.name = name
        self.family = family
        self.desc = desc
        self.area = ""
        self.files: Dict[str, object] = {"facts.yaml": DEFAULT_FACTS}
        self.queries: List[dict] = []
        #: extra ``--render-as`` formats every query is also run under.
        self.render: tuple = ()
        #: the answers depend on the Python version, the stack depth, an
        #: environment variable or the platform's path rules, so a corpus
        #: leaves the scenario out.
        self.volatile = False
        self._ids: set = set()

    def file(self, rel: str, content) -> "Scn":
        """Add a file: text is written as UTF-8 exactly as given, bytes verbatim,
        ``None`` makes an empty directory."""
        self.files[rel] = content
        return self

    def hiera(self, text) -> "Scn":
        return self.file("hiera.yaml", text)

    def facts(self, text) -> "Scn":
        return self.file("facts.yaml", text)

    def simple(self, levels=("common",)) -> "Scn":
        """A version 5 ``hiera.yaml`` with one ``yaml_data`` path level per name."""
        text = V5
        for level in levels:
            text += '  - name: "{0}"\n    path: "{0}.yaml"\n'.format(level)
        return self.hiera(text)

    def q(self, key, merge=None, default=None, type=None, args=None, id=None) -> "Scn":
        """Add one query.

        :param key: a key or a list of keys.
        :param merge: a ``--merge`` strategy name or a dict with ``strategy`` and
            the deep options.
        :param default: the ``--default`` text.
        :param type: the ``--type`` expression.
        :param args: extra ``puppet lookup`` flags (``--strict X``,
            ``--environment E``).
        :param id: an explicit id; otherwise one is derived from the other arguments.
        """
        query: dict = {"key": key}
        if merge is not None:
            query["merge"] = merge
        if default is not None:
            query["default"] = default
        if type is not None:
            query["type"] = type
        if args:
            query["args"] = list(args)
        base = id or self._auto_id(query)
        qid, n = base, 1
        while qid in self._ids:
            n += 1
            qid = "{}#{}".format(base, n)
        self._ids.add(qid)
        query["id"] = qid
        self.queries.append(query)
        return self

    def qs(self, keys: Iterable, **kw) -> "Scn":
        for key in keys:
            self.q(key, **kw)
        return self

    @staticmethod
    def _auto_id(query: dict) -> str:
        key = query["key"]
        parts = ["|".join(key) if isinstance(key, list) else key]
        if "merge" in query:
            m = query["merge"]
            parts.append(
                "m=" + (m if isinstance(m, str) else json.dumps(m, sort_keys=True))
            )
        if "default" in query:
            parts.append("d=" + query["default"])
        if "type" in query:
            parts.append("t=" + query["type"])
        if "args" in query:
            parts.append(" ".join(query["args"]))
        return " ".join(parts)

    def _payload(self, rel: str) -> Optional[bytes]:
        content = self.files[rel]
        if content is None:
            return None
        return content if isinstance(content, bytes) else content.encode("utf-8")

    def write(self, root: Path) -> Path:
        """Write the tree under ``root/<name>``, replacing a previous one, and
        return its directory. Files are written as bytes, so no platform
        translates a line ending."""
        base = Path(root) / self.name
        if base.is_dir():
            shutil.rmtree(str(base))
        for rel in sorted(self.files):
            path = base / rel
            data = self._payload(rel)
            if data is None:
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return base

    def digest(self) -> str:
        """SHA-256 over the sorted tree and the queries: the identity a recorded
        corpus pins, equal on every platform and interpreter."""
        h = hashlib.sha256()
        for rel in sorted(self.files):
            data = self._payload(rel)
            h.update(rel.encode("utf-8") + b"\0")
            h.update(b"D" if data is None else b"F" + data)
            h.update(b"\0")
        h.update(json.dumps(self.queries, sort_keys=True).encode("utf-8"))
        h.update(json.dumps(list(self.render)).encode("utf-8"))
        h.update(b"V" if self.volatile else b"S")
        return h.hexdigest()


def scenario(fn: Callable) -> Callable:
    """Register ``fn(ctx)`` as a builder of the area named by its module."""
    area = fn.__module__.rsplit(".", 1)[-1]
    REGISTRY.setdefault(area, []).append(fn)
    return fn


def lookup_argv(query: dict) -> List[str]:
    """The ``puppet lookup`` argument tail for one query, in the recorder's
    order and with its ``--strict warning`` default."""
    args: List[str] = []
    extra = query.get("args") or []
    if "--strict" not in extra:
        args += ["--strict", "warning"]
    args += extra
    args += merge_flags(query.get("merge"))
    if query.get("type"):
        args += ["--type", query["type"]]
    if query.get("default") is not None:
        args += ["--default", query["default"]]
    key = query["key"]
    args += key if isinstance(key, list) else [key]
    return args


def merge_flags(merge) -> List[str]:
    if merge is None:
        return []
    if isinstance(merge, str):
        return ["--merge", merge]
    flags = ["--merge", merge["strategy"]]
    if "knockout_prefix" in merge:
        flags += ["--knock-out-prefix", merge["knockout_prefix"]]
    if merge.get("sort_merged_arrays"):
        flags.append("--sort-merged-arrays")
    if merge.get("merge_hash_arrays"):
        flags.append("--merge-hash-arrays")
    return flags


def iter_builders(area: str) -> Iterator[Callable]:
    """The builders of one area, ordered by name."""
    return iter(sorted(REGISTRY.get(area, []), key=lambda fn: fn.__name__))
