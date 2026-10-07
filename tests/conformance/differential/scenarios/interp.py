"""Interpolation scenarios: variables, functions, nesting, strict modes."""

from __future__ import annotations

from ..scenario import Scn, V5, d, scenario

THREE = V5 + """\
  - name: node
    path: "nodes/%{facts.a}.yaml"
  - name: os
    path: "os/%{facts.os.family}.yaml"
  - name: common
    path: common.yaml
"""

TARGETS = """\
target: tval
int_target: 7
float_target: 2.5
bool_target: false
nil_target: ~
arr_target: [1, two, [3]]
hash_target:
  a:
    b: deep
  list: [l0, l1]
  "dotted.key": dk
empty_str: ""
empty_arr: []
empty_hash: {}
"""


def _add(s, entries, strict=("warning",)):
    """entries: list of (key, yaml-value-text). Adds one query per key per
    strict mode."""
    import yaml

    body = ""
    for k, v in entries:
        line = "{}: {}\n".format(k, v)
        try:
            yaml.safe_load(line)
        except Exception as e:  # a typo in this generator, not a test input
            raise SystemExit("bad generator entry {!r}: {}".format(line, e))
        body += line
    for k, _ in entries:
        for st in strict:
            s.q(k, args=["--strict", st] if st != "warning" else None)
    return body


@scenario
def interp_vars(ctx):
    s = Scn("interp-vars", "interp", "variable references in values")
    s.hiera(THREE)
    ents = [
        ("v_plain", '"x%{facts.a}y"'),
        ("v_top", '"%{::a}"'),
        ("v_bare", '"%{a}"'),
        ("v_nested", '"%{facts.os.family}"'),
        ("v_nested3", '"%{facts.os.release.major}"'),
        ("v_arr_idx", '"%{facts.arr.1}"'),
        ("v_arr_idx_hash", '"%{facts.arr.2.k}"'),
        ("v_arr_oob", '"%{facts.arr.9}"'),
        ("v_arr_neg", '"%{facts.arr.-1}"'),
        ("v_arr_str_idx", '"%{facts.arr.x}"'),
        ("v_hash_whole", '"%{facts.os}"'),
        ("v_hash_whole2", '"%{facts.hsh}"'),
        ("v_arr_whole", '"%{facts.arr}"'),
        ("v_int", '"%{facts.n}"'),
        ("v_bool", '"%{facts.b}"'),
        ("v_float", '"%{facts.f}"'),
        ("v_empty", '"[%{facts.empty}]"'),
        ("v_missing", '"[%{facts.nope}]"'),
        ("v_missing_top", '"[%{nope}]"'),
        ("v_missing_topscope", '"[%{::nope}]"'),
        ("v_missing_deep", '"[%{facts.os.nope.deeper}]"'),
        ("v_into_string", '"[%{facts.a.x}]"'),
        ("v_into_int", '"[%{facts.n.x}]"'),
        ("v_topscope_facts", '"%{::facts.a}"'),
        ("v_os_top", '"%{os.family}"'),
        ("v_os_topscope", '"%{::os.family}"'),
        ("v_quoted_seg", "\"%{facts.'a'}\""),
        ("v_dquoted_seg", "'%{facts.\"a\"}'"),
        ("v_environment", '"%{environment}"'),
        ("v_environment2", '"%{::environment}"'),
        ("v_server_facts", '"%{server_facts.serverversion}"'),
        ("v_trusted", '"[%{trusted.certname}]"'),
        ("v_trusted_auth", '"[%{trusted.authenticated}]"'),
        ("v_trusted_ext", '"[%{trusted.extensions}]"'),
        ("v_trusted_whole", '"[%{trusted}]"'),
        ("v_facts_whole_missing", '"[%{factz}]"'),
        ("v_spaces", '"%{ facts.a }"'),
        ("v_spaces2", '"%{facts.a }"'),
        ("v_spaces_in", '"%{facts. a}"'),
        ("v_empty_expr", '"[%{}]"'),
        ("v_empty_topscope", '"[%{::}]"'),
        ("v_quoted_empty", "\"[%{''}]\""),
        ("v_two", '"%{facts.a}-%{facts.role}"'),
        ("v_adjacent", '"%{facts.a}%{facts.role}"'),
        ("v_unclosed", '"x%{facts.a"'),
        ("v_pct", '"100%"'),
        ("v_pct_brace", '"%{"'),
        ("v_double_pct", '"%%{facts.a}"'),
        ("v_nested_braces", '"%{facts.%{facts.role}}"'),
        ("v_closing_only", '"a}b"'),
        ("v_dollar", '"${facts.a}"'),
        ("v_case", '"%{Facts.a}"'),
        ("v_upper", '"%{facts.A}"'),
        ("v_weird", '"%{facts.weird}"'),
        ("v_numeric_var", '"[%{0}]"'),
        ("v_name_title", '"[%{name}|%{title}|%{module_name}]"'),
        ("v_dot_lead", '"[%{.a}]"'),
        ("v_dot_trail", '"[%{facts.a.}]"'),
        ("v_dot_double", '"[%{facts..a}]"'),
        ("v_colon_mid", '"[%{facts::a}]"'),
        ("v_ns_var", '"[%{foo::bar}]"'),
        ("v_ns_var_top", '"[%{::foo::bar}]"'),
        ("v_hsh_key_num", '"[%{facts.hsh.one}]"'),
    ]
    body = _add(s, ents)
    s.file("data/common.yaml", TARGETS + body)
    yield s

    # the same variable set under each strict mode, undefined ones only
    s = Scn("interp-vars-strict", "interp", "undefined variables under each --strict")
    s.hiera(THREE)
    ents = [
        ("u_missing", '"[%{facts.nope}]"'),
        ("u_missing_top", '"[%{nope}]"'),
        ("u_missing_topscope", '"[%{::nope}]"'),
        ("u_missing_deep", '"[%{facts.os.nope.deeper}]"'),
        ("u_ns", '"[%{foo::bar}]"'),
        ("u_scope_fn", "\"[%{scope('nope')}]\""),
        ("u_scope_fn_deep", "\"[%{scope('facts.nope')}]\""),
        ("u_in_hash", '{k: "[%{nope}]", ok: 1}'),
        ("u_in_arr", '["[%{nope}]", ok]'),
        ("u_in_key", '{"%{nope}": v}'),
        ("u_defined", '"[%{facts.a}]"'),
        ("u_empty_fact", '"[%{facts.empty}]"'),
        ("u_trusted_missing", '"[%{trusted.nope}]"'),
        ("u_arr_oob", '"[%{facts.arr.9}]"'),
        ("u_lookup_missing", "\"[%{lookup('nope')}]\""),
        ("u_alias_missing", "\"%{alias('nope')}\""),
        ("u_empty_expr", '"[%{}]"'),
    ]
    body = _add(s, ents, strict=("off", "warning", "error"))
    s.file("data/common.yaml", TARGETS + body)
    # a key in a file that also holds an undefined reference elsewhere
    s.q("target", args=["--strict", "error"])
    yield s


