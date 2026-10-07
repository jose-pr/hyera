"""Global / environment / module layers; version 3 and 4 hiera.yaml."""

from __future__ import annotations

from ..scenario import Scn, V5, d, scenario

ONE = V5 + "  - name: common\n    path: common.yaml\n"
TWO = (
    V5
    + '  - name: role\n    path: "roles/%{facts.role}.yaml"\n  - name: common\n    path: common.yaml\n'
)
V4 = """\
version: 4
datadir: data
hierarchy:
  - name: role
    backend: yaml
    path: "roles/%{facts.role}"
  - name: common
    backend: yaml
"""
V3 = """\
---
:backends:
  - yaml
:yaml:
  :datadir: data
:hierarchy:
  - "roles/%{::role}"
  - common
"""

LAYER_KEYS = [
    "g_only",
    "e_only",
    "shared",
    "shared_h",
    "shared_a",
    "mymod::a",
    "mymod::h",
    "mymod::l",
    "mymod::only_mod",
    "mymod::only_env",
    "mymod::only_global",
    "mymod::unprefixed_victim",
    "unprefixed",
    "othermod::x",
    "mymod",
    "mymod::",
    "::mymod::a",
    "Mymod::a",
    "mymod::sub::k",
    "mymod::h.m",
    "mymod::nope",
    "nomod::k",
    "envmod::k",
    "envmod::shadow",
    "mymod::ref_global",
    "mymod::ref_env",
    "mymod::ref_self",
    "g_ref_mod",
    "e_ref_mod",
    "g_ref_env",
    "mymod::dflt",
    "mymod::dflt_h",
    "mymod::dflt_shadowed",
    "mymod::conv",
    "mymod::pat_x",
    "lookup_options",
    "mymod::lookup_options",
    "mymod::interp_fact",
    "mymod::alias_g",
]


