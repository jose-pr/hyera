# hyera

A small, dependency-light Python implementation of [Puppet
Hiera](https://help.puppet.com/core/8/Content/PuppetCore/hiera_intro.htm) hierarchical data
lookup — a `src/hyera` packaged library plus an optional `hyera` CLI, built on
the `duho`/`pathlib_next` stack. The distribution, the import package and the
console script are all `hyera` (`pip install hyera`, `import hyera`, `hyera`
or `python -m hyera`).

The goal is the same lookup outcome and value as Puppet 8's own `lookup`
(the reference is Puppet 8.10.0 as measured): a lookup finds, misses or fails
where Puppet's does and returns the same value. Error message text, the
command's output text and its exit statuses (`1` miss, `2` error) are hyera's
own. Every deliberate difference is listed in
[`README.md#differences-from-puppet`](README.md#differences-from-puppet); the
per-feature fidelity table is
[`README.md#hiera-coverage`](README.md#hiera-coverage).

| Shipped header | Covers |
| --- | --- |
| [`src/hyera/AGENTS.md`](src/hyera/AGENTS.md) | the whole public API: `hyera`, `hyera.exceptions`, `hyera.types`, `hyera.backends` and `hyera.cli` with every signature, argument, contract and gotcha, the exception classes, the environment variables and the differences from Puppet |

## Layout

```
src/hyera/
├── __init__.py            # public re-exports (see src/hyera/AGENTS.md for the header)
├── types.py                 # hyera.types: the public Puppet type objects (Integer, Optional, Struct, ...)
├── _enums.py                  # the base of the public string enums (Merge, Strict, FunctionKind, BackendKind, RenderAs)
├── _digits.py                   # decimal integer parsing/formatting independent of Python's 4300-digit limit
├── _subprocess.py                 # the one place sops and facter run: timeout, process-tree kill, closed stdin
├── __main__.py             # python -m hyera: the same entry point as the console script
├── py.typed                 # PEP 561 marker: the package ships inline types
├── AGENTS.md                 # the shipped API header -- every export, signature and gotcha
├── core.py                    # Hiera: the public class -- construction, cache control, lookup, dig/get/getvar, explain, sources (data_provider.rb, functions/{lookup,dig,get,getvar}.rb)
├── exceptions.py                # HieraError -> ConfigError, BackendError, HieraLookupError (InterpolationError, MergeError, KeyNotFoundError)
├── cli/                           # the hyera console script on duho: hyera.cli exports main and Lookup
│   ├── __init__.py                  # Lookup (the duho command) and main()
│   ├── __main__.py                   # python -m hyera.cli
│   ├── _args.py                       # the duho.Arg annotation of every Lookup field and its agent help
│   ├── _argv.py                        # argument-vector preparation: values that look like options
│   ├── _options.py                      # merge-option validation in Puppet's order
│   ├── _run.py                           # one lookup: build the Hiera, resolve, render
│   ├── _scope.py                          # scope, facts and path-list handling
│   └── _stdout.py                          # writing the result to stdout
├── _config/                  # base hiera.yaml, hierarchy/location resolution, layer discovery
│   ├── __init__.py           # package marker
│   ├── config_source.py      # a hiera.yaml's source, its error messages and its version dispatch (hiera_config.rb, issues.rb)
│   ├── config_v5.py          # Hiera 5 schema validation (hiera_config.rb, issues.rb)
│   ├── config_v4.py          # the Hiera 4 dialect: defaults, validation, levels (hiera_config.rb)
│   ├── config_v3.py          # the Hiera 3 dialect: defaults, validation, levels, the code directory (hiera_config.rb, type_mismatch_describer.rb, run_mode.rb)
│   ├── hiera_config.py       # FunctionKind, HieraLevel, base config reading (hiera_config.rb)
│   ├── level_builder.py      # HieraLevel objects from a validated config's hierarchy entries (hiera_config.rb)
│   ├── location_resolver.py  # hierarchy level path resolution: interpolation rules, mapped_paths, uris, glob specs (location_resolver.rb, hiera_config.rb, uri's rfc3986_parser.rb)
│   ├── pathname.py           # Ruby's Pathname#+ and the Windows anchor rules host paths follow (pathname.rb)
│   ├── dir_glob.py           # Ruby's Dir.glob over a literal root: braces, fnmatch, the walk
│   └── data_provider.py      # global/environment/module layer discovery and per-layer config loading (lookup_adapter.rb, environment_data_provider.rb, module_data_provider.rb)
├── _lookup/                  # dispatch, interpolation, merge, navigation, caching
│   ├── __init__.py           # package marker
│   ├── function_provider.py  # LookupContext, the contexts and the helpers the providers share (function_provider.rb, context.rb, data_provider.rb)
│   ├── provider_classes.py   # the data_hash/lookup_key/data_dig provider classes and PROVIDER_CLASSES ({data_hash,lookup_key,data_dig}_function_provider.rb, function_provider.rb)
│   ├── cache.py              # scope-keyed caching: Puppet's scope-interpolation stability check (hiera_config.rb, context.rb)
│   ├── locations.py          # _LocationStore: resolved locations, glob listings and parsed data files, shared by an instance and its views (data_hash_function_provider.rb)
│   ├── providers.py          # per-level function providers: build, refresh while revalidating, and the sources() walk (hiera_config.rb)
│   ├── lookup_options.py     # gather and compose lookup_options across the global, environment and module layers; the default_hierarchy lookup (lookup_adapter.rb, module_data_provider.rb)
│   ├── navigation.py         # dotted-key sub-navigation: split_key/sub_lookup (sub_lookup.rb, lookup_key.rb)
│   ├── interpolation.py      # the %{...} engine: resolving functions and variable references (interpolation.rb)
│   ├── invocation.py         # per-lookup state for interpolation: scope, sub-lookup and recursion stack (invocation.rb)
│   ├── merge_strategy.py     # Merge and the merge strategies (merge_strategy.rb)
│   ├── deep_merge.py         # the deep_merge gem's deep_merge! and the Ruby value helpers the strategies share (deep_merge core.rb)
│   ├── layer_walk.py         # the lookup walk, bound as Hiera's private methods: levels, the global/environment/module layers, lookup_options, merge, sub-key (lookup_adapter.rb)
│   ├── lookup_adapter.py     # lookup_options matching + convert_result (lookup_adapter.rb)
│   ├── lookup_function.py    # the public lookup() call: dispatch + precedence (functions/lookup.rb, pops/lookup.rb)
│   └── data_functions.py     # dig, get, getvar: navigation over a looked-up value or the scope (functions/dig.rb, get.rb, getvar.rb)
├── _types/                   # the Puppet type model: type objects, parsing, mismatch, conversion
│   ├── __init__.py           # package marker
│   ├── types.py              # Any, the scalar types, SensitiveType, Sensitive, convert_to (types.rb, type_calculator.rb, type_formatter.rb, p_sensitive_type.rb)
│   ├── compound_types.py     # Collection, Array, Hash, Tuple, Struct, Variant, Runtime, TypeAlias and the static aliases (types.rb, static_loader.rb)
│   ├── inference.py          # infer, infer_set and their generalizing variants (type_calculator.rb)
│   ├── literal_format.py     # puppet_quote, range bounds and size arguments rendered as Puppet literals (string_converter.rb)
│   ├── ruby_regexp.py        # translating a Ruby (Onigmo) regexp into Python's re
│   ├── type_syntax.py        # the lexer and recursive-descent reader of type expressions (type_parser.rb)
│   ├── type_names.py         # the bare, never-parameterized and unsupported type names (type_parser.rb)
│   ├── parser.py             # parse_type, build_access, as_type: the builders that interpret the reader's nodes (type_parser.rb)
│   ├── mismatch.py           # type mismatch messages and instance assertion (type_mismatch_describer.rb, type_asserter.rb)
│   ├── string_converter.py   # value-to-string conversion: String.new()'s engine (string_converter.rb)
│   └── new_function.py       # new_instance: Puppet's new() plus each type's own new_function (functions/new.rb)
├── _scope/                   # scope and fact sources bound to a lookup
│   ├── __init__.py           # package marker
│   ├── scope.py              # Scope, Strict: top-scope variables, the lookup rules and the collision warning (compiler.rb, node.rb, scope.rb)
│   ├── scope_data.py         # validating and sanitizing variables, facts and trusted data (scope.rb, node/facts.rb, trusted_information.rb)
│   └── facts.py              # load_facts, facts_from_facter: --facts file rules and bare facter (application/lookup.rb, util/yaml.rb)
├── _output/                  # the explain tree and CLI render backends
│   ├── __init__.py           # package marker
│   ├── explain.py            # Explainer, ExplainResult and the debug wrapper (pops/lookup/explainer.rb)
│   ├── explain_nodes.py      # the explain tree's node classes and their rendering (explainer.rb)
│   ├── explain_refs.py       # the provider and location references a node carries, and the value dump (explainer.rb, configured_data_provider.rb)
│   └── render.py             # s/json/yaml CLI render backends: puppet lookup --render-as output
└── backends/                 # self-registering Backend registry (same import path: hyera.backends)
    ├── __init__.py           # Backend, registry, default_backends; re-exports every public backend class
    ├── _yaml.py              # YAMLBackend, Puppet-only (functions/yaml_data.rb)
    ├── _json.py              # JSONBackend
    ├── _hocon.py             # HOCONBackend, has_hocon (the private pyhocon parser copy)
    ├── _hocon_includes.py    # the HOCON include-directive scanner: refuse or allow each form
    ├── _sops.py              # SopsBackend, DotenvBackend
    ├── _eyaml.py             # EyamlBackend: token scanning and PKCS7 key loading (eyaml_lookup_key.rb, hiera-eyaml's encrypted_tokens.rb, parser.rb)
    ├── _pkcs7.py             # a bounds-checked BER/DER reader and the cert-free PKCS7 decrypt (hiera-eyaml's pkcs7.rb)
    ├── _psych.py             # RubySymbol, symkeys_to_string and Psych's scalar scanner (scalar_scanner.rb, hiera_config.rb)
    └── _psych_loader.py      # safe_load: the PyYAML loader wired to Psych's rules (to_ruby.rb, util/yaml.rb)

tests/
├── conftest.py              # make_tree: a valid Hiera 5 tree on disk, LF/UTF-8, per test
├── test_*.py                 # unit tests, one module per engine area (backends, config, merge, interpolation, CLI, ...)
├── data/                       # tables recorded from Ruby (Dir.glob, Pathname#+), replayed by test_dir_glob.py and test_locations.py
├── typing/                      # consumer_types.py: type-checked with pyright, never executed
└── conformance/
    ├── _golden.py             # golden schema/digest/lint (no hyera import)
    ├── _ours.py                 # the only module that calls into hyera's API/CLI
    ├── record.py                 # recorder (dev-only, needs real Puppet)
    ├── test_conformance.py        # replay: the API/library channel
    ├── test_conformance_cli.py     # replay: the CLI channel
    ├── test_golden_lint.py          # the golden linter's rules
    ├── test_record.py                # the recorder's own logic (needs no Puppet)
    ├── puppet_modules/hyera_fixture/  # Puppet module the apply channel loads: emit() and a facts terminus
    └── cases/<area>-<topic>/        # a hand-written case.yaml + a generated golden.json per case

benchmarks/
├── README.md            # schema + reproduce command
├── run.py                 # the benchmark runner (--save writes benchmarks/results/)
└── results/                 # tracked, committed JSON results (one per version/interpreter/config)

examples/
├── README.md         # how to run the example, and its expected output
├── hiera.yaml          # a runnable Hiera 5 hierarchy (nodes/os/role/common)
├── facts.yaml            # a puppet lookup --facts-shaped fact file
├── lookup.py                # loads the facts, builds a Hiera, prints five values
└── data/                       # the data tree the hierarchy reads

docs/
├── index.md            # the hand-written landing page (its examples are tested, see Checks)
├── changelog.md          # snippet-embeds CHANGELOG.md
└── api/                     # one `:::` mkdocstrings page per public module (hyera, hyera.exceptions, hyera.types, hyera.backends, hyera.cli)

.github/workflows/
├── test.yml            # on-demand test matrix, types, floors, coverage, format, docs, console-script, benchmark
├── release.yml           # v* tag: test + floors -> build -> wheel-smoke + docs-gate -> github-release -> publish-pypi / docs-deploy
└── docs.yml                 # every GitHub Pages deploy

mkdocs.yml                   # docs site config (MkDocs + Material + mkdocstrings)
pyproject.toml                 # hatchling build, extras, pytest/black config
CHANGELOG.md                     # Keep a Changelog; [Unreleased] is the only section this repo edits
README.md                          # the PyPI long description and the user guide
LICENSE, NOTICE, LICENSES/            # MIT for original code; NOTICE credits every upstream this project ports or derives from (see License below)
.gitattributes                          # * text=auto eol=lf
```

### How it fits together

`Hiera(base_config, ...)` loads a Hiera 5 base config (path, file-like, or
dict) and builds a `HieraLevel` per hierarchy entry (each pairing a `Backend`
with its source path template(s)); reading only happens then — no data file
is read until the first lookup that needs it. `Hiera.lookup(name, ...)`
(Puppet's own `lookup()`, also reachable as `h(...)`/`h[...]`/`name in h`)
resolves each candidate name's *root* key against the hierarchy, nested the
way Puppet's provider stack does: locations within a level, levels within
the hierarchy, then the global/environment/module layer stack
(`_config/data_provider.py`) — reducing at each layer with a `MergeStrategy`
(first-match by default), fully resolving interpolation and hiera function
calls in the found root value *before* it is merged (`_lookup/layer_walk.py` resolves each
level in turn, then merges — it never accumulates raw values across levels
and resolves them afterward), then digging any dotted sub-key out of the
merged result exactly once.

Backends register under one or more Hiera `data_hash`/`lookup_key`/
`data_dig` names (see the "Backends" table in
[`README.md`](README.md#backends)) and only need to implement
`read_file`/`load` (or, for a provider backend, a function hook taking a
`hyera.LookupContext`); every backend parses into plain `dict`/`list`, and
`_lookup/navigation.py` (`split_key`/`sub_lookup`, ported from Puppet's own
`sub_lookup.rb`) is what makes an `"a.b.0.c"`-style dotted key or `%{...}`
reference navigate that data uniformly, rather than a container method.

The CLI (`src/hyera/cli/`) is a thin `duho.Cli` wrapper around
`Hiera.lookup` that takes `puppet lookup`'s flags (see
[`README.md`](README.md#command-line)), designed for unattended use: no
interactive prompts, deterministic output through the `_output/render.py` registry,
and its own exit codes: `0` (found), `1` (key missing) and `2` (any other
error), `130` on an interrupt.
`HYERA_MCP=stdio hyera` serves the same command as an MCP tool over stdio.

## Environment

- venvs: `.venv/3.14-nt-amd64` and `.venv/3.9-nt-amd64`, named
  `<version>-<os>-<arch>` (both interpreter bounds are supported and kept
  green).
- Editable install: `<py> -m pip install -e ".[dev,docs]"`. `dev` composes
  every extra that has tests depending on it (`cli`, `hocon`, `eyaml`) plus
  `build`/`pytest`/`twine`/`coverage`/`pyright[nodejs]` and, on Python 3.10+, `black`
  (pinned to major 26); `docs` adds the MkDocs toolchain (Python 3.10+ only
  — install it on the 3.14 venv, not the 3.9 floor).

## Checks

- Tests: `<py> -m pytest -q -rs` (pytest config in `pyproject.toml` puts
  `src/` on the path). `tests/test_readme.py` runs the README's `pycon`,
  `console` and `sh` blocks, so a command that stops working turns it red.
- Coverage: `<py> -m coverage run --branch --source=src/hyera -m pytest -q
  -rs`, then `<py> -m coverage report --show-missing`. CI reports branch
  coverage on every run (the `coverage` job, Python 3.14 only) without
  gating on it — a drift signal, not a check that can fail the run.
- **Conformance goldens** (`tests/conformance/`): replay needs no Puppet,
  `<py> -m pytest -q -rs tests/conformance`. Recording needs Puppet 8.10's
  `puppet lookup`, local or in WSL: `<py> tests/conformance/record.py
  --runner {local|wsl} [--jobs N] [CASE ...]`; add `--check` to re-record in
  memory and diff against the committed goldens, or `--list-markers` to list
  every marker without needing Puppet at all. A case/query's `divergence:`
  marker is a strict `xfail` against a real bug still to fix — turning it
  green (`XPASS(strict)`) means the fix landed and the marker must come out;
  a `deviation:` marker is a permanent, asserted-as-passing documented
  difference, tested against `README.md`'s "Differences from Puppet" list
  by `tests/test_readme.py::test_differences_match_deviations`.
  A query with `expression:` instead of `key:` is recorded by `puppet apply`
  rather than `puppet lookup`: the Puppet source in `expression:` is wrapped
  in `hyera_fixture::emit(...)`, which writes its value as Puppet's own
  rich-data JSON, and the facts come from the case's `facts.yaml` through the
  fixture's facts terminus. Its `python:` field is a hand-written call spec
  (`target` `hiera` or `types`, `method`, `args`, `kwargs`, `params` for a
  subscripted type, `block` naming a callable of `_ours.BLOCKS`) that the API
  replay runs; the command channel skips it. It needs an `id:`, takes no
  `merge`, `default`, `type`, `explain` or `hash_inspect`, and is recorded
  like any case (`record.py --runner wsl apply-lookup-forms`). The `apply-*`
  cases hold these queries.
- Format: the exact command CI runs —
  `<py> -m black --check src/ tests/ benchmarks/ examples/` — on the newest
  interpreter (the 3.14 venv) only: the 3.9 venv has no black, and an older
  black major formats some files differently.
- Type gate: the exact command CI runs —
  `<py> -m pyright --pythonpath "$(which python)" --verifytypes hyera --ignoreexternal`
  (must report 100% and no public symbol without a docstring; CI runs it on
  Python 3.9 and 3.14).
- Docs: `<py> -m mkdocs build --strict` (3.14 venv). `docs/index.md` is
  hand-written; its runnable examples and extras table are checked by
  `tests/test_docs_examples.py`. `docs/api/` holds exactly one `:::` page
  per public module — renaming or removing one updates both its page and
  `mkdocs.yml`'s nav. `docs/changelog.md` snippet-embeds `CHANGELOG.md`.
- Benchmarks: `<py> benchmarks/run.py [--save]` (see `benchmarks/README.md`
  for the JSON schema). Compare only same-machine results; a local number
  never backs a release claim on its own.

## Conventions

`hyera._*` modules are private engine internals mirroring Puppet's own file
split; import public names from `hyera` itself, or from the public modules
`hyera.exceptions`, `hyera.types`, `hyera.backends` and `hyera.cli`, never
from a `hyera._*` module. `src/hyera/AGENTS.md` is the shipped API header (see
[Packaging](#packaging) below) — every export with its exact signature,
arguments and gotchas, so a consuming agent skips the source.

`pathlib_next.Path` is used throughout instead of stdlib `pathlib`; glob
levels use hyera's own Ruby `Dir.glob` port in `_config/dir_glob.py`, never
`Path.glob`.

### Packaging

Built with `hatchling`. `src/hyera/AGENTS.md` (the shipped API header) and
`README.md` both ship in the sdist and the wheel; the root `AGENTS.md` you
are reading does not — it is a development-only, repo-root file. The
version lives in exactly one place, `src/hyera/__init__.py`'s
`__version__`; `[tool.hatch.version]` reads it directly (no import, no
installed-metadata lookup) to build the package. Bump it in the same commit
as the matching `## [x.y.z]` `CHANGELOG.md` heading.

### License

MIT, for this project's own code. Several modules port code translated
from [Puppet](https://github.com/puppetlabs/puppet) (Apache-2.0), the
[deep_merge](https://github.com/danielsdeleo/deep_merge) gem (MIT), from
[Psych](https://github.com/ruby/psych) (MIT), Ruby's YAML library, and from
Ruby's [uri](https://github.com/ruby/uri) library (2-clause BSDL); those
files carry their own notice. See `NOTICE` and `LICENSES/`.

## Releasing

Three workflow files, one per concern — test, release, docs — so a release
is never the first time the test suite or the docs build is exercised, and
the docs site can be redeployed without cutting a release.

- **`test.yml`**: `workflow_dispatch` (with a `ref` input) or a throwaway
  `ci-*` tag — nothing runs on an ordinary push. 19 jobs on every run: `test` (a
  10-leg OS/Python matrix: every supported Python on Ubuntu, the oldest and
  newest on Windows and macOS), `types` (`pyright --verifytypes`, Python
  3.9 and 3.14), `floors` (every declared dependency pinned to its
  `pyproject.toml` floor, on the oldest supported Python), `format`
  (`black --check`, black 26, on the newest Python), `coverage` ("Coverage report (not a gate)", Python
  3.14 only: branch coverage via `coverage run`/`report`, printed to the
  job's own step summary — `continue-on-error: true`, so a coverage-tool
  break never fails the run), `docs` (the same strict `mkdocs build` the
  release gates on), and `console-script` (build the wheel, install it into
  a clean venv, run the installed script — Ubuntu, Windows and macOS). A
  `benchmark` job (two legs, Python 3.9 and 3.14, both revalidate modes,
  results kept as artifacts, never a gate) is the only other job: it runs
  only on a dispatch with `benchmark` set or a `ci-bench-*` tag, so a run
  has 19 or 21 jobs. A
  `ci-*` tag is throwaway: give it a unique name, push it, poll the run,
  then delete it locally and on the remote.
- **`docs.yml`**: push to `main` touching `docs/`, `mkdocs.yml`, `src/` or
  `CHANGELOG.md`, and `workflow_dispatch` (a manual redeploy of any ref,
  and the one a release dispatches at its own tag). It owns every Pages
  deploy — `release.yml` never deploys docs itself, only gates on a strict
  build.
- **`release.yml`** (`v*` tag): `test` (a 6-leg matrix: Ubuntu/Windows/
  macOS × the oldest and newest supported Python) and `floors` (the same
  dependency-floor job `test.yml` runs) → `build` (checks the tag names the
  version actually built) → `wheel-smoke` (Python 3.9 and 3.14: install the
  built wheel bare into a clean venv, check it ships `py.typed` and
  `AGENTS.md` and that `import hyera` loads no extra); `docs-gate` (strict
  docs build, no deploy) needs only `test`; `github-release` (flagged
  pre-release when the tag's PEP 440 form says so) needs `build`,
  `wheel-smoke` and `docs-gate`; `publish-pypi` (every tag, pre-releases included, PyPI
  Trusted Publishing, no stored token) and `docs-deploy` (final
  tags only, dispatches `docs.yml` at the tag) follow it. A pre-release tag (`v1.0.0-rc.1`,
  `v0.0.0a0`) is uploaded to PyPI as a pre-release, which `pip install
  hyera` skips unless asked for (`--pre` or an exact pin); it does not
  redeploy the docs.
- **Owner-only prerequisites** (no claim is made here about their current
  state — check before assuming a release or a docs deploy will work):
  PyPI Trusted Publishing registered for the `hyera` project, this
  repository and `release.yml`, environment `pypi`; GitHub Pages set to
  build from GitHub Actions; the repository's default workflow permissions
  set to read and write; a tag deployment-branch policy (`v*`) on the
  `github-pages` environment.
- **Tagging discipline**: a `v*` tag is pushed only with the owner's
  explicit consent for that release and that version — publish is
  irreversible. A `ci-*` tag needs no such consent.