@scenario
def interp_functions(ctx):
    s = Scn("interp-fns", "interp", "lookup/hiera/alias/scope/literal")
    s.hiera(THREE)
    ents = [
        ("f_lookup", "\"%{lookup('target')}\""),
        ("f_hiera", "\"%{hiera('target')}\""),
        ("f_alias", "\"%{alias('hash_target')}\""),
        ("f_alias_arr", "\"%{alias('arr_target')}\""),
        ("f_alias_int", "\"%{alias('int_target')}\""),
        ("f_alias_bool", "\"%{alias('bool_target')}\""),
        ("f_alias_nil", "\"%{alias('nil_target')}\""),
        ("f_alias_str", "\"%{alias('target')}\""),
        ("f_alias_embedded", "\"x%{alias('hash_target')}\""),
        ("f_alias_embedded2", "\"%{alias('hash_target')}x\""),
        ("f_alias_twice", "\"%{alias('target')}%{alias('target')}\""),
        ("f_alias_dotted", "\"%{alias('hash_target.a')}\""),
        ("f_alias_dotted_list", "\"%{alias('hash_target.list.1')}\""),
        ("f_alias_missing", "\"%{alias('nope')}\""),
        ("f_alias_spaces", "\"%{ alias('hash_target') }\""),
        ("f_alias_ws_inside", "\"%{alias( 'hash_target' )}\""),
        ("f_scope", "\"%{scope('a')}\""),
        ("f_scope_facts", "\"%{scope('facts.os.family')}\""),
        ("f_scope_top", "\"%{scope('::a')}\""),
        ("f_scope_hash", "\"%{scope('facts.os')}\""),
        ("f_literal", "\"%{literal('%')}{facts.a}\""),
        ("f_literal_str", "\"%{literal('hello')}\""),
        ("f_literal_empty", "\"[%{literal('')}]\""),
        ("f_lookup_dq", "'%{lookup(\"target\")}'"),
        ("f_lookup_noquote", '"[%{lookup(target)}]"'),
        ("f_lookup_spaces", "\"%{ lookup('target') }\""),
        ("f_lookup_inner_ws", "\"%{lookup( 'target' )}\""),
        ("f_lookup_fn_ws", "\"%{lookup ('target')}\""),
        ("f_lookup_int", "\"%{lookup('int_target')}\""),
        ("f_lookup_float", "\"%{lookup('float_target')}\""),
        ("f_lookup_bool", "\"%{lookup('bool_target')}\""),
        ("f_lookup_nil", "\"[%{lookup('nil_target')}]\""),
        ("f_lookup_arr", "\"%{lookup('arr_target')}\""),
        ("f_lookup_hash", "\"%{lookup('hash_target')}\""),
        ("f_lookup_hash_embedded", "\"x%{lookup('hash_target')}y\""),
        ("f_lookup_empty_hash", "\"[%{lookup('empty_hash')}]\""),
        ("f_lookup_empty_arr", "\"[%{lookup('empty_arr')}]\""),
        ("f_lookup_empty_str", "\"[%{lookup('empty_str')}]\""),
        ("f_lookup_dotted", "\"%{lookup('hash_target.a.b')}\""),
        ("f_lookup_dotted_idx", "\"%{lookup('hash_target.list.0')}\""),
        ("f_lookup_dotted_quoted", "'%{lookup(''hash_target.\"dotted.key\"'')}'"),
        ("f_lookup_dotted_missing", "\"[%{lookup('hash_target.nope')}]\""),
        ("f_lookup_dotted_into_str", "\"[%{lookup('target.x')}]\""),
        ("f_lookup_missing", "\"[%{lookup('nope')}]\""),
        ("f_hiera_missing", "\"[%{hiera('nope')}]\""),
        ("f_lookup_self", "\"%{lookup('f_lookup_self')}\""),
        ("f_cycle_a", "\"%{lookup('f_cycle_b')}\""),
        ("f_cycle_b", "\"%{lookup('f_cycle_a')}\""),
        ("f_alias_self", "\"%{alias('f_alias_self')}\""),
        ("f_chain1", "\"%{lookup('f_chain2')}\""),
        ("f_chain2", "\"<%{lookup('f_chain3')}>\""),
        ("f_chain3", '"%{facts.a}"'),
        ("f_alias_chain", "\"%{alias('f_alias')}\""),
        ("f_unknown", "\"%{foo('x')}\""),
        ("f_unknown_noarg", '"[%{foo()}]"'),
        ("f_lookup_noarg", '"[%{lookup()}]"'),
        ("f_lookup_emptyarg", "\"[%{lookup('')}]\""),
        ("f_lookup_two", "\"%{lookup('target')}/%{lookup('int_target')}\""),
        ("f_lookup_nested", "\"%{lookup('%{facts.role}_key')}\""),
        ("f_lookup_mismatch_quote", "'[%{lookup(''target\")}]'"),
        ("f_lookup_upper", "\"[%{LOOKUP('target')}]\""),
        ("f_lookup_other_level", "\"%{lookup('os_only')}\""),
        ("f_lookup_overridden", "\"%{lookup('layered')}\""),
        ("f_lookup_ns", "\"%{lookup('ns::key')}\""),
        ("f_lookup_ns_top", "\"[%{lookup('::ns::key')}]\""),
        ("f_scope_missing", "\"[%{scope('nope')}]\""),
        ("f_scope_noquote", '"[%{scope(a)}]"'),
        ("f_literal_noquote", '"[%{literal(%)}]"'),
        ("f_var_parens", '"[%{facts.a()}]"'),
        (
            "f_in_hash",
            "{a: \"%{lookup('target')}\", b: {c: \"%{alias('arr_target')}\"}}",
        ),
        (
            "f_in_arr",
            '["%{lookup(\'target\')}", "%{alias(\'hash_target\')}", ["%{facts.a}"]]',
        ),
        ("f_in_key", '{"%{lookup(\'target\')}": v, "k_%{facts.a}": w}'),
        ("f_in_key_alias", "{\"%{alias('target')}\": v}"),
        ("f_in_key_alias_hash", "{\"%{alias('hash_target')}\": v}"),
        ("f_key_collision", '{"%{facts.a}": 1, "alpha": 2}'),
        ("f_lookup_with_merge", "\"%{lookup('merged_arr')}\""),
        ("f_alias_with_merge", "\"%{alias('merged_arr')}\""),
        ("f_alias_with_convert", "\"%{alias('conv')}\""),
        ("f_lookup_lookup_options", "\"%{alias('lookup_options')}\""),
        ("f_lookup_dotted_alias", "\"%{alias('f_in_hash.b.c')}\""),
        ("f_hiera_dotted", "\"%{hiera('hash_target.a.b')}\""),
        ("f_lookup_interp_target", "\"%{lookup('v_ref')}\""),
        ("v_ref", '"%{facts.os.family}"'),
        ("ns::key", "nsval"),
        ("role_key", "notused"),
        ("web_key", "webval"),
    ]
    body = _add(s, ents)
    s.file(
        "data/common.yaml",
        TARGETS + body + d("""\
            layered: from_common
            merged_arr: [c1, c2]
            conv: "12"
            lookup_options:
              merged_arr: {merge: unique}
              conv: {convert_to: Integer}
            """),
    )
    s.file(
        "data/os/RedHat.yaml", "os_only: rh\nlayered: from_os\nmerged_arr: [o1, c1]\n"
    )
    s.file("data/nodes/alpha.yaml", "layered: from_node\nmerged_arr: [n1]\n")
    s.qs(["merged_arr", "conv", "layered", "lookup_options"])
    s.q("merged_arr", merge="first")
    yield s


