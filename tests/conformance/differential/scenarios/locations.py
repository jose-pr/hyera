"""Hierarchy location kinds mixed in one hierarchy; JSON and HOCON data."""

from __future__ import annotations

from ..scenario import Scn, d, scenario

FACTS = """\
a: alpha
role: web
roles: [web, db, "we b", ""]
roles_h: {web: 1, db: 2}
roles_s: single
roles_nested: [[x, y], {k: v}]
n: 42
b: true
empty: ""
nilv: ~
os: {family: RedHat, release: {major: "9"}}
star: "*"
qm: "?"
brackets: "[ab]"
braces: "{a,b}"
dotdot: ".."
slash: "sub/inner"
pct: "%{facts.role}"
"""

DATA_KEYS = ["k", "l", "h", "who"]


def _data(tag):
    return "k: {0}\nl: [{0}]\nh: {{{1}: 1}}\nwho: {0}\nonly_{1}: 1\n".format(
        tag, tag.replace("-", "_").replace("/", "_").replace(".", "_").replace(" ", "_")
    )


def _tree(s):
    for rel in [
        "common",
        "web",
        "db",
        "we b",
        "alpha",
        "RedHat",
        "roles/web",
        "roles/db",
        "roles/we b",
        "glob/a",
        "glob/b",
        "glob/Z",
        "glob/_x",
        "glob/.hidden",
        "glob/sub/deep",
        "glob/sub/deeper/x",
        "glob/web-1",
        "glob/web-2",
        "glob/10",
        "glob/9",
        "glob/é",
        "sub/inner",
        "ab/a",
        "ab/b",
        "ab/[ab]",
        "ab/{a,b}",
        "42",
        "true",
        "single",
        "x/k",
        "x/v",
        "nodes/alpha",
    ]:
        s.file("data/" + rel + ".yaml", _data(rel))
    s.file(
        "data/glob/c.json",
        '{"k": "glob/c.json", "l": ["glob/c.json"], "h": {"cjson": 1}, "who": "glob/c.json"}\n',
    )
    s.file("data/glob/notyaml.txt", "k: txt\n")
    s.file("data/glob/dir.yaml/inner.yaml", _data("dir.yaml/inner"))
    s.file("data/glob/empty.yaml", "")
    s.file(
        "data/common.json",
        '{"k": "common.json", "l": ["common.json"], "h": {"cj": 1}, "who": "common.json", "only_cj": 1}\n',
    )
    s.file(
        "data/common.conf",
        'k = "common.conf"\nl = ["common.conf"]\nh { hc = 1 }\nwho = common.conf\nonly_hc = 1\n',
    )
    s.file("data/web.json", '{"k": "web.json", "l": ["web.json"], "h": {"wj": 1}}\n')
    s.file("alt/common.yaml", _data("alt/common"))
    s.file("alt/web.yaml", _data("alt/web"))
    s.file("outside.yaml", _data("outside"))


