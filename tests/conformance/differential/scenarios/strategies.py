"""Merge strategies over randomly generated, seeded data.

Four levels (two yaml_data, one json_data, one yaml_data), eight keys. Each
key has a dominant kind (hash / array / scalar) that most levels follow and
a minority of levels break, so both real merges and type clashes occur.
Every key is queried with no --merge (lookup_options decides) and with each
explicit strategy.
"""

from __future__ import annotations

from ..scenario import Scn, jdump, scenario

HIERA = """\
version: 5
defaults:
  datadir: data
  data_hash: yaml_data
hierarchy:
  - name: node
    path: "nodes/%{facts.a}.yaml"
  - name: os
    data_hash: json_data
    path: "os/%{facts.os.family}.json"
  - name: role
    path: "roles/%{facts.role}.yaml"
  - name: common
    path: common.yaml
"""
FILES = [
    "data/nodes/alpha.yaml",
    "data/os/RedHat.json",
    "data/roles/web.yaml",
    "data/common.yaml",
]

SCALARS = [
    "a",
    "b",
    "c",
    "--a",
    "--b",
    "--",
    "",
    "x y",
    "z",
    0,
    1,
    2,
    -1,
    1.5,
    True,
    False,
    None,
    "1",
    "true",
    "A",
    "10",
    10,
    "--c",
    "b",
    "a",
]
HKEYS = ["a", "b", "c", "d", "--a", "1", "name", "id"]

MERGES = [
    None,
    "first",
    "unique",
    "hash",
    "deep",
    {"strategy": "deep", "knockout_prefix": "--"},
    {"strategy": "deep", "sort_merged_arrays": True},
    {"strategy": "deep", "merge_hash_arrays": True},
    {
        "strategy": "deep",
        "knockout_prefix": "--",
        "sort_merged_arrays": True,
        "merge_hash_arrays": True,
    },
]

LOPT_MERGES = [
    "first",
    "unique",
    "hash",
    "deep",
    {"strategy": "deep"},
    {"strategy": "deep", "knockout_prefix": "--"},
    {"strategy": "deep", "merge_hash_arrays": True},
    {"strategy": "deep", "sort_merged_arrays": True},
    {
        "strategy": "deep",
        "knockout_prefix": "--",
        "merge_hash_arrays": True,
        "sort_merged_arrays": True,
    },
    {"strategy": "unique"},
    {"strategy": "hash"},
    {"strategy": "first"},
]


def scalar(rng):
    return rng.choice(SCALARS)


def value(rng, kind, depth):
    if kind == "scalar" or depth <= 0:
        return scalar(rng)
    if kind == "hash":
        out = {}
        for k in rng.sample(HKEYS, rng.randint(0, 4)):
            out[k] = value(
                rng, rng.choice(["scalar", "scalar", "hash", "arr"]), depth - 1
            )
        return out
    if kind == "arr":
        n = rng.randint(0, 4)
        ek = rng.choice(["scalar", "scalar", "scalar", "hash", "arr", "mixed"])
        out = []
        for _ in range(n):
            k = ek if ek != "mixed" else rng.choice(["scalar", "hash", "arr"])
            out.append(value(rng, k, depth - 1))
        return out
    raise AssertionError(kind)


def build(ctx, index, with_lopts):
    rng = ctx.rng("merge-random", with_lopts, index)
    s = Scn(
        "merge-rnd-%s%03d" % ("lo-" if with_lopts else "", index),
        "merge-lopts" if with_lopts else "merge",
        "random 4-level merge, index %d" % index,
    )
    s.hiera(HIERA)
    keys = ["k%d" % i for i in range(8)]
    levels = [dict() for _ in FILES]
    for k in keys:
        main = rng.choice(["hash", "hash", "hash", "arr", "arr", "scalar"])
        for lv in levels:
            if rng.random() < 0.25:
                continue
            kind = (
                main if rng.random() < 0.85 else rng.choice(["hash", "arr", "scalar"])
            )
            lv[k] = value(rng, kind, 3)
    if with_lopts:
        for lv in levels:
            if rng.random() < 0.3:
                continue
            lo = {}
            for k in rng.sample(keys, rng.randint(1, 4)):
                lo[k] = {"merge": rng.choice(LOPT_MERGES)}
            if rng.random() < 0.6:
                pat = rng.choice(
                    ["^k[0-3]$", "^k", "^k[4-7]", "k1", "^K", "^k.$", "^(k5|k6)$", ".*"]
                )
                lo[pat] = {"merge": rng.choice(LOPT_MERGES)}
            lv["lookup_options"] = lo
    for path, lv in zip(FILES, levels):
        s.file(path, jdump(lv))
    for k in keys:
        if with_lopts:
            s.q(k)
            s.q(k, merge="first")
            s.q(k, merge=rng.choice(MERGES[2:]))
        else:
            for m in MERGES:
                s.q(k, merge=m)
    if with_lopts:
        s.q("lookup_options")
    # a few dotted sub-keys of merged results
    for k in rng.sample(keys, 3):
        s.q(k + ".a", merge="deep")
        s.q(k + ".0", merge="unique")
    return s


