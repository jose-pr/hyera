"""Generated merge cases: value lists and a strategy spec.

A case is ``{"id", "spec", "variants"}``; the variants are the values the levels
of a hierarchy hold (highest priority first), or the missing marker. Values are
encoded so that ``1`` and ``1.0``, non-string hash keys and the marker survive
JSON: ``None``, booleans, integers and strings stand for themselves, a float is
``{"f": "<repr>"}``, an array is an array, a hash is ``{"h": [[key, value], ...]}``
and the missing marker is ``{"m": 1}``.
"""

from __future__ import annotations

import random
from typing import List

SCALARS = [
    None, True, False, 0, 1, 2, 3, 1.0, 2.5, "a", "b", "c", "--a", "--b", "--",
    "-", "", "1", "a,b", "x\n--a", "--a\n--b", "B", "é",
]  # fmt: skip
KEYS = ["a", "b", "c", "--a", "", 1, 2, 1.0, True, None, "1"]
STR_KEYS = ["a", "b", "c", "--a", "d"]
#: ``keys`` mode: keys Ruby keeps apart and Python treats as equal.
EQUAL_KEYS = [1, 1.0, True, "1", 2, 2.0, None, False, 0, 0.0, "a"]
EQUAL_SCALARS = [None, True, False, 0, 1, 1.0, 0.0, -0.0, "a", "1", 2, 2.0]

DEEP_OPTS = [
    {},
    {"knockout_prefix": "--"},
    {"knockout_prefix": "-"},
    {"sort_merged_arrays": True},
    {"merge_hash_arrays": True},
    {"knockout_prefix": "--", "sort_merged_arrays": True},
    {"knockout_prefix": "--", "merge_hash_arrays": True},
    {"sort_merged_arrays": True, "merge_hash_arrays": True},
    {"knockout_prefix": "--", "sort_merged_arrays": True, "merge_hash_arrays": True},
    {"merge_debug": False, "knockout_prefix": None},
]
UNCONSTRAINED_OPTS = DEEP_OPTS[:6] + [
    {"preserve_unmergeables": True},
    {"overwrite_arrays": True},
    {"unpack_arrays": ","},
    {"extend_existing_arrays": True},
    {"keep_array_duplicates": True},
    {"merge_nil_values": True},
    {"keep_array_duplicates": True, "knockout_prefix": "--"},
    {"extend_existing_arrays": True, "merge_nil_values": True},
    {"unpack_arrays": ",", "knockout_prefix": "--", "sort_merged_arrays": True},
    {"keep_array_duplicates": True, "merge_hash_arrays": True},
    {"preserve_unmergeables": True, "merge_hash_arrays": True},
]  # fmt: skip

#: ``values`` draws ordinary data; ``keys`` draws keys Python cannot tell apart.
MODES = ("values", "keys")


def specs() -> list:
    """Every strategy spec a case may carry, in a fixed order."""
    out: list = [
        "first",
        "unique",
        "hash",
        "deep",
        "unconstrained_deep",
        "reverse_deep",
    ]
    out += [dict(strategy="deep", **o) for o in DEEP_OPTS[1:]]
    out += [dict(strategy="unconstrained_deep", **o) for o in UNCONSTRAINED_OPTS[1:]]
    out += [dict(strategy="reverse_deep", **o) for o in UNCONSTRAINED_OPTS[1:]]
    return out


def _gen(rng: random.Random, depth: int, strkeys: bool, mode: str):
    scalars = EQUAL_SCALARS if mode == "keys" else SCALARS
    r = rng.random()
    if depth <= 0 or r < 0.35:
        return rng.choice(scalars)
    if r < 0.65:
        return [_gen(rng, depth - 1, strkeys, mode) for _ in range(rng.randint(0, 4))]
    if mode == "keys":
        keys = EQUAL_KEYS
    else:
        keys = STR_KEYS if (strkeys or rng.random() < 0.8) else KEYS
    out = {}
    for _ in range(rng.randint(0, 4)):
        out[rng.choice(keys)] = _gen(rng, depth - 1, strkeys, mode)
    return out


def encode(value):
    """A value in the JSON-safe encoding."""
    if isinstance(value, float):
        return {"f": repr(value)}
    if isinstance(value, list):
        return [encode(x) for x in value]
    if isinstance(value, dict):
        return {"h": [[encode(k), encode(x)] for k, x in value.items()]}
    return value


def generate(seed: int, count: int, mode: str = "values") -> List[dict]:
    """``count`` cases, the same on every platform for one ``seed`` and ``mode``.

    :param seed: the seed of the generator.
    :param count: how many cases.
    :param mode: one of :data:`MODES`.
    """
    if mode not in MODES:
        raise ValueError("unknown mode {!r}".format(mode))
    rng = random.Random("merge:{}:{}".format(mode, seed))
    all_specs = specs()
    key_pool = EQUAL_KEYS if mode == "keys" else STR_KEYS
    cases = []
    for index in range(count):
        spec = rng.choice(all_specs)
        name = spec if isinstance(spec, str) else spec["strategy"]
        strkeys = name == "hash" or rng.random() < 0.5
        variants = []
        for _ in range(rng.choice([1, 2, 2, 2, 3, 3, 4])):
            if rng.random() < 0.08:
                variants.append({"m": 1})
                continue
            # bias toward the shapes each strategy is meant for, mismatches mixed in
            r = rng.random()
            if name == "hash" or (name.endswith("deep") and r < 0.75):
                value = {
                    rng.choice(key_pool): _gen(rng, 3, strkeys, mode)
                    for _ in range(rng.randint(0, 4))
                }
            elif name == "unique" and r < 0.8:
                value = [_gen(rng, 2, strkeys, mode) for _ in range(rng.randint(0, 4))]
            else:
                value = _gen(rng, 3, strkeys, mode)
            variants.append(encode(value))
        cases.append(
            {"id": "{}-{}".format(mode, index), "spec": spec, "variants": variants}
        )
    return cases