@scenario
def interp_cross_level(ctx):
    """Higher-priority levels referencing lower ones and the reverse; merges
    over interpolated values; interpolation resolved before the merge."""
    s = Scn("interp-cross", "interp", "interpolation across levels + merges")
    s.hiera(THREE)
    s.file(
        "data/nodes/alpha.yaml",
        d("""\
            h:
              from_node: "%{lookup('c_only')}"
              shared: "node-%{facts.a}"
              "%{facts.role}": node_key
            a: ["%{lookup('c_only')}", n]
            ref_down: "%{lookup('c_only')}"
            alias_down: "%{alias('c_hash')}"
            mixed: "%{alias('c_hash')}"
            ko: ["--x", "%{facts.a}"]
            """),
    )
    s.file(
        "data/os/RedHat.yaml",
        d("""\
            h:
              from_os: "%{lookup('n_only')}"
              shared: os
              web: os_key
            a: ["%{facts.os.family}", n, x]
            n_only_ref: "%{lookup('ref_down')}"
            mixed: [1, 2]
            ko: [x, y, alpha]
            """),
    )
    s.file(
        "data/common.yaml",
        d("""\
            c_only: cval
            n_only: from-common-but-shadowed
            c_hash: {x: 1, y: "%{facts.a}"}
            h:
              from_common: "%{alias('c_hash')}"
              shared: common
            a: [cval, "%{alias('c_hash')}"]
            ref_up: "%{lookup('ref_down')}"
            mixed: {k: v}
            ko: [z]
            """),
    )
    for k in ["h", "a", "mixed", "ko"]:
        for m in [
            None,
            "first",
            "unique",
            "hash",
            "deep",
            {"strategy": "deep", "knockout_prefix": "--"},
            {"strategy": "deep", "merge_hash_arrays": True},
            {"strategy": "deep", "sort_merged_arrays": True},
        ]:
            s.q(k, merge=m)
    s.qs(
        [
            "ref_down",
            "alias_down",
            "n_only_ref",
            "ref_up",
            "h.from_common.y",
            "h.web",
            "a.0",
            "a.1",
        ]
    )
    s.q("h.shared", merge="deep")
    s.q("h.from_common.y", merge="deep")
    s.q("a.3", merge="unique")
    yield s