def _full(name, env_cfg=TWO, mod_cfg=None, global_cfg=ONE, mod_default=True):
    s = Scn(name, "layers", "three layers: " + name)
    s.hiera(global_cfg)
    s.file(
        "data/common.yaml",
        d("""\
            g_only: g
            shared: g
            shared_h: {g: 1, x: g}
            shared_a: [g, x]
            mymod::a: g
            mymod::h: {g: 1, m: g}
            mymod::l: [g]
            mymod::only_global: g
            g_ref_mod: "%{lookup('mymod::only_mod')}"
            g_ref_env: "%{lookup('e_only')}"
            mymod::dflt_shadowed: g_wins
            lookup_options:
              shared_h: {merge: hash}
              mymod::l: {merge: unique}
            """),
    )
    if env_cfg is not None:
        s.file("environments/production/hiera.yaml", env_cfg)
    s.file(
        "environments/production/data/common.yaml",
        d("""\
            e_only: e
            shared: e
            shared_h: {e: 1, x: e}
            shared_a: [e, x]
            mymod::a: e
            mymod::h: {e: 1, m: e}
            mymod::l: [e, g]
            mymod::only_env: e
            e_ref_mod: "%{lookup('mymod::only_mod')}"
            envmod::shadow: e
            lookup_options:
              shared_a: {merge: unique}
              mymod::h: {merge: deep}
              shared_h: {merge: first}
            """),
    )
    s.file(
        "environments/production/data/roles/web.yaml", "e_role: web\nshared: e_role\n"
    )
    s.file("environments/production/modules/envmod/hiera.yaml", ONE)
    s.file(
        "environments/production/modules/envmod/data/common.yaml",
        "envmod::k: envmod\nenvmod::shadow: m\n",
    )
    s.file("modules/envmod/hiera.yaml", ONE)
    s.file(
        "modules/envmod/data/common.yaml", "envmod::k: basemod\nenvmod::base_only: b\n"
    )
    mod_cfg = mod_cfg if mod_cfg is not None else ONE
    if mod_default and mod_cfg is ONE:
        mod_cfg = (
            ONE + "default_hierarchy:\n  - name: defaults\n    path: defaults.yaml\n"
        )
    if mod_cfg != "":
        s.file("modules/mymod/hiera.yaml", mod_cfg)
    s.file(
        "modules/mymod/data/common.yaml",
        d("""\
            mymod::a: m
            mymod::h: {mo: 1, m: m}
            mymod::l: [m, e]
            mymod::only_mod: m
            mymod::sub::k: sub
            unprefixed: leak
            othermod::x: leak
            mymod: bare
            "mymod::": trailing
            Mymod::a: upper
            mymod::ref_global: "%{lookup('g_only')}"
            mymod::ref_env: "%{lookup('e_only')}"
            mymod::ref_self: "%{lookup('mymod::only_mod')}"
            mymod::alias_g: "%{alias('shared_h')}"
            mymod::interp_fact: "%{facts.role}"
            mymod::conv: '12'
            mymod::pat_x: [m]
            lookup_options:
              mymod::conv: {convert_to: Integer}
              "^mymod::pat_": {merge: unique}
              mymod::l: {merge: first}
            """),
    )
    s.file(
        "modules/mymod/data/defaults.yaml",
        d("""\
            mymod::dflt: d
            mymod::dflt_h: {d: 1}
            mymod::dflt_shadowed: d_loses
            mymod::a: d_loses
            lookup_options:
              mymod::dflt_h: {merge: hash}
            """),
    )
    s.file("modules/mymod/data/roles/web.yaml", "mymod::role: web\nmymod::a: m_role\n")
    full = name == "layers-v5"
    keys = (
        LAYER_KEYS
        if full
        else [
            "g_only",
            "e_only",
            "shared",
            "shared_h",
            "shared_a",
            "mymod::a",
            "mymod::h",
            "mymod::l",
            "mymod::only_mod",
            "unprefixed",
            "mymod::sub::k",
            "envmod::k",
            "envmod::shadow",
            "mymod::ref_env",
            "g_ref_mod",
            "mymod::dflt",
            "mymod::dflt_shadowed",
            "mymod::conv",
            "mymod::pat_x",
            "lookup_options",
            "mymod::nope",
            "nomod::k",
        ]
    )
    for k in keys:
        s.q(k)
    mkeys = [
        "shared",
        "shared_h",
        "shared_a",
        "mymod::a",
        "mymod::h",
        "mymod::l",
        "mymod::dflt_h",
        "mymod::dflt",
        "mymod::pat_x",
        "envmod::k",
        "envmod::shadow",
    ]
    for k in (mkeys if full else ["shared_h", "mymod::h", "mymod::l", "mymod::dflt_h"]):
        for m in (["first", "unique", "hash", "deep"] if full else ["unique", "deep"]):
            s.q(k, merge=m)
    s.q("mymod::a", args=["--environment", "staging"])
    s.q("mymod::a", args=["--strict", "error"])
    s.q("e_only", args=["--strict", "error"])
    s.q("g_only", args=["--strict", "error"])
    if full:
        s.q("e_only", args=["--environment", "staging"])
        s.q(["nope", "mymod::dflt"])
        s.q("mymod::dflt", default="D")
        s.q("mymod::nope", default="D")
        s.q("mymod::conv", type="Integer")
    return s