@scenario
def merge_random(ctx):
    # an eighth of the budget for each kind: the other builders of the area
    # contribute the rest
    each = max(1, ctx.count // 8)
    for index in range(each):
        yield build(ctx, index, False)
    for index in range(each):
        yield build(ctx, index, True)


@scenario
def merge_targeted(ctx):
    """Hand-picked merge corners."""
    s = Scn("merge-falsy-shadow", "merge", "nil/false/0/''/[]/{} at the top level")
    s.hiera(HIERA)
    top = {
        "v_nil": None,
        "v_false": False,
        "v_zero": 0,
        "v_empty": "",
        "v_arr": [],
        "v_hash": {},
        "h": {"a": None, "b": False, "c": [], "d": {}},
        "a": [None, False, 0, ""],
    }
    low = {
        "v_nil": "low",
        "v_false": "low",
        "v_zero": "low",
        "v_empty": "low",
        "v_arr": ["low"],
        "v_hash": {"low": 1},
        "h": {"a": "low", "b": "low", "c": ["low"], "d": {"low": 1}},
        "a": ["low", None],
    }
    s.file(FILES[0], jdump(top))
    s.file(FILES[1], jdump({}))
    s.file(FILES[2], jdump({"v_nil": "role"}))
    s.file(FILES[3], jdump(low))
    for k in top:
        for m in MERGES:
            s.q(k, merge=m)
    s.q("v_nil", default="dflt")
    s.q("h.a")
    s.q("h.a", default="dflt")
    s.q("h.b", default="dflt")
    s.q("h.nope", default="dflt")
    yield s

    s = Scn("merge-knockout", "merge", "knockout prefixes in every position")
    s.hiera(HIERA)
    s.file(
        FILES[0],
        jdump(
            {
                "arr": ["--b", "n", "--zz"],
                "h": {"--x": "gone?", "y": "--", "z": "--z1", "l": ["--l1"]},
                "str": "--knock",
                "whole": "--",
                "hstr": {"a": "--"},
                "nested": {"h": {"l": ["--a", "new"]}},
                "arrh": [{"id": 1, "v": "--old"}, "--plain"],
                "only_ko": ["--a"],
                "ko_hash_key": {"--a": 1},
                "csv": "a,--b,c",
                "num": ["--1", 2],
                "space": ["-- b", "--  "],
                "dup": ["--a", "a"],
                "double": ["----a"],
                "prefixonly": ["--", "x"],
            }
        ),
    )
    s.file(
        FILES[1],
        jdump({"arr": ["b", "o"], "h": {"x": 1, "y": 2, "z": "z1", "l": ["l1", "l2"]}}),
    )
    s.file(
        FILES[2],
        jdump(
            {"arr": ["zz", "r"], "str": "knock", "whole": {"a": 1}, "hstr": {"a": "v"}}
        ),
    )
    s.file(
        FILES[3],
        jdump(
            {
                "arr": ["b", "c", "zz"],
                "h": {"x": 0, "y": 0, "w": 0, "l": ["l1", "l3"]},
                "str": "common",
                "whole": {"b": 2},
                "hstr": {"a": "c", "b": "c"},
                "nested": {"h": {"l": ["a", "b"]}},
                "arrh": [{"id": 1, "v": "old"}, "plain"],
                "only_ko": ["a", "b"],
                "ko_hash_key": {"a": 0},
                "csv": "b,d",
                "num": [1, 3],
                "space": ["b", " b"],
                "dup": ["a"],
                "double": ["--a", "a"],
                "prefixonly": ["y"],
            }
        ),
    )
    for k in [
        "arr",
        "h",
        "str",
        "whole",
        "hstr",
        "nested",
        "arrh",
        "only_ko",
        "ko_hash_key",
        "csv",
        "num",
        "space",
        "dup",
        "double",
        "prefixonly",
    ]:
        for m in [
            "deep",
            "unique",
            "hash",
            {"strategy": "deep", "knockout_prefix": "--"},
            {"strategy": "deep", "knockout_prefix": "--", "merge_hash_arrays": True},
            {"strategy": "deep", "knockout_prefix": "--", "sort_merged_arrays": True},
            {"strategy": "deep", "knockout_prefix": "-"},
            {"strategy": "deep", "knockout_prefix": "x"},
        ]:
            s.q(k, merge=m)
    yield s

    s = Scn(
        "merge-sort-mixed", "merge", "sort_merged_arrays over unsortable / mixed arrays"
    )
    s.hiera(HIERA)
    s.file(
        FILES[0],
        jdump(
            {
                "ints": [3, 1],
                "strs": ["b", "a"],
                "mixed": [2, "a"],
                "floats": [1.5, 1],
                "bools": [True, False],
                "nils": [None, "a"],
                "hashes": [{"b": 1}],
                "nested": [[2], [1]],
                "h": {"l": [3, 1], "m": ["z", 1]},
                "case": ["b", "B", "a", "A"],
                "uni": ["é", "e", "z"],
                "numstr": ["10", "9", "1"],
                "dup": [2, 2, 1],
            }
        ),
    )
    s.file(FILES[1], jdump({}))
    s.file(FILES[2], jdump({}))
    s.file(
        FILES[3],
        jdump(
            {
                "ints": [2, 0],
                "strs": ["c", "a"],
                "mixed": [1, "b"],
                "floats": [0.5, 2],
                "bools": [True],
                "nils": ["b"],
                "hashes": [{"a": 1}],
                "nested": [[0]],
                "h": {"l": [2], "m": ["a"]},
                "case": ["C"],
                "uni": ["a"],
                "numstr": ["2"],
                "dup": [1, 3],
            }
        ),
    )
    for k in [
        "ints",
        "strs",
        "mixed",
        "floats",
        "bools",
        "nils",
        "hashes",
        "nested",
        "h",
        "case",
        "uni",
        "numstr",
        "dup",
    ]:
        for m in [
            "unique",
            "deep",
            {"strategy": "deep", "sort_merged_arrays": True},
            {"strategy": "deep", "sort_merged_arrays": True, "merge_hash_arrays": True},
        ]:
            s.q(k, merge=m)
    yield s

    s = Scn("merge-hash-arrays", "merge", "merge_hash_arrays shapes")
    s.hiera(HIERA)
    s.file(
        FILES[0],
        jdump(
            {
                "l": [{"a": 1}, {"b": 2}],
                "uneven": [{"a": 1}],
                "mixed": [{"a": 1}, "s"],
                "nested": [{"h": {"x": 1}, "l": [1]}],
                "h": {"l": [{"a": 1, "n": {"x": 1}}]},
                "empty": [],
                "scalar_vs": [1, 2],
                "ko": [{"a": "--"}],
                "deepl": [[{"a": 1}]],
            }
        ),
    )
    s.file(FILES[1], jdump({"l": [{"c": 3}]}))
    s.file(FILES[2], jdump({}))
    s.file(
        FILES[3],
        jdump(
            {
                "l": [{"a": 9, "z": 0}, {"b": 9, "y": 0}, {"q": 1}],
                "uneven": [{"a": 2, "b": 2}, {"c": 3}],
                "mixed": ["t", {"b": 2}],
                "nested": [{"h": {"y": 2}, "l": [2]}],
                "h": {"l": [{"a": 2, "b": 2, "n": {"y": 2}}, {"c": 3}]},
                "empty": [{"a": 1}],
                "scalar_vs": [{"a": 1}],
                "ko": [{"a": "v", "b": "w"}],
                "deepl": [[{"b": 2}]],
            }
        ),
    )
    for k in [
        "l",
        "uneven",
        "mixed",
        "nested",
        "h",
        "empty",
        "scalar_vs",
        "ko",
        "deepl",
    ]:
        for m in [
            "deep",
            "unique",
            {"strategy": "deep", "merge_hash_arrays": True},
            {"strategy": "deep", "merge_hash_arrays": True, "knockout_prefix": "--"},
            {"strategy": "deep", "merge_hash_arrays": True, "sort_merged_arrays": True},
        ]:
            s.q(k, merge=m)
    yield s

    s = Scn(
        "merge-unique-shapes", "merge", "unique over scalars, nested arrays, hashes"
    )
    s.hiera(HIERA)
    s.file(
        FILES[0],
        jdump(
            {
                "sc": "top",
                "nested": [[1, 2], [3]],
                "withh": [{"a": 1}],
                "sc_arr": "top",
                "ints": [1, "1", 1.0],
                "h": {"a": 1},
                "deepn": [[[1]], [1]],
                "nilmix": [None],
                "one": "only",
                "bool": [True, "true"],
                "emp": [],
            }
        ),
    )
    s.file(
        FILES[1],
        jdump({"sc": "os", "nested": [[1, 2], 3], "sc_arr": ["os"], "h": {"b": 2}}),
    )
    s.file(FILES[2], jdump({"sc": "top", "sc_arr": "role", "nilmix": None}))
    s.file(
        FILES[3],
        jdump(
            {
                "sc": "common",
                "nested": [1],
                "withh": [{"a": 1}, {"a": 2}],
                "sc_arr": ["common", "os"],
                "ints": [1, 2],
                "h": {"a": 3},
                "deepn": [1],
                "nilmix": [None, 1],
                "bool": [True, False],
                "emp": [],
            }
        ),
    )
    for k in [
        "sc",
        "nested",
        "withh",
        "sc_arr",
        "ints",
        "h",
        "deepn",
        "nilmix",
        "one",
        "bool",
        "emp",
    ]:
        for m in ["unique", "hash", "deep", "first"]:
            s.q(k, merge=m)
    yield s

    # --merge spellings and option placement on the command line
    s = Scn("merge-cli-spellings", "merge-cli", "--merge values and deep-only flags")
    s.hiera(HIERA)
    s.file(FILES[0], jdump({"h": {"a": 1, "l": ["--x", "b"]}, "l": [2, 1]}))
    s.file(FILES[1], jdump({}))
    s.file(FILES[2], jdump({}))
    s.file(FILES[3], jdump({"h": {"b": 2, "l": ["x", "a"]}, "l": [3, 1]}))
    for key in ["h", "l"]:
        for args in (
            ["--merge", "default"],
            ["--merge", "FIRST"],
            ["--merge", "Deep"],
            ["--merge", "bogus"],
            ["--merge", ""],
            ["--merge", "reverse_deep"],
            ["--merge", "unconstrained_deep"],
            ["--merge", "deep", "--knock-out-prefix", "--"],
            ["--merge", "deep", "--knock-out-prefix=--"],
            ["--merge", "hash", "--knock-out-prefix=--"],
            ["--merge", "unique", "--sort-merged-arrays"],
            ["--merge", "first", "--merge-hash-arrays"],
            ["--knock-out-prefix=--"],
            ["--sort-merged-arrays"],
            ["--merge", "deep", "--knock-out-prefix="],
            ["--merge", "deep", "--knock-out-prefix", " "],
            ["--merge", "deep", "--knock-out-prefix=**"],
            ["--merge", "deep", "--knock-out-prefix=.*"],
            ["--merge", "deep", "--knock-out-prefix=["],
            ["--merge", "deep", "--knock-out-prefix=\\"],
            ["--merge", "deep", "--knock-out-prefix=(?<n>-)-"],
            ["--merge", "deep", "--knock-out-prefix=\\h"],
            ["--merge", "deep", "--sort-merged-arrays", "--merge-hash-arrays"],
            ["--merge", "first", "--merge", "deep"],
            ["--merge=deep"],
        ):
            s.q(key, args=args)
    yield s
