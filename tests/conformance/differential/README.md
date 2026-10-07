# Differential comparison with Puppet

A development tool, not part of the package: it generates Hiera scenarios from a
seed, asks the reference Puppet (8.10.0, run on the reference machine) and hyera
the same questions, and says where the answers differ and whether a published
difference explains it. The recorded conformance cases under `../cases/` are
hand-written and fixed; this tool covers the space around them, in bulk.

It is not imported by `hyera` and not in the wheel. The sdist does carry it,
because it ships `tests/` whole.

## The one command

```bash
python tests/conformance/differential/run.py --runner wsl --area all --seed 1 --count 200
```

- `--runner local|wsl|wsl:<distro>`: where Puppet runs, as for `../record.py`. The
  default is `wsl` on Windows and `local` elsewhere. Puppet needs the `ruby`
  program and the `puppet` and `deep_merge` gems on that machine.
- `--area NAME ...|all`: one or more of the scenario areas below, or `merge`.
- `--seed N`, `--count M`: the generator's seed, and at most M scenarios per area
  (M merge cases for `merge`). The same two numbers give byte-identical trees on
  every platform and Python version.
- `--keep DIR`: keep the generated trees and driver files there (they are removed
  otherwise). A failing query can be re-run by hand in its directory.
- `--puppet-workers N`, `--hyera-workers N`: parallelism of each side.
- `--record-corpus`: write the replay corpus (see below).

One summary line is printed per area (scenarios, queries, how many agree, how
many a declared difference explains, how many a known open defect explains, how
many nothing explains), then the unclassified disagreements in full: the query's
argument tail, Puppet's outcome, and hyera's API and CLI outcomes. The status is
`0` when nothing is unclassified, `1` when something is, and `2` when the
reference could not be run.

Puppet is slow, and it is shared: the driver (`drive_puppet.rb`) runs every query
of a batch in one Ruby process, forking one `puppet lookup` per query.

## What is compared

Each query runs through hyera twice, in this process: the API (`Hiera.lookup`,
built the way the conformance replay builds it) and the CLI (`hyera.cli.main`
with the query's own arguments). A query with a flag only the CLI understands runs
through the CLI alone. The `--render-as yaml` and `s` variants of a scenario run
through the CLI only.

An outcome is `found` (with the value), `not_found` or `error`. Two sides
disagree when the status differs (`STATUS`), the values differ (`VALUE`), the
text or key order differs for equal values (`RENDER`), the API and CLI differ
(`CHANNEL`), or the API raised something that is not a `HieraError` (`CRASH`).
Two errors always agree: the error text is status-only, so a difference in
wording is counted under `error-message-text` and is not a disagreement.

The API driver treats what `puppet lookup` refuses before it looks anything up
(a fact named like a reserved variable, an unknown `--strict` value, an empty
facts file) as an error, and an empty `--type` as no type, so both channels answer
the question the command answers.

## Classification

`rules.py` holds one rule per declared difference. A rule has the id the README
publishes under "Differences from Puppet" and a predicate that decides whether
this disagreement is that difference. A few differences are published only as a
row of the README's "Not supported" table; such a rule names the row's text
instead, and a test checks both against the README. The first rule that accepts a
disagreement wins; a disagreement none accepts is unclassified.

`OPEN` in the same module holds defects that are known and not yet fixed or
declared. They are listed apart, do not fail a run, and each names what diverges.
Delete the entry when the defect is fixed: the corpus replay fails once hyera
stops diverging.

A classification is only as exact as its predicate. Read a new id's count in the
summary before trusting it, and tighten the predicate when it catches more than
the README describes.

## Areas and builders

Each module under `scenarios/` is an area; its `@scenario` functions are the
builders. A builder takes a context (`ctx.seed`, `ctx.count`, `ctx.rng(...)`) and
yields `Scn` objects.

| Area | Covers |
| --- | --- |
| `backends` | `json_data`, `hocon_data`, rendering in every format |
| `config` | `hiera.yaml` version 5 shapes and schema violations |
| `extra` | HOCON constructs, type expressions and interpolation keys, one per file |
| `interp` | variables, functions, nesting, strict modes, `hiera.yaml` interpolation |
| `interp_sweep` | one data key per interpolation expression shape |
| `keys` | key syntax, `--default`, `--type`, several keys, odd command lines |
| `layers` | global, environment and module layers, versions 3 and 4 |
| `locations` | `path`, `paths`, `glob`, `mapped_paths`, `uri`, per-level `datadir` |
| `lopts` | `lookup_options`: placement, patterns, precedence, shapes, `convert_to` |
| `strategies` | merge strategies through the hierarchy, seeded random data |
| `yaml_data` | YAML typing in data files and in facts |
| `merge` | `merge/`: the merge strategies directly, see below |

An area keeps at most `--count` scenarios. Builders are called in name order; when
an area yields more than `--count` scenarios a seeded sample of that size is kept.
Random data is drawn from `ctx.rng(...)`, which hashes its labels with SHA-512,
never from `hash()`, a set or the clock.

### Adding a builder

1. Write `@scenario def name(ctx):` in the area's module (or a new module, which
   `scenarios/__init__.py` imports and lists in `AREAS`).