@scenario
def layers(ctx):
    yield _full("layers-v5")
    yield _full("layers-env-v4", env_cfg=V4)
    yield _full("layers-env-v3", env_cfg=V3)
    yield _full("layers-env-nocfg", env_cfg=None)
    yield _full("layers-env-empty", env_cfg="")
    yield _full("layers-env-comment-only", env_cfg="# nothing\n")
    yield _full("layers-env-v5-nohier", env_cfg="version: 5\n")
    yield _full(
        "layers-env-default-hierarchy",
        env_cfg=ONE + "default_hierarchy:\n  - name: d\n    path: d.yaml\n",
    )
    yield _full("layers-env-broken", env_cfg="version: 5\nhierarchy: [unclosed\n")
    yield _full(
        "layers-env-bad-schema",
        env_cfg="version: 5\nhierarchy:\n  - name: x\n    bogus: 1\n",
    )
    yield _full("layers-env-v6", env_cfg="version: 6\n")
    yield _full(
        "layers-env-hiera3-backend",
        env_cfg="version: 5\nhierarchy:\n  - name: x\n    hiera3_backend: yaml\n    path: common.yaml\n",
    )
    yield _full("layers-mod-v4", mod_cfg=V4)
    yield _full("layers-mod-v3", mod_cfg=V3)
    yield _full("layers-mod-nocfg", mod_cfg="")
    yield _full("layers-mod-no-default", mod_default=False)
    yield _full("layers-mod-broken", mod_cfg="version: 5\nhierarchy: [unclosed\n")
    yield _full(
        "layers-mod-bad-schema",
        mod_cfg="version: 5\nhierarchy:\n  - name: x\n    bogus: 1\n",
    )
    yield _full("layers-mod-v5-nohier", mod_cfg="version: 5\n")
    yield _full(
        "layers-mod-hiera3-backend",
        mod_cfg="version: 5\nhierarchy:\n  - name: x\n    hiera3_backend: yaml\n    path: common.yaml\n",
    )
    yield _full(
        "layers-mod-plan-hierarchy",
        mod_cfg=ONE + "plan_hierarchy:\n  - name: p\n    path: defaults.yaml\n",
    )
    yield _full(
        "layers-mod-default-only",
        mod_cfg="version: 5\ndefault_hierarchy:\n  - name: defaults\n    path: defaults.yaml\n",
    )
    yield _full("layers-mod-default-empty", mod_cfg=ONE + "default_hierarchy: []\n")
    yield _full(
        "layers-mod-default-with-glob",
        mod_cfg=ONE + "default_hierarchy:\n  - name: d\n    glob: 'def*.yaml'\n",
    )
    yield _full("layers-global-v3", global_cfg=V3.replace("roles/%{::role}", "nothing"))
    yield _full("layers-global-v4", global_cfg=V4)
    yield _full(
        "layers-global-default-hierarchy",
        global_cfg=ONE + "default_hierarchy:\n  - name: d\n    path: d.yaml\n",
    )
    yield _full(
        "layers-global-plan-hierarchy",
        global_cfg=ONE + "plan_hierarchy:\n  - name: d\n    path: common.yaml\n",
    )
    yield _full(
        "layers-global-empty-hierarchy", global_cfg="version: 5\nhierarchy: []\n"
    )
    yield _full("layers-global-nohier", global_cfg="version: 5\n")
    yield _full("layers-global-empty-file", global_cfg="")
    yield _full("layers-global-comment-only", global_cfg="# nothing here\n")
    yield _full(
        "layers-global-versionless-v5ish",
        global_cfg="hierarchy:\n  - name: common\n    path: common.yaml\n",
    )