@scenario
def interp_in_config(ctx):
    """Interpolation in hiera.yaml itself: datadir, paths, options, function
    syntax (not allowed in a location)."""
    variants = {
        "datadir": 'defaults:\n  datadir: "data-%{facts.role}"\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: common.yaml\n',
        "datadir-missing": 'defaults:\n  datadir: "data-%{facts.nope}"\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: common.yaml\n',
        "level-datadir": 'defaults:\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    datadir: "data-%{facts.role}"\n    path: common.yaml\n  - name: d\n    path: common.yaml\n',
        "path-fn-lookup": "defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: \"%{lookup('k')}.yaml\"\n  - name: d\n    path: common.yaml\n",
        "path-fn-literal": "defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: \"%{literal('common')}.yaml\"\n",
        "path-fn-scope": "defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: \"%{scope('facts.role')}.yaml\"\n  - name: d\n    path: common.yaml\n",
        "path-fn-alias": "defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: \"%{alias('k')}.yaml\"\n  - name: d\n    path: common.yaml\n",
        "path-hash-fact": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: "%{facts.os}.yaml"\n  - name: d\n    path: common.yaml\n',
        "path-arr-fact": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: "%{facts.arr}.yaml"\n  - name: d\n    path: common.yaml\n',
        "path-int-bool": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    paths: ["n%{facts.n}.yaml", "b%{facts.b}.yaml", "f%{facts.f}.yaml"]\n  - name: d\n    path: common.yaml\n',
        "path-empty-fact": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    paths: ["%{facts.empty}.yaml", "%{facts.nope}.yaml", "x/%{facts.empty}/web.yaml", "%{facts.empty}"]\n  - name: d\n    path: common.yaml\n',
        "path-slash-fact": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    paths: ["%{facts.slash}.yaml", "%{facts.dotdot}/outside.yaml", "%{facts.weird}.yaml"]\n  - name: d\n    path: common.yaml\n',
        "name-interp": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: "lvl %{facts.role}"\n    path: "%{facts.role}.yaml"\n  - name: d\n    path: common.yaml\n',
        "path-trusted-env": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    paths: ["%{trusted.certname}.yaml", "%{environment}.yaml", "%{::environment}.yaml", "%{server_facts.serverversion}.yaml"]\n  - name: d\n    path: common.yaml\n',
        "path-topscope": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    paths: ["%{::role}.yaml", "%{role}.yaml", "%{::facts.role}.yaml", "%{::os.family}.yaml"]\n  - name: d\n    path: common.yaml\n',
        "path-abs": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: "/nonexistent/%{facts.role}.yaml"\n  - name: d\n    path: common.yaml\n',
        "path-dotdot": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: "../outside.yaml"\n  - name: d\n    path: common.yaml\n',
        "datadir-abs-missing": "defaults:\n  datadir: /nonexistent/hf\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: common.yaml\n",
        "datadir-dotdot": "defaults:\n  datadir: data/../data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: ./common.yaml\n",
        "path-is-dir": "defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: adir\n  - name: d\n    path: common.yaml\n",
        "path-no-ext": "defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: noext\n  - name: d\n    path: common.yaml\n",
        "options-interp": 'defaults:\n  datadir: data\n  data_hash: yaml_data\nhierarchy:\n  - name: c\n    path: common.yaml\n    options:\n      x: "%{facts.role}"\n',
    }
    for name, body in variants.items():
        s = Scn(
            "interp-cfg-" + name, "config-interp", "hiera.yaml interpolation: " + name
        )
        s.hiera("version: 5\n" + body)
        s.file("data/common.yaml", "k: common\nonly_common: 1\n")
        s.file("data/web.yaml", "k: web\n")
        s.file("data-web/common.yaml", "k: data-web\n")
        s.file("data-/common.yaml", "k: data-empty\n")
        s.file("data/n42.yaml", "k: n42\nn: 1\n")
        s.file("data/btrue.yaml", "k: btrue\nb: 1\n")
        s.file("data/f1.5.yaml", "k: f1.5\nf: 1\n")
        s.file("data/.yaml", "k: dotyaml\ndot: 1\n")
        s.file("data/x/web.yaml", "k: xweb\nxw: 1\n")
        s.file("data/x//placeholder", "")
        s.file("data/sub/inner.yaml", "k: subinner\nsi: 1\n")
        s.file("outside.yaml", "k: outside\nout: 1\n")
        s.file("data/a b/c.yaml", "k: weird\nw: 1\n")
        s.file("data/production.yaml", "k: production\nprod: 1\n")
        s.file("data/8.10.0.yaml", "k: sv\nsv: 1\n")
        s.file("data/RedHat.yaml", "k: redhat\nrh: 1\n")
        s.file("data/adir/placeholder", "")
        s.file("data/noext", "k: noext\nne: 1\n")
        s.file("data/lvl web.yaml", "k: lvlweb\n")
        s.qs(
            [
                "k",
                "only_common",
                "n",
                "b",
                "f",
                "dot",
                "xw",
                "si",
                "out",
                "w",
                "prod",
                "sv",
                "rh",
                "ne",
            ]
        )
        s.q("k", merge="unique")
        s.q("k", args=["--strict", "error"])
        yield s