HIERARCHIES = {
    "mix-all": """\
  - name: node
    path: "nodes/%{facts.a}.yaml"
  - name: roles mapped
    mapped_paths: [facts.roles, r, "roles/%{r}.yaml"]
  - name: glob
    glob: "glob/*.yaml"
  - name: json
    data_hash: json_data
    paths: ["%{facts.role}.json", "common.json"]
  - name: hocon
    data_hash: hocon_data
    path: common.conf
  - name: alt
    datadir: alt
    paths: ["%{facts.role}.yaml", common.yaml]
  - name: common
    path: common.yaml
""",
    "globs-many": """\
  - name: g1
    globs: ["glob/web-*.yaml", "glob/[ab].yaml", "glob/{a,b,B}.yaml"]
  - name: g2
    glob: "glob/**/*.yaml"
  - name: g3
    glob: "glob/?.yaml"
  - name: g4
    glob: "glob/*"
  - name: common
    path: common.yaml
""",
    "glob-order": """\
  - name: g
    glob: "glob/*.yaml"
""",
    "glob-recursive": """\
  - name: g
    glob: "**/*.yaml"
""",
    "glob-recursive2": """\
  - name: g
    glob: "glob/**/x.yaml"
  - name: g2
    glob: "glob/**"
  - name: g3
    glob: "**/deep.yaml"
""",
    "glob-dot": """\
  - name: g
    globs: ["glob/.*.yaml", "glob/.*", "glob/*.{yaml,json}", "glob/{.hidden,a}.yaml"]
""",
    "glob-nomatch": """\
  - name: g
    globs: ["nope/*.yaml", "glob/zz*.yaml"]
  - name: common
    path: common.yaml
""",
    "glob-literal": """\
  - name: g
    globs: ["glob/a.yaml", "common.yaml", "glob/missing.yaml"]
""",
    "glob-dir-match": """\
  - name: g
    glob: "glob/dir*"
  - name: common
    path: common.yaml
""",
    "glob-interp": """\
  - name: g
    globs: ["glob/%{facts.role}-*.yaml", "ab/%{facts.brackets}.yaml", "ab/%{facts.braces}.yaml", "glob/%{facts.star}.yaml", "glob/%{facts.nope}*.yaml"]
""",
    "glob-escape": """\
  - name: g
    globs: ["ab/\\\\[ab\\\\].yaml", "ab/[[]ab].yaml", "ab/\\\\{a,b\\\\}.yaml"]
  - name: common
    path: common.yaml
""",
    "glob-case": """\
  - name: g
    globs: ["glob/b.yaml", "glob/[A-Z].yaml", "glob/[a-z].yaml", "GLOB/a.yaml"]
""",
    "glob-braces-nested": """\
  - name: g
    globs: ["glob/{a,{b,Z}}.yaml", "glob/{web-{1,2},9}.yaml", "glob/{a,}.yaml", "{glob,ab}/a.yaml", "glob/{a.yaml,b.yaml}"]
""",
    "glob-classes": """\
  - name: g
    globs: ["glob/[!a].yaml", "glob/[^ab].yaml", "glob/[a-].yaml", "glob/[]a].yaml", "glob/[0-9]*.yaml", "glob/[[:digit:]].yaml"]
""",
    "glob-dotdot": """\
  - name: g
    globs: ["../outside.yaml", "glob/../common.yaml", "./glob/a.yaml", "glob//b.yaml", "glob/sub/../a.yaml"]
""",
    "glob-abs": """\
  - name: g
    glob: "/nonexistent/*.yaml"
  - name: common
    path: common.yaml
""",
    "glob-trailing-slash": """\
  - name: g
    globs: ["glob/*/", "glob/sub/"]
  - name: common
    path: common.yaml
""",
    "glob-empty-pattern": """\
  - name: g
    globs: ["", "glob/"]
  - name: common
    path: common.yaml
""",
    "glob-double-star-forms": """\
  - name: g
    globs: ["glob/**.yaml", "glob/**/", "**/sub/*.yaml", "glob/**/**/x.yaml", "**"]
""",
    "mapped-array": """\
  - name: m
    mapped_paths: [facts.roles, r, "roles/%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-hash": """\
  - name: m
    mapped_paths: [facts.roles_h, r, "x/%{r.0}.yaml"]
  - name: m2
    mapped_paths: [facts.roles_h, r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-scalar": """\
  - name: m
    mapped_paths: [facts.roles_s, r, "%{r}.yaml"]
  - name: m2
    mapped_paths: [facts.n, r, "%{r}.yaml"]
  - name: m3
    mapped_paths: [facts.b, r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-missing": """\
  - name: m
    mapped_paths: [facts.nope, r, "%{r}.yaml"]
  - name: m2
    mapped_paths: [facts.nilv, r, "%{r}.yaml"]
  - name: m3
    mapped_paths: [facts.empty, r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-nested": """\
  - name: m
    mapped_paths: [facts.roles_nested, r, "x/%{r.0}.yaml"]
  - name: m2
    mapped_paths: [facts.roles_nested, r, "x/%{r.k}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-topscope": """\
  - name: m
    mapped_paths: [roles, r, "roles/%{r}.yaml"]
  - name: m2
    mapped_paths: ["::roles", r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-shadow-var": """\
  - name: m
    mapped_paths: [facts.roles, role, "roles/%{role}.yaml"]
  - name: m2
    mapped_paths: [facts.roles, facts, "%{facts}.yaml"]
  - name: after
    path: "%{role}.yaml"
""",
    "mapped-other-vars": """\
  - name: m
    mapped_paths: [facts.roles, r, "roles/%{r}-%{facts.nope}.yaml"]
  - name: m2
    mapped_paths: [facts.roles, r, "%{facts.a}.yaml"]
  - name: m3
    mapped_paths: [facts.roles, r, "%{::r}.yaml"]
  - name: m4
    mapped_paths: [facts.roles, r, "%{r.x}.yaml"]
""",
    "mapped-var-forms": """\
  - name: m
    mapped_paths: ["facts.os.family", r, "%{r}.yaml"]
  - name: m2
    mapped_paths: ["facts.'roles'", r, "roles/%{r}.yaml"]
  - name: m3
    mapped_paths: ["facts.roles.0", r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-fn": """\
  - name: m
    mapped_paths: ["lookup('k')", r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-interp-var": """\
  - name: m
    mapped_paths: ["%{facts.role}", r, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "mapped-bad-arity": """\
  - name: m
    mapped_paths: [facts.roles, r]
  - name: common
    path: common.yaml
""",
    "mapped-bad-types": """\
  - name: m
    mapped_paths: [facts.roles, 1, "%{r}.yaml"]
  - name: common
    path: common.yaml
""",
    "paths-order": """\
  - name: p
    paths: [common.yaml, "%{facts.role}.yaml", "nope.yaml", "%{facts.role}.yaml"]
""",
    "paths-empty": """\
  - name: p
    paths: []
  - name: common
    path: common.yaml
""",
    "path-with-space": """\
  - name: p
    paths: ["we b.yaml", " web.yaml", "web.yaml "]
  - name: common
    path: common.yaml
""",
    "path-pct-fact": """\
  - name: p
    paths: ["%{facts.pct}.yaml", "%{facts.slash}.yaml", "%{facts.dotdot}/outside.yaml"]
  - name: common
    path: common.yaml
""",
    "per-level-datadir": """\
  - name: alt first
    datadir: alt
    path: "%{facts.role}.yaml"
  - name: abs-missing
    datadir: /nonexistent/hf
    path: common.yaml
  - name: dot
    datadir: .
    path: outside.yaml
  - name: dotdot
    datadir: data/..
    path: outside.yaml
  - name: interp
    datadir: "%{facts.nope}alt"
    path: common.yaml
  - name: common
    path: common.yaml
""",
    "json-first": """\
  - name: j
    data_hash: json_data
    paths: [web.json, common.json, nope.json]
  - name: common
    path: common.yaml
""",
    "hocon-first": """\
  - name: h
    data_hash: hocon_data
    paths: [common.conf, nope.conf]
  - name: common
    path: common.yaml
""",
    "wrong-format": """\
  - name: json reads yaml
    data_hash: json_data
    path: web.yaml
  - name: common
    path: common.yaml
""",
    "wrong-format2": """\
  - name: yaml reads json
    data_hash: yaml_data
    path: common.json
  - name: yaml reads hocon
    data_hash: yaml_data
    path: common.conf
""",
    "wrong-format3": """\
  - name: hocon reads yaml
    data_hash: hocon_data
    path: common.yaml
  - name: hocon reads json
    data_hash: hocon_data
    path: common.json
""",
    "uri-level": """\
  - name: u
    uri: "http://example.com/%{facts.role}"
  - name: common
    path: common.yaml
""",
    "no-location": """\
  - name: nothing
    data_hash: yaml_data
  - name: common
    path: common.yaml
""",
    "two-locations": """\
  - name: both
    path: common.yaml
    glob: "glob/*.yaml"
""",
    "dup-level-names": """\
  - name: same
    path: web.yaml
  - name: same
    path: common.yaml
""",
    "same-file-twice": """\
  - name: one
    path: common.yaml
  - name: two
    path: common.yaml
  - name: three
    glob: "comm*.yaml"
""",
}


@scenario
def locations(ctx):
    for name, hier in HIERARCHIES.items():
        s = Scn("loc-" + name, "locations", "location kinds: " + name)
        # Windows drops a trailing space, and case-insensitive filesystems match
        # a literal segment in any case
        s.volatile = name in ("path-with-space", "glob-case")
        s.facts(FACTS)
        s.hiera(
            "version: 5\ndefaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n"
            + hier
        )
        _tree(s)
        for k in DATA_KEYS:
            s.q(k)
        s.q("l", merge="unique")
        s.q("h", merge="hash")
        s.q("who", merge="unique")
        s.q("nope")
        s.q("l", merge="unique", args=["--strict", "error"])
        yield s