@scenario
def layers_module_rules(ctx):
    """Module-layer key rules: prefixes, lookup_options prefixes, odd names."""
    bad_mod_data = {
        "lopts-unprefixed-key": "mymod::k: [m]\nlookup_options:\n  other: {merge: unique}\n  mymod::k: {merge: unique}\n",
        "lopts-unprefixed-pattern": "mymod::k: [m]\nlookup_options:\n  '^other': {merge: unique}\n  mymod::k: {merge: unique}\n",
        "lopts-prefixed-pattern": "mymod::k: [m]\nlookup_options:\n  '^mymod::': {merge: unique}\n",
        "lopts-pattern-no-colons": "mymod::k: [m]\nlookup_options:\n  '^mymod': {merge: unique}\n",
        "lopts-pattern-any": "mymod::k: [m]\nlookup_options:\n  '^.*': {merge: unique}\n",
        "lopts-global-key": "mymod::k: [m]\nlookup_options:\n  g: {merge: unique}\n",
        "non-hash": "- a\n- b\n",
        "empty": "",
        "only-unprefixed": "k: v\nother: w\n",
        "nested-ok": "mymod::k: {unprefixed: fine}\n",
        "int-key": "1: x\nmymod::k: [m]\n",
        "prefix-case": "MYMOD::k: up\nmymod::k: [m]\n",
        "prefix-substring": "mymodx::k: leak\nmymod::k: [m]\n",
        "lookup-options-string": "mymod::k: [m]\nlookup_options: nope\n",
    }
    for name, data in bad_mod_data.items():
        s = Scn("layers-modrule-" + name, "layers-rules", "module data rule: " + name)
        s.hiera(ONE)
        s.file("data/common.yaml", "g: [g]\nmymod::k: [g]\nother: [g]\n")
        s.file("modules/mymod/hiera.yaml", ONE)
        s.file("modules/mymod/data/common.yaml", data)
        s.file("modules/mymodx/hiera.yaml", ONE)
        s.file("modules/mymodx/data/common.yaml", "mymodx::k: realx\n")
        for k in [
            "mymod::k",
            "g",
            "other",
            "k",
            "mymodx::k",
            "mymod::nope",
            "MYMOD::k",
            "1",
        ]:
            s.q(k)
        s.q("mymod::k", merge="unique")
        s.q("mymod::k", args=["--strict", "error"])
        s.q("mymod::k", args=["--strict", "off"])
        yield s

    # module directory names
    s = Scn("layers-modnames", "layers-rules", "module directory names and key shapes")
    s.hiera(ONE)
    s.file("data/common.yaml", "g: g\n")
    for mod in [
        "good",
        "with_under",
        "with-dash",
        "UPPER",
        "Mixed",
        "1num",
        "n1",
        "a.b",
        "_lead",
        "a",
    ]:
        s.file("modules/%s/hiera.yaml" % mod, ONE)
        s.file(
            "modules/%s/data/common.yaml" % mod,
            "%s::k: %s\n%s::k: lower-%s\n" % (mod, mod, mod.lower(), mod),
        )
        s.q(mod + "::k")
        s.q(mod.lower() + "::k")
        s.q("::" + mod + "::k")
        s.q(mod + "::nope")
    s.q("good::k.x")
    s.q("good::k", merge="deep")
    s.q("nomod::k")
    s.q("good")
    s.q("good::")
    s.q("::good")
    s.q("good::sub::k")
    yield s

    # two modulepath roots holding the same module; env modules dir missing
    s = Scn(
        "layers-env-names",
        "layers-rules",
        "environment selection and odd environment directories",
    )
    s.hiera(ONE)
    s.file("data/common.yaml", 'g: g\nwhich: global\nenv_interp: "%{environment}"\n')
    for env in ["production", "staging", "dev_1", "UPPER", "with-dash", "a.b"]:
        s.file("environments/%s/hiera.yaml" % env, ONE)
        s.file(
            "environments/%s/data/common.yaml" % env,
            "which_env: %s\ne_%s: 1\n"
            % (env, env.lower().replace("-", "_").replace(".", "_")),
        )
        s.q("which_env", args=["--environment", env])
        s.q("env_interp", args=["--environment", env])
        s.q("g", args=["--environment", env])
    s.q("which_env", args=["--environment", "nope"])
    s.q("g", args=["--environment", "nope"])
    s.q("which_env", args=["--environment", ""])
    s.q("which_env", args=["--environment", "../production"])
    s.q("which_env", args=["--environment", "production/"])
    s.q("which_env", args=["--environment", "pro duction"])
    s.q("which_env")
    yield s


