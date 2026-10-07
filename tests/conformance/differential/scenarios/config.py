"""hiera.yaml (version 5) shapes: valid oddities and schema violations.

The README claims the version 5 schema is validated "with Puppet's own error
messages", so the message text is compared, not only the outcome.
"""

from __future__ import annotations

from ..scenario import Scn, scenario

H = "hierarchy:\n  - name: c\n    path: common.yaml\n"

CONFIGS = {
    # --- top level
    "minimal": "version: 5\n",
    "version-only-hier": "version: 5\n" + H,
    "no-version": H,
    "version-str": "version: '5'\n" + H,
    "version-float": "version: 5.0\n" + H,
    "version-4": "version: 4\n" + H,
    "version-7": "version: 7\n" + H,
    "version-neg": "version: -5\n" + H,
    "version-list": "version: [5]\n" + H,
    "unknown-top": "version: 5\nbogus: 1\n" + H,
    "unknown-top-two": "version: 5\nbogus: 1\nother: 2\n" + H,
    "sym-version": ":version: 5\n" + H,
    "backends-in-v5": "version: 5\n:backends: yaml\n" + H,
    "top-list": "- version: 5\n",
    "top-string": "version 5\n",
    "top-null": "~\n",
    "top-int": "5\n",
    "dup-version": "version: 5\nversion: 3\n" + H,
    "dup-hierarchy": "version: 5\n" + H + H,
    "tabs": "version: 5\nhierarchy:\n\t- name: c\n",
    "bom": b"\xef\xbb\xbfversion: 5\n" + H.encode(),
    "crlf": ("version: 5\n" + H).replace("\n", "\r\n").encode(),
    "multi-doc": "version: 5\n" + H + "---\nversion: 5\nhierarchy: []\n",
    "anchors": "version: 5\ndefaults: &d\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: common.yaml\n    <<: *d\n",
    "merge-key-level": "version: 5\nbase: &b {data_hash: yaml_data, datadir: data}\n"
    + H,
    "yes-keys": "version: 5\nhierarchy:\n  - name: yes\n    path: common.yaml\n",
    "interp-version": 'version: "%{facts.n}"\n' + H,
    # --- defaults
    "defaults-null": "version: 5\ndefaults: ~\n" + H,
    "defaults-empty": "version: 5\ndefaults: {}\n" + H,
    "defaults-list": "version: 5\ndefaults: []\n" + H,
    "defaults-string": "version: 5\ndefaults: x\n" + H,
    "defaults-unknown": "version: 5\ndefaults:\n  bogus: 1\n" + H,
    "defaults-path": "version: 5\ndefaults:\n  path: common.yaml\n" + H,
    "defaults-name": "version: 5\ndefaults:\n  name: x\n" + H,
    "defaults-two-fns": "version: 5\ndefaults:\n  data_hash: yaml_data\n  lookup_key: eyaml_lookup_key\n"
    + H,
    "defaults-lookup-key": "version: 5\ndefaults:\n  lookup_key: yaml_data\n" + H,
    "defaults-data-dig": "version: 5\ndefaults:\n  data_dig: yaml_data\n" + H,
    "defaults-options": "version: 5\ndefaults:\n  options: {a: 1}\n" + H,
    "defaults-options-with-level-fn": "version: 5\ndefaults:\n  data_hash: yaml_data\n  options: {a: 1}\nhierarchy:\n  - name: c\n    data_hash: json_data\n    path: common.json\n  - name: d\n    path: common.yaml\n",
    "defaults-datadir-int": "version: 5\ndefaults:\n  datadir: 5\n" + H,
    "defaults-datadir-null": "version: 5\ndefaults:\n  datadir: ~\n" + H,
    "defaults-datadir-empty": "version: 5\ndefaults:\n  datadir: ''\n" + H,
    "defaults-datadir-list": "version: 5\ndefaults:\n  datadir: [data]\n" + H,
    "defaults-fn-empty": "version: 5\ndefaults:\n  data_hash: ''\n" + H,
    "defaults-fn-null": "version: 5\ndefaults:\n  data_hash: ~\n" + H,
    "defaults-fn-int": "version: 5\ndefaults:\n  data_hash: 5\n" + H,
    "defaults-fn-unknown": "version: 5\ndefaults:\n  data_hash: nope_data\n" + H,
    "defaults-fn-upper": "version: 5\ndefaults:\n  data_hash: YAML_DATA\n" + H,
    "defaults-fn-ns": "version: 5\ndefaults:\n  data_hash: mymod::data\n" + H,
    "defaults-fn-bad-chars": "version: 5\ndefaults:\n  data_hash: 'yaml-data'\n" + H,
    "defaults-hiera3": "version: 5\ndefaults:\n  hiera3_backend: yaml\n" + H,
    # --- hierarchy
    "hier-null": "version: 5\nhierarchy: ~\n",
    "hier-string": "version: 5\nhierarchy: common\n",
    "hier-hash": "version: 5\nhierarchy:\n  name: c\n  path: common.yaml\n",
    "hier-empty": "version: 5\nhierarchy: []\n",
    "hier-entry-string": "version: 5\nhierarchy:\n  - common\n",
    "hier-entry-null": "version: 5\nhierarchy:\n  - ~\n",
    "hier-entry-empty": "version: 5\nhierarchy:\n  - {}\n",
    "hier-entry-list": "version: 5\nhierarchy:\n  - [name, c]\n",
    "no-name": "version: 5\nhierarchy:\n  - path: common.yaml\n",
    "name-empty": "version: 5\nhierarchy:\n  - name: ''\n    path: common.yaml\n",
    "name-int": "version: 5\nhierarchy:\n  - name: 5\n    path: common.yaml\n",
    "name-null": "version: 5\nhierarchy:\n  - name: ~\n    path: common.yaml\n",
    "name-list": "version: 5\nhierarchy:\n  - name: [a]\n    path: common.yaml\n",
    "name-dup": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n  - name: c\n    path: other.yaml\n",
    "name-dup-case": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n  - name: C\n    path: other.yaml\n",
    "name-only": "version: 5\nhierarchy:\n  - name: c\n",
    "name-only-json": "version: 5\nhierarchy:\n  - name: c\n    data_hash: json_data\n",
    "unknown-level-key": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    bogus: 1\n",
    "level-backend-v4key": "version: 5\nhierarchy:\n  - name: c\n    backend: yaml\n    path: common.yaml\n",
    "path-int": "version: 5\nhierarchy:\n  - name: c\n    path: 5\n",
    "path-null": "version: 5\nhierarchy:\n  - name: c\n    path: ~\n",
    "path-empty": "version: 5\nhierarchy:\n  - name: c\n    path: ''\n",
    "path-list": "version: 5\nhierarchy:\n  - name: c\n    path: [common.yaml]\n",
    "paths-string": "version: 5\nhierarchy:\n  - name: c\n    paths: common.yaml\n",
    "paths-empty": "version: 5\nhierarchy:\n  - name: c\n    paths: []\n",
    "paths-int": "version: 5\nhierarchy:\n  - name: c\n    paths: [1, common.yaml]\n",
    "paths-null-item": "version: 5\nhierarchy:\n  - name: c\n    paths: [~, common.yaml]\n",
    "paths-empty-item": "version: 5\nhierarchy:\n  - name: c\n    paths: ['', common.yaml]\n",
    "path-and-paths": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    paths: [other.yaml]\n",
    "path-and-glob": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    glob: '*.yaml'\n",
    "path-and-uri": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    uri: 'http://x'\n",
    "three-locs": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    glob: '*.yaml'\n    uri: 'http://x'\n",
    "glob-int": "version: 5\nhierarchy:\n  - name: c\n    glob: 5\n",
    "globs-string": "version: 5\nhierarchy:\n  - name: c\n    globs: '*.yaml'\n",
    "globs-empty": "version: 5\nhierarchy:\n  - name: c\n    globs: []\n",
    "uri-bad": "version: 5\nhierarchy:\n  - name: c\n    uri: 'not a uri %%'\n",
    "uri-ok-yaml": "version: 5\nhierarchy:\n  - name: c\n    uri: 'http://example.com/x'\n",
    "uris-mixed": "version: 5\nhierarchy:\n  - name: c\n    uris: ['http://example.com/x', 'bad uri']\n",
    "uri-relative": "version: 5\nhierarchy:\n  - name: c\n    uri: 'common.yaml'\n",
    "uri-file": "version: 5\nhierarchy:\n  - name: c\n    uri: 'file:///nonexistent'\n",
    "uri-int": "version: 5\nhierarchy:\n  - name: c\n    uri: 5\n",
    "uri-empty": "version: 5\nhierarchy:\n  - name: c\n    uri: ''\n",
    "mapped-string": "version: 5\nhierarchy:\n  - name: c\n    mapped_paths: facts.roles\n",
    "mapped-two": "version: 5\nhierarchy:\n  - name: c\n    mapped_paths: [facts.roles, r]\n",
    "mapped-four": "version: 5\nhierarchy:\n  - name: c\n    mapped_paths: [facts.roles, r, a, b]\n",
    "mapped-empty-items": "version: 5\nhierarchy:\n  - name: c\n    mapped_paths: ['', '', '']\n",
    "mapped-and-path": "version: 5\nhierarchy:\n  - name: c\n    mapped_paths: [facts.arr, r, '%{r}.yaml']\n    path: common.yaml\n",
    "datadir-int": "version: 5\nhierarchy:\n  - name: c\n    datadir: 5\n    path: common.yaml\n",
    "datadir-null": "version: 5\nhierarchy:\n  - name: c\n    datadir: ~\n    path: common.yaml\n",
    "datadir-empty": "version: 5\nhierarchy:\n  - name: c\n    datadir: ''\n    path: common.yaml\n",
    "datadir-with-uri": "version: 5\nhierarchy:\n  - name: c\n    datadir: data\n    uri: 'http://x'\n",
    "two-fns": "version: 5\nhierarchy:\n  - name: c\n    data_hash: yaml_data\n    lookup_key: eyaml_lookup_key\n    path: common.yaml\n",
    "three-fns": "version: 5\nhierarchy:\n  - name: c\n    data_hash: yaml_data\n    lookup_key: eyaml_lookup_key\n    data_dig: x\n    path: common.yaml\n",
    "fn-unknown": "version: 5\nhierarchy:\n  - name: c\n    data_hash: nope_data\n    path: common.yaml\n",
    "fn-unknown-missing-file": "version: 5\nhierarchy:\n  - name: c\n    data_hash: nope_data\n    path: missing.yaml\n  - name: d\n    path: common.yaml\n",
    "fn-kind-mismatch": "version: 5\nhierarchy:\n  - name: c\n    lookup_key: yaml_data\n    path: common.yaml\n",
    "fn-kind-mismatch-dig": "version: 5\nhierarchy:\n  - name: c\n    data_dig: yaml_data\n    path: common.yaml\n",
    "fn-kind-mismatch-hash": "version: 5\nhierarchy:\n  - name: c\n    data_hash: eyaml_lookup_key\n    path: common.yaml\n",
    "fn-kind-mismatch-missing-file": "version: 5\nhierarchy:\n  - name: c\n    lookup_key: yaml_data\n    path: missing.yaml\n  - name: d\n    path: common.yaml\n",
    "fn-empty": "version: 5\nhierarchy:\n  - name: c\n    data_hash: ''\n    path: common.yaml\n",
    "fn-int": "version: 5\nhierarchy:\n  - name: c\n    data_hash: 5\n    path: common.yaml\n",
    "fn-upper": "version: 5\nhierarchy:\n  - name: c\n    data_hash: Yaml_Data\n    path: common.yaml\n",
    "fn-leading-colons": "version: 5\nhierarchy:\n  - name: c\n    data_hash: '::yaml_data'\n    path: common.yaml\n",
    "fn-puppet-function": "version: 5\nhierarchy:\n  - name: c\n    data_hash: lookup\n    path: common.yaml\n",
    "fn-builtin-other": "version: 5\nhierarchy:\n  - name: c\n    data_hash: sprintf\n    path: common.yaml\n",
    "fn-module-missing": "version: 5\nhierarchy:\n  - name: c\n    data_hash: mymod::data\n    path: common.yaml\n",
    "fn-sops": "version: 5\nhierarchy:\n  - name: c\n    data_hash: sops_data\n    path: common.yaml\n",
    "fn-yaml-no-path": "version: 5\nhierarchy:\n  - name: c\n    data_hash: yaml_data\n    options: {a: 1}\n",
    "fn-eyaml-no-keys": "version: 5\nhierarchy:\n  - name: c\n    lookup_key: eyaml_lookup_key\n    path: common.yaml\n",
    "fn-eyaml-missing-file": "version: 5\nhierarchy:\n  - name: c\n    lookup_key: eyaml_lookup_key\n    path: missing.yaml\n  - name: d\n    path: common.yaml\n",
    "fn-hiera3-level": "version: 5\nhierarchy:\n  - name: c\n    hiera3_backend: yaml\n    path: common\n    datadir: data\n",
    "fn-hiera3-level-ext": "version: 5\nhierarchy:\n  - name: c\n    hiera3_backend: yaml\n    paths: [common, nope]\n    datadir: data\n    options: {extension: yaml}\n",
    "fn-hiera3-unknown": "version: 5\nhierarchy:\n  - name: c\n    hiera3_backend: bogus\n    path: common\n",
    "fn-hiera3-json": "version: 5\nhierarchy:\n  - name: c\n    hiera3_backend: json\n    path: common\n    datadir: data\n",
    "options-string": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: x\n",
    "options-list": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: [a]\n",
    "options-null": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: ~\n",
    "options-empty": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {}\n",
    "options-path-key": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {path: other.yaml}\n",
    "options-uri-key": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {uri: 'http://x'}\n",
    "options-int-key": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {1: a}\n",
    "options-bad-key": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {'a-b': 1}\n",
    "options-upper-key": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {Abc: 1}\n",
    "options-nested": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {a: {b: [1, ~, true, 1.5]}}\n",
    "options-with-lookup-in": "version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {a: \"%{lookup('k')}\"}\n",
    "options-undefined-var": 'version: 5\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {a: "%{nope}"}\n',
    "default-hier-global": "version: 5\n"
    + H
    + "default_hierarchy:\n  - name: d\n    path: common.yaml\n",
    "plan-hier-global": "version: 5\n"
    + H
    + "plan_hierarchy:\n  - name: d\n    path: other.yaml\n",
    "plan-hier-bad": "version: 5\n"
    + H
    + "plan_hierarchy:\n  - name: d\n    bogus: 1\n",
    "plan-hier-dup-name": "version: 5\n"
    + H
    + "plan_hierarchy:\n  - name: c\n    path: other.yaml\n",
    "level-fn-overrides-default-options": "version: 5\ndefaults:\n  data_hash: yaml_data\n  datadir: data\n  options: {a: 1}\nhierarchy:\n  - name: c\n    path: common.yaml\n",
    "level-options-no-fn": "version: 5\ndefaults:\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: common.yaml\n    options: {a: 1}\n",
    "second-level-bad": "version: 5\nhierarchy:\n  - name: ok\n    path: common.yaml\n  - name: bad\n    bogus: 1\n",
    "second-level-two-errors": "version: 5\nbogus: 1\nhierarchy:\n  - name: ok\n    path: common.yaml\n    nope: 1\n  - name: 5\n    path: [x]\n",
    "interp-name": 'version: 5\nhierarchy:\n  - name: "%{facts.a}"\n    path: common.yaml\n  - name: alpha\n    path: other.yaml\n',
    "interp-fn": 'version: 5\nhierarchy:\n  - name: c\n    data_hash: "%{facts.fn}"\n    path: common.yaml\n',
    "long-hierarchy": "version: 5\nhierarchy:\n"
    + "".join("  - name: l%d\n    path: l%d.yaml\n" % (i, i) for i in range(60))
    + "  - name: c\n    path: common.yaml\n",
    "ext-yml": "version: 5\nhierarchy:\n  - name: c\n    path: other.yml\n",
    "ext-none": "version: 5\nhierarchy:\n  - name: c\n    path: common\n",
    "ext-eyaml-as-yaml": "version: 5\nhierarchy:\n  - name: c\n    path: common.eyaml\n",
}


@scenario
def configs(ctx):
    for name, cfg in CONFIGS.items():
        s = Scn("cfg-" + name, "config", "hiera.yaml: " + name)
        s.facts(
            "a: alpha\nrole: web\nn: 5\nfn: yaml_data\narr: [common]\nroles: [common]\n"
        )
        s.hiera(cfg)
        s.file("data/common.yaml", "k: common\nl: [common]\n")
        s.file("data/common.json", '{"k": "common.json", "l": ["common.json"]}\n')
        s.file("data/other.yaml", "k: other\nl: [other]\n")
        s.file("data/other.yml", "k: other_yml\n")
        s.file("data/common", "k: noext\n")
        s.file("data/common.eyaml", "k: eyaml_plain\n")
        s.q("k")
        s.q("l", merge="unique")
        s.q("nope")
        s.q("k", args=["--strict", "error"])
        yield s
