"""The differential tool's own checks. None of them needs Puppet.

Generation is deterministic (a pinned digest per area), every classification rule
names a difference the README publishes and fires on a hand-made example, and
the hyera driver answers a hand-built scenario.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from differential import corpus, drive_hyera, outcomes, batch
from differential.generate import build, make_jobs, write_area
from differential.merge import gen as merge_gen
from differential.merge import ours as merge_ours
from differential.rules import OPEN, OPEN_PREFIX, RULES, judge
from differential.scenario import REGISTRY, Scn
from differential.scenarios import AREAS

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

#: seed 1, count 5: the digest of each area's scenarios, joined. A change here
#: means the generated trees changed, on some platform or interpreter.
AREA_DIGESTS = {
    "backends": "a4e12c041a5b4ed947e2845c12397a62f9545434a0d8df08917ea8117d5d783f",
    "config": "98566819de7349b71b3a1d809943f2e009609d8635820bc9590a9991d2e58bdb",
    "extra": "d0d72c0d5dac103575441c25337b267318884b46eca3019aec4a56c3adb0e226",
    "interp": "53a55f578c0f7a46d8a307265b6fef7827e022af796b3eb62bf9e486b2ec10b6",
    "interp_sweep": "b2c795014d8f99fa06031fb35f10c8eef1f21e3eba837ad83704bd09b60b696c",
    "keys": "5b5a457c4f2719cbde4444aab5f8c2b36f2a5e76ae4d782fde01ecebcaeb8e14",
    "layers": "28650be4f27ccaece9552598b61ba198e3da3098abc997a802616395cc87ba4f",
    "locations": "4c92631822602b302b585660c45d59eef5e772dcaafca2929ac38658772a33d4",
    "lopts": "5bb7c8b6b784a4b56437076341ef4b107ac5a8095e963a0472a9a463c3f70a74",
    "strategies": "5609fae284631f786e315f0e5e3fb21c84d230a4dc67704c9d2b2ff0d393acbe",
    "yaml_data": "abb4af318e8bc20155c4ce018d1a39e9add02ca986389e36d80e76618a2a6b03",
}

MERGE_DIGEST = "70791ae62450e8a6f26d154e1fc564dbef876f95315da5da4b4bd18e89990668"


def _area_digest(area: str, seed: int = 1, count: int = 5) -> str:
    h = hashlib.sha256()
    for scn in build(area, seed, count):
        h.update(scn.name.encode("utf-8") + b"=" + scn.digest().encode("ascii") + b"\n")
    return h.hexdigest()


def _readme_ids() -> set:
    text = README.read_text(encoding="utf-8")
    section = text.split("## Differences from Puppet", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"\(id: `([a-z0-9-]+)`\)", section))


@pytest.mark.parametrize("area", AREAS)
def test_the_same_seed_gives_the_same_trees(area):
    assert _area_digest(area) == _area_digest(area)


@pytest.mark.parametrize("area", AREAS)
def test_each_area_matches_its_recorded_digest(area):
    assert _area_digest(area) == AREA_DIGESTS[area]


def test_a_different_seed_changes_the_generated_data():
    assert _area_digest("strategies", seed=1) != _area_digest("strategies", seed=2)


def test_a_count_below_the_total_keeps_a_seeded_sample():
    small = [s.name for s in build("config", 1, 7)]
    assert len(small) == 7
    assert small == sorted(small)
    assert small == [s.name for s in build("config", 1, 7)]
    assert small != [s.name for s in build("config", 2, 7)]


def test_every_builder_is_registered_under_an_area():
    assert set(REGISTRY) == set(AREAS)
    assert sum(len(v) for v in REGISTRY.values()) == 27


@pytest.mark.parametrize("area", AREAS)
def test_scenario_trees_hold_only_lf_text_and_no_absolute_path(area, tmp_path):
    scenarios = build(area, 1, 12)
    write_area(scenarios, tmp_path)
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file() or path.name == "scenario.json":
            continue
        data = path.read_bytes()
        if b"\r" in data or b"\xef\xbb\xbf" in data or path.suffix == ".raw":
            continue
        assert str(tmp_path).encode("utf-8") not in data


def test_job_ids_are_unique_and_relative(tmp_path):
    for area in AREAS:
        jobs = make_jobs(build(area, 1, 8))
        ids = [j["id"] for j in jobs]
        assert len(ids) == len(set(ids))
        assert all(not Path(j["dir"]).is_absolute() for j in jobs)


def test_every_rule_names_a_published_difference():
    readme = README.read_text(encoding="utf-8")
    ids = _readme_ids()
    assert len({rule.id for rule in RULES}) == len(RULES)
    for rule in RULES:
        if rule.published is None:
            assert rule.id in ids, rule.id
        else:
            assert rule.id not in ids
            assert rule.published in readme, rule.published


def test_every_open_defect_has_a_distinct_key_and_a_description():
    keys = [defect.key for defect in OPEN]
    assert len(keys) == len(set(keys))
    assert all(defect.what for defect in OPEN)


def _found(value, **extra):
    return {"status": "found", "value": value, **extra}


def _error(message):
    return {"status": "error", "message": message}


def _both(outcome):
    return {"api": outcome, "cli": outcome}


def _job(key="k", render=None, exts=(), **query):
    job = {
        "id": "x",
        "scn": "unit",
        "argv": [key],
        "exts": list(exts),
        "query": dict(key=key, **query),
    }
    if render:
        job["render"] = render
    return job


def _cli_only(outcome):
    return {"api": None, "cli": outcome}


_MISS = {"status": "not_found"}

#: rule id -> (job, Puppet's outcome, hyera's outcomes) that the rule explains,
#: for every declared difference and every open defect.
EXAMPLES = {
    "render-yaml-sensitive-redacted": (
        _job(render="yaml"),
        {"status": "found", "text": "--- !ruby/object:Sensitive\nx\n"},
        _cli_only({"status": "found", "text": "--- Sensitive [value redacted]\n"}),
    ),
    "render-yaml-equivalent-not-identical": (
        _job(render="yaml"),
        {"status": "found", "text": "a: 'x'\n"},
        _cli_only({"status": "found", "text": "a: x\n"}),
    ),
    "json-float-spelling": (
        _job(),
        _found(1e-05, text="0.00001"),
        _both(_found(1e-05)),
    ),
    "knockout-prefix-not-python-regex": (
        _job(merge={"strategy": "deep", "knockout_prefix": "**"}),
        _found({"a": 1}),
        _both(_error("a nested repeat")),
    ),
    "ruby-regex-constructs": (
        _job(),
        _MISS,
        _both(_error("hyera does not support \\R in the Ruby regular expression /k/")),
    ),
    "unrunnable-types": (
        _job(type="Iterable"),
        _found([1]),
        _both(_error("hyera does not support the Puppet type 'Iterable'")),
    ),
    "convert-to-unsupported-type": (
        _job(),
        _found("x"),
        _both(_error("hyera does not support new() for the Puppet type 'Timestamp'")),
    ),
    "v3-ruby-backend-unavailable": (
        _job(),
        _MISS,
        _both(_error("Ruby Hiera 3 backends cannot run here")),
    ),
    "string-format-subset": (
        _job(),
        _found("x"),
        _both(_error("hyera does not support the # (indenting) flag in a format")),
    ),
    "interpolation-key-shapes": (
        _job(),
        _found({"[1]": "v"}),
        _both(_error("Interpolated hash key [1] is not hashable")),
    ),
    "environment-conf-compile-trusted-unsupported": (
        {**_job(), "argv": ["--compile", "k"]},
        _MISS,
        _cli_only(_error("usage")),
    ),
    "missing-config-raises": (
        {**_job(), "argv": ["--hiera_config", "nope.yaml", "k"]},
        _MISS,
        _cli_only(_error("Unable to read the Lookup Configuration at 'x'")),
    ),
    "python-equal-hash-keys": (
        _job(),
        _found("v"),
        _both(_error("mapping keys 1 and True are indistinguishable to hyera")),
    ),
    "environment-trailing-slash": (
        _job(args=["--environment", "production/"]),
        _found("production"),
        _both(_error("Could not find a directory environment named 'production/'")),
    ),
    "non-utf8-data": (
        _job(),
        _found("fallback"),
        _both(_error("Unable to parse (x): 'utf-8' codec can't decode byte 0xe9")),
    ),
    "nesting-bound": (
        _job(),
        _error("Unable to parse (x): nesting of 101 is too deep"),
        _both(_found([1])),
    ),
    "puppet-crashes-hyera-answers": (
        _job(),
        _error("undefined method 'include?' for an instance of Integer"),
        _both(_found(1)),
    ),
    "hocon-pyhocon-parser": (
        _job(exts=[".conf"]),
        _found([1, 2]),
        _both(_found(2)),
    ),
    "empty-environment": (
        _job(args=["--environment", ""]),
        _MISS,
        _both(_error("environment must not be empty")),
    ),
    "open:bare-enum-matches-any-string": (
        _job(type="Enum"),
        _error("Found value has wrong type, expects a match for Enum, got 'x'"),
        _both(_found("x")),
    ),
    "open:type-forms-puppet-accepts": (
        _job(type="Integer[1.0, 50]"),
        _found(42),
        _both(
            _error(
                "The expression <Integer[1.0, 50]> is not a valid type specification."
            )
        ),
    ),
    "open:lookup-options-key-on-broken-layer": (
        _job("lookup_options"),
        _MISS,
        _both(_error("The Lookup Configuration at 'x' has wrong type")),
    ),
    "open:environment-name-case": (
        _job(args=["--environment", "Production"]),
        _found("production"),
        _both(_error("Could not find a directory environment named 'Production'")),
    ),
    "open:convert-to-hash-keys-and-build": (
        _job(),
        _found({"[1]": "x"}),
        _both(_error("convert_to raised error: unusable Hash key: cannot use 'list'")),
    ),
    "open:yaml-tags-psych-loads": (
        _job(),
        _found(1),
        _both(_error("Unable to parse (x): Tried to load unspecified class: Encoding")),
    ),
}


def test_every_rule_and_open_defect_has_an_example():
    expected = {rule.id for rule in RULES} | {OPEN_PREFIX + d.key for d in OPEN}
    assert set(EXAMPLES) == expected


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_each_rule_explains_its_example(name):
    job, puppet, hyera = EXAMPLES[name]
    kind, found = judge(job, puppet, hyera)
    assert kind != "AGREE"
    assert found == name


def test_a_variable_with_four_leading_colons_is_a_declared_difference():
    job = {**_job(), "qid": "t178 alone: %{::::fa}"}
    assert judge(job, _found("FA"), _both(_found(""))) == (
        "VALUE",
        "interpolation-key-shapes",
    )
    assert judge(
        {**job, "qid": "t1 alone: %{fa}"}, _found("FA"), _both(_found(""))
    ) == (
        "VALUE",
        None,
    )


def test_an_open_defect_is_not_an_unclassified_disagreement():
    job, puppet, hyera = EXAMPLES["open:bare-enum-matches-any-string"]
    row = batch.Row(job, puppet, hyera, *judge(job, puppet, hyera))
    assert not row.unclassified
    assert batch.summarize([row])["open"] == {"bare-enum-matches-any-string": 1}


def test_a_disagreement_no_rule_explains_is_unclassified():
    puppet = _found(1)
    hyera = {"api": _found(2), "cli": _found(2)}
    job = _job()
    assert judge(job, puppet, hyera) == ("VALUE", None)
    assert batch.Row(job, puppet, hyera, "VALUE", None).unclassified


def test_two_errors_with_different_text_agree_under_the_message_difference():
    puppet = {"status": "error", "message": "Puppet's words"}
    hyera = {
        "api": {"status": "error", "message": "hyera's words"},
        "cli": {"status": "error", "message": "hyera's words"},
    }
    job = {"query": {"key": "k"}, "argv": ["k"]}
    assert judge(job, puppet, hyera) == ("AGREE", "error-message-text")


def test_the_paths_in_a_message_are_normalised_in_every_slash_form():
    text = "no such file C:\\work\\a\\b\\data\\c.yaml and C:/work/a/b/x"
    assert outcomes.normalize(text, "C:\\work\\a\\b") == (
        "no such file <case>/data/c.yaml and <case>/x"
    )


def test_ruby_hash_inspect_spacing_is_compared_in_the_ruby_32_form():
    assert outcomes.canon({"a": '{"k" => 1}'}) == outcomes.canon({"a": '{"k"=>1}'})
    assert outcomes.canon({"a": "'k' => 1"}) != outcomes.canon({"a": "'k'=>1"})


def test_the_hyera_driver_answers_a_hand_built_scenario(tmp_path):
    scn = Scn("hand-built", "unit", "one of each outcome")
    scn.area = "unit"
    scn.simple()
    scn.file("data/common.yaml", "k: v\nn: 1\nh: {a: 1}\n")
    scn.q("k")
    scn.q("n")
    scn.q("nope")
    scn.q("h", merge="deep")
    scn.q("k", type="Integer")
    scn.q("k", args=["--merge", "bogus"], id="cli-only")
    scn.write(tmp_path / "unit")
    jobs = make_jobs([scn])
    results = {r["id"]: r for r in drive_hyera.run_jobs(jobs, tmp_path)}
    by_qid = {j["qid"]: results[j["id"]] for j in jobs}
    assert by_qid["k"]["api"]["value"] == "v"
    assert by_qid["k"]["cli"]["rc"] == 0
    assert by_qid["n"]["api"]["value"] == 1
    assert by_qid["nope"]["api"]["status"] == "not_found"
    assert by_qid["nope"]["cli"]["rc"] == 1
    assert by_qid["h m=deep"]["api"]["value"] == {"a": 1}
    assert by_qid["k t=Integer"]["api"]["status"] == "error"
    assert by_qid["cli-only"]["api"]["status"] == "skipped"
    assert by_qid["cli-only"]["cli"]["rc"] == 2


def test_a_found_value_is_judged_against_a_recorded_puppet_outcome(tmp_path):
    scn = Scn("judged", "unit")
    scn.area = "unit"
    scn.simple()
    scn.file("data/common.yaml", "k: v\n")
    scn.q("k")
    scn.write(tmp_path / "unit")
    (job,) = make_jobs([scn])
    (raw,) = drive_hyera.run_jobs([job], tmp_path)
    hyera = outcomes.hyera_outcomes(raw, str(tmp_path / "unit" / "judged"), None)
    assert judge(job, {"status": "found", "value": "v"}, hyera) == ("AGREE", None)
    assert judge(job, {"status": "found", "value": "w"}, hyera) == ("VALUE", None)
    assert judge(job, {"status": "not_found"}, hyera) == ("STATUS", None)


def test_merge_cases_are_the_same_for_the_same_seed():
    first = merge_gen.generate(1, 40)
    assert first == merge_gen.generate(1, 40)
    assert first != merge_gen.generate(2, 40)
    assert merge_gen.generate(1, 10, "keys") != merge_gen.generate(1, 10, "values")


def test_merge_cases_match_their_recorded_digest():
    h = hashlib.sha256()
    for mode in merge_gen.MODES:
        for case in merge_gen.generate(1, 60, mode):
            h.update(repr(case).encode("utf-8"))
    assert h.hexdigest() == MERGE_DIGEST


def test_hyera_merges_a_hand_built_case():
    case = {
        "id": "x",
        "spec": {"strategy": "deep", "knockout_prefix": "--"},
        "variants": [
            {"h": [["a", ["x", "--y"]]]},
            {"h": [["a", ["y", "z"]], ["b", 1]]},
        ],
    }
    (result,) = merge_ours.run_cases([case])
    assert merge_ours.normal(result["res"]) == merge_ours.normal(
        {"h": [["a", ["z", "x"]], ["b", 1]]}
    )


def test_hash_keys_python_cannot_tell_apart_are_detected():
    case = {"variants": [{"h": [[1, "a"]]}, {"h": [[{"f": "1.0"}, "b"]]}]}
    assert merge_ours.has_python_equal_keys(case)
    assert not merge_ours.has_python_equal_keys({"variants": [{"h": [["a", 1]]}]})


def test_the_corpus_plan_covers_every_area():
    assert set(corpus.PLAN) == set(AREAS)