2. Build a `Scn`: `Scn(name, family, description)`, then `.hiera(...)`,
   `.file(path, text_or_bytes)`, `.facts(...)`, and `.q(key, merge=, default=,
   type=, args=, id=)` for each query. Names must differ case-insensitively.
3. Keep it deterministic: sort what you iterate, take randomness from `ctx.rng`,
   and write text with `\n` only.
4. Run the area, then update the pinned digests in `../test_differential_tool.py`
   (the test says which area changed) and re-record the corpus.

## The merge harness

`merge/` merges generated value lists directly: `oracle.rb` runs each case through
Puppet's `MergeStrategy` (and so the `deep_merge` gem), `ours.py` through hyera's
port, with every strategy and option combination (`unconstrained_deep` and
`reverse_deep` included). Cases come from `merge/gen.py`: nested hashes, arrays of
hashes, mixed scalars, knockout prefixes, `sort_merged_arrays` and
`merge_hash_arrays`. `--count M` makes `M` cases of ordinary data and `M // 4`
whose hash keys Python cannot tell apart (`1`, `1.0`, `true`), which is the
declared `python-equal-hash-keys`.

```bash
python tests/conformance/differential/run.py --runner wsl --area merge --seed 1 --count 6000
```

## The corpus and its replay

`corpus/<area>.jsonl` holds Puppet's answers for one fixed seed and size per
area (`corpus.PLAN`), so the generator's reach is checked without Puppet. A
recording runs the whole area, then keeps a seeded sample of the plain
agreements and a few queries of every kind of classified difference, so each
declared difference and open defect the area reaches is in the corpus:

- line 1 is the header: format, area, seed, sizes, Puppet's, Ruby's and the gems'
  versions;
- a `{"scn", "sha"}` line pins the tree each following answer was recorded for;
- each query line holds its id, Puppet's outcome and, when it is not a plain
  agreement, the kind and the declared-difference id.

`../test_differential_corpus.py` rebuilds the scenarios from the seed in a
temporary directory, checks each digest, runs hyera and expects the recorded
verdict. A query a declared difference explains must still be explained by it, so
hyera's declared behaviour is asserted, not Puppet's answer. The replay runs on
every platform and Python version.

Re-record after a new Puppet release, a new or removed difference id, a new or
changed builder, or a change to the corpus plan:

```bash
python tests/conformance/differential/run.py --runner wsl --record-corpus
```

A recording runs every area in full, so it takes as long as the one command
above. It refuses to write while anything is unclassified, scans every answer with the
recorder's leak scan (no host name, address or path) and refuses a corpus over a
megabyte.

## Files

| File | Role |
| --- | --- |
| `run.py` | the command |
| `scenario.py`, `generate.py` | scenario trees, determinism, the job list |
| `scenarios/` | the builders |
| `drive_puppet.rb`, `reference.py` | Puppet's side: one Ruby process per batch |
| `drive_hyera.py` | hyera's side: the API and the CLI |
| `outcomes.py` | raw answers to outcomes, and the comparison |
| `rules.py` | declared differences and open defects |
| `batch.py` | one area's run and its report |
| `merge/` | the merge harness |
| `corpus.py`, `corpus/` | the recorded corpus |
