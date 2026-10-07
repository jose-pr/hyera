"""The differential tool's own checks. None of them needs Puppet.

Generation is deterministic (a pinned digest per area), every classification rule
names a difference the README publishes and fires on a hand-made example, and
the hyera driver answers a hand-built scenario.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from differential import batch, corpus, drive_hyera, outcomes
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
    "backends": "c17bd7a107c1e8238d17b5840d5268dd7cadb763a06df9db6fa6b7d4e13f8d11",
    "config": "83c765b5588e6ccb60deb838af3226b2e05b5d1e799196e1454e7485d0c3e093",
    "extra": "10d069e19d5836e1fc457856a33fce99bb705593aaf4effd633304809a9dc983",
    "interp": "33a153f84ac46e4aa111f592bba6b9b1144316437f306dd7376d0a363ec71717",
    "interp_sweep": "08df50ce7a2e607ad697085202f04c759e1ff99b14693ec17a182ccd6f282702",
    "keys": "89c337daae93acd29fb8bb2e392f6348927853fdfd26f29e3e3a00cd509886cb",
    "layers": "934617525cf560c79732edb696a546d8a969a14b62cccd80e4dd1909778c4382",
    "locations": "67413490f16cef82015cfc8e526342ccbb07f8446eb1440d107775a8cdf8c6d4",
    "lopts": "08c479a787edab6449c9cb27c8cf0aeee61f9ced1d002f67ddae43629baec6d9",
    "strategies": "5cf1fe899ed41312c7497e6c62fa3d2f8fa8a626279e6cbb8c1055ed22e545ff",
    "yaml_data": "ab0d9720acb655c9d7afea4014d3b80956fb3d6e0b3fd1f1a61735f82bd7a243",
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


@pytest.mark.parametrize("area", AREAS)
def test_a_scenario_holds_no_two_paths_that_differ_only_in_case(area):
    for scn in build(area, 1, 400):
        folded = [rel.lower() for rel in scn.files]
        assert len(folded) == len(set(folded)), scn.name
        assert all(len(rel) < 80 for rel in scn.files), scn.name


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


def test_a_ruby_object_address_is_normalised():
    text = (
        "private method 'load' called for #<Puppet::Parser::Scope:0x0000f43089b701e8>"
    )
    assert outcomes.normalize(text) == (
        "private method 'load' called for #<Puppet::Parser::Scope:0x0>"
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


_SCN = build("config", 1, 200)[0].name


def _row(n, kind="AGREE", rule=None, **job):
    ident = "config/{}::q{:03d}".format(_SCN, n)
    job = dict({"id": ident, "scn": _SCN, "qid": "q%03d" % n}, **job)
    return batch.Row(job, _found(n), _both(_found(n)), kind, rule)


def test_a_corpus_keeps_a_few_of_every_kind_and_a_seeded_sample_of_the_rest():
    rows = [_row(n) for n in range(100)]
    rows += [_row(100 + n, "STATUS", "puppet-crashes-hyera-answers") for n in range(9)]
    rows += [_row(200, "VALUE", "hocon-pyhocon-parser")]
    plan = corpus.Plan(1, 200, 10, 3)
    kept = corpus.select(rows, plan, "area")
    assert len(kept) == 10 + 3 + 1
    assert _row(200).job["id"] in kept
    assert kept == corpus.select(rows, plan, "area")
    assert kept != corpus.select(rows, plan._replace(seed=2), "area")


def test_a_stored_line_keeps_the_key_order_of_puppets_value():
    line = corpus._line({"q": "x", "p": {"value": {"b": 1, "a": 2}}})
    assert line.index('"b"') < line.index('"a"')
    assert "\u2028" not in corpus._line({"p": "a\u2028b"})


def test_a_volatile_scenario_is_marked_on_its_jobs_and_left_out_of_a_corpus():
    jobs = [j for j in make_jobs(build("backends", 1, 200)) if j.get("volatile")]
    assert {j["scn"] for j in jobs} == {"json-deep-nesting", "hocon-subst-env"}
    rec = corpus.Recording("config", 1, 200, [_row(1, volatile=True), _row(2)])
    lines = corpus.scenario_lines(rec, corpus.Plan(1, 200, 5, 3), ())
    assert [json.loads(l).get("q") for l in lines if '"q"' in l] == [
        "{}::q002".format(_SCN)
    ]


def test_a_query_whose_answer_looks_like_a_path_is_not_recorded():
    row = batch.Row(
        {"id": "area/s::q1", "scn": "s", "qid": "q1"},
        _found("c:\\path"),
        _both(_found("c:\\path")),
        "AGREE",
        None,
    )
    rec = corpus.Recording("backends", 1, 200, [row])
    assert not [
        l
        for l in corpus.scenario_lines(rec, corpus.Plan(1, 200, 5, 3), ())
        if '"q"' in l
    ]


def test_the_corpus_plan_covers_every_area():
    assert set(corpus.PLAN) == set(AREAS)