@scenario
def v3_global(ctx):
    cfgs = {
        "basic": V3,
        "json-yaml": '---\n:backends: [json, yaml]\n:yaml:\n  :datadir: data\n:json:\n  :datadir: data\n:hierarchy:\n  - "roles/%{::role}"\n  - common\n',
        "yaml-json": '---\n:backends: [yaml, json]\n:yaml:\n  :datadir: data\n:json:\n  :datadir: data\n:hierarchy:\n  - "roles/%{::role}"\n  - common\n',
        "string-keys": "---\nbackends: yaml\nyaml:\n  datadir: data\nhierarchy: common\n",
        "mixed-keys": "---\n:backends: yaml\nyaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-3": "---\nversion: 3\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-sym-3": "---\n:version: 3\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-1": "---\n:version: 1\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-2": "---\nversion: 2\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "no-backends": '---\n:yaml:\n  :datadir: data\n:hierarchy:\n  - "roles/%{::role}"\n  - common\n',
        "no-hierarchy": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n",
        "no-datadir": "---\n:backends: yaml\n:hierarchy:\n  - common\n",
        "hierarchy-string": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy: common\n",
        "merge-native": '---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - "roles/%{::role}"\n  - common\n:merge_behavior: native\n',
        "merge-deeper": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - \"roles/%{::role}\"\n  - common\n:merge_behavior: deeper\n:deep_merge_options:\n  :knockout_prefix: '--'\n",
        "merge-deep": '---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - "roles/%{::role}"\n  - common\n:merge_behavior: deep\n',
        "merge-bogus": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n:merge_behavior: bogus\n",
        "logger": "---\n:backends: yaml\n:logger: console\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "logger-bogus": "---\n:backends: yaml\n:logger: bogus\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "extension": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n  :extension: yml\n:hierarchy:\n  - common\n",
        "datadir-interp": '---\n:backends: yaml\n:yaml:\n  :datadir: "data-%{::role}"\n:hierarchy:\n  - common\n',
        "datadir-abs-missing": "---\n:backends: yaml\n:yaml:\n  :datadir: /nonexistent/hf\n:hierarchy:\n  - common\n",
        "hierarchy-interp-forms": '---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - "roles/%{role}"\n  - "roles/%{facts.role}"\n  - "os/%{facts.os.family}"\n  - "os/%{::os.family}"\n  - "%{nope}"\n  - "x/%{::nope}/y"\n  - common\n',
        "hierarchy-fn": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - \"%{hiera('levelname')}\"\n  - common\n",
        "hierarchy-literal": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - \"%{literal('common')}\"\n",
        "hierarchy-dup": '---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n  - common\n  - "roles/%{::role}"\n',
        "hierarchy-empty": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy: []\n",
        "hierarchy-nonstring": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - 1\n  - common\n",
        "backends-empty": "---\n:backends: []\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "backends-dup": "---\n:backends: [yaml, yaml]\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "backends-unknown": "---\n:backends: [yaml, bogus]\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "backends-hocon": "---\n:backends: [hocon, yaml]\n:yaml:\n  :datadir: data\n:hocon:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "backend-opts-missing": "---\n:backends: [json]\n:hierarchy:\n  - common\n",
        "unknown-key": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n:bogus: 1\n",
        "defaults-key": "---\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\ndefaults:\n  datadir: x\n",
        "v5-keys-mixed": "---\n:backends: yaml\n:yaml:\n  :datadir: data\nhierarchy:\n  - name: c\n    path: common.yaml\n",
        "empty-file": "",
        "list": "- a\n",
        "version-string-3": "---\nversion: '3'\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-float": "---\nversion: 3.0\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-5-string": "---\nversion: '5'\nhierarchy:\n  - name: c\n    path: common.yaml\n",
        "version-5-float": "---\nversion: 5.0\nhierarchy:\n  - name: c\n    path: common.yaml\n",
        "version-0": "---\nversion: 0\n",
        "version-6": "---\nversion: 6\n",
        "version-null": "---\nversion: ~\n:backends: yaml\n:yaml:\n  :datadir: data\n:hierarchy:\n  - common\n",
        "version-true": "---\nversion: true\n",
        "eyaml-backend": "---\n:backends: [eyaml]\n:eyaml:\n  :datadir: data\n  :extension: yaml\n:hierarchy:\n  - common\n",
    }
    for name, cfg in cfgs.items():
        s = Scn("v3-" + name, "v3", "version 3 global hiera.yaml: " + name)
        s.hiera(cfg)
        s.file(
            "data/common.yaml",
            'k: c_yaml\nh: {c: 1, s: c}\nl: [c, x]\nonly_c: 1\nref: "%{hiera(\'k\')}"\nref2: "%{::role}"\nlevelname: common\nlookup_options:\n  l: {merge: unique}\n',
        )
        s.file(
            "data/common.json",
            '{"k": "c_json", "h": {"cj": 1, "s": "cj"}, "l": ["cj", "x"], "only_cj": 1}\n',
        )
        s.file("data/common.yml", "k: c_yml\nonly_yml: 1\n")
        s.file("data/common.conf", "k = c_hocon\nonly_hocon = 1\n")
        s.file(
            "data/roles/web.yaml",
            "k: r_yaml\nh: {r: 1, s: r, l: ['--x']}\nl: [r, x]\nonly_r: 1\n",
        )
        s.file("data/roles/web.json", '{"k": "r_json", "h": {"rj": 1}, "l": ["rj"]}\n')
        s.file("data/os/RedHat.yaml", "k: os_yaml\nonly_os: 1\n")
        s.file("data-web/common.yaml", "k: dataweb\n")
        s.file("hieradata/common.yaml", "k: hieradata\n")
        s.file("environments/production/hieradata/common.yaml", "k: env_hieradata\n")
        for k in [
            "k",
            "h",
            "l",
            "only_c",
            "only_cj",
            "only_r",
            "only_yml",
            "only_hocon",
            "only_os",
            "ref",
            "nope",
        ]:
            s.q(k)
        s.q("k", merge="unique")
        s.q("h", merge="hash")
        s.q("h", merge={"strategy": "deep", "knockout_prefix": "--"})
        s.q("l", merge="deep")
        s.q("k", args=["--strict", "error"])
        yield s
