"""HOCON, type and interpolation constructs, one construct per file, so a file one
side rejects on a single entry does not hide the rest."""

from __future__ import annotations

from ..scenario import Scn, d, scenario

HOCON_ONE = {
    # one construct per file: a file with several constructs is rejected as a whole
    # on a single bad entry
    "num-leading-zero": "k = 010\n",
    "num-hex": "k = 0x10\n",
    "num-exp": "k = 1e3\n",
    "num-exp-upper": "k = 1E3\n",
    "num-float-int": "k = 1.0\n",
    "num-neg": "k = -1\n",
    "num-plus": "k = +1\n",
    "num-dot-lead": "k = .5\n",
    "num-dot-trail": "k = 5.\n",
    "num-big": "k = 123456789012345678901234567890\n",
    "num-underscore": "k = 1_000\n",
    "num-small-exp": "k = 1.5e-7\n",
    "num-big-float": "k = 100000000000000000000.0\n",
    "num-neg-zero": "k = -0.0\n",
    "num-double-zero": "k = 00\n",
    "num-trailing-zero": "k = 0.10\n",
    "num-in-string-concat": "k = 1 2\n",
    "num-then-unit": "k = 10px\n",
    "num-dotted": "k = 1.2.3\n",
    "num-ip": "k = 10.0.0.1\n",
    "esc-basic": 'k = "a\\nb\\tc"\n',
    "esc-unicode": 'k = "\\u00e9"\n',
    "esc-backslash": 'k = "a\\\\b"\n',
    "esc-slash": 'k = "a\\/b"\n',
    "esc-quote": 'k = "a\\"b"\n',
    "esc-bad": 'k = "\\q"\n',
    "bool-true": "k = true\n",
    "bool-capitalized": "k = True\n",
    "bool-upper": "k = TRUE\n",
    "bool-yes": "k = yes\n",
    "bool-on": "k = on\n",
    "null-capitalized": "k = Null\n",
    "null-upper": "k = NULL\n",
    "concat-bools": "k = true false\n",
    "concat-bool-str": "k = true x\n",
    "concat-null-str": "k = null x\n",
    "concat-num-str": "k = 5 apples\n",
    "concat-quoted": 'k = "a" "b"\n',
    "concat-spaces": "k = a   b\n",
    "plus-equals-existing": "k = [1]\nk += 2\n",
    "plus-equals-new": "k += 1\n",
    "plus-equals-twice": "k += 1\nk += 2\n",
    "dup-obj-then-scalar": "k { a = 1 }\nk = 5\n",
    "dup-scalar-then-obj": "k = 5\nk { a = 1 }\n",
    "dup-obj-merge": "k { a = 1 }\nk { b = 2 }\n",
    "quoted-key-dot": '"q.r" = 4\nk = ${"q.r"}\n',
    "quoted-key-space": '"a b" = 1\nk = 1\n',
    "quoted-key-empty": '"" = 2\nk = 1\n',
    "unquoted-key-space": "a b = 3\nk = 1\n",
    "quoted-path": '"x"."y" = 4\nk = 1\n',
    "key-with-colon-ns": "mod::k = 1\nk = 1\n",
    "key-quoted-ns": '"mod::k" = 1\nk = 1\n',
    "bom": b"\xef\xbb\xbfk = v\n",
    "optional-subst-concat": "k = pre ${?nope} post\n",
    "optional-subst-alone": "k = ${?nope}\nj = 1\n",
    "subst-in-array": "x = 1\nk = [${x}, 2]\n",
    "subst-obj-merge": "base { a = 1 }\nk = ${base} { b = 2 }\n",
    "triple-quote": 'k = """a\n"b"\nc"""\n',
    "unquoted-url": "k = http://example.com/a?b=c\n",
    "unquoted-path": "k = /etc/app/conf.d\n",
    "unquoted-dash": "k = a-b-c\n",
    "unquoted-dots": "k = foo.bar.baz\n",
    "unquoted-email": "k = user@example.com\n",
    "trailing-comma-arr": "k = [1, 2,]\n",
    "double-comma-arr": "k = [1,, 2]\n",
    "leading-comma-arr": "k = [, 1]\n",
    "newline-sep-arr": "k = [1\n2]\n",
    "arr-concat": "k = [1] [2]\n",
    "obj-concat": "k = { a = 1 } { b = 2 }\n",
    "nested-path-then-obj": "k.a = 1\nk { b = 2 }\n",
    "duration": "k = 10 seconds\n",
    "duration-short": "k = 5s\n",
    "duration-days": "k = 2 days\n",
    "duration-ms": "k = 10ms\n",
    "size": "k = 512K\n",
    "percent": "k = 100%\n",
    "include-key-like": "includes = 1\nk = 1\n",
    "comment-hash-in-unquoted": "k = a#b\n",
    "comment-slashes-in-unquoted": "k = a//b\n",
    "empty-string": 'k = ""\n',
    "whitespace-value": 'k = "  "\n',
    "multiline-obj-commas": "k {\n a = 1,\n b = 2\n}\n",
    "json-top": '{"k": {"a": [1, 2.5, null, true]}}\n',
}


@scenario
def hocon_one(ctx):
    for name, content in HOCON_ONE.items():
        s = Scn("hocon1-" + name, "hocon", "hocon_data, one construct: " + name)
        s.hiera(
            "version: 5\ndefaults:\n  datadir: data\n  data_hash: hocon_data\nhierarchy:\n  - name: subject\n    path: subject.conf\n  - name: fallback\n    path: fallback.conf\n"
        )
        s.file("data/subject.conf", content)
        s.file("data/fallback.conf", "fb = fallback\n")
        s.q("k")
        s.q("fb")
        for extra in {
            "quoted-key-dot": ["q.r", "'q.r'", "q"],
            "quoted-key-space": ["a b"],
            "quoted-key-empty": [""],
            "unquoted-key-space": ["a b", "a"],
            "quoted-path": ["x", "x.y"],
            "key-with-colon-ns": ["mod::k"],
            "key-quoted-ns": ["mod::k"],
            "optional-subst-alone": ["j"],
            "include-key-like": ["includes"],
            "nested-path-then-obj": ["k.a", "k.b"],
        }.get(name, []):
            s.q(extra)
        yield s


@scenario
def extra_types(ctx):
    s = Scn(
        "extra-types", "types", "value_type neighbours of the first-pass differences"
    )
    s.simple()
    s.file(
        "data/common.yaml",
        d("""\
            str: hello
            int: 42
            arr: [1, 2, 3]
            arr_s: [a, b]
            arr_e: []
            hash: {a: 1, b: two}
            hash_e: {}
            svc: {name: web}
            svc_full: {name: web, port: 80}
            svc_nil: {name: web, port: ~}
            nil: ~
            """),
    )
    for k, ts in {
        "arr": [
            "Tuple",
            "Tuple[Integer]",
            "Tuple[Integer, 3]",
            "Tuple[Integer, 1, default]",
            "Tuple[Integer, default, 5]",
            "Tuple[Integer, Integer, 2, 5]",
            "Tuple[Integer, 4, 5]",
            "Tuple[Any]",
            "Tuple[Any, 0]",
            "Array[Integer, default]",
            "Array[Integer, default, default]",
            "Array[Integer, 0x1]",
            "Array[Integer, 010]",
            "Collection",
            "Collection[3, 3]",
            "Collection[default, 2]",
            "Iterable",
            "Iterable[Integer]",
            "Iterator",
            "Variant[Array, Hash]",
            "Optional[Tuple]",
            "Array[Integer[0x0, 0xF]]",
            "Array[Numeric]",
            "Array[Scalar, 3]",
            "NotUndef[Array]",
            "Array[Integer][1]",
            "Array[Integer,]",
            "Array[ Integer ]",
            "Array[\nInteger\n]",
            "Array[Integer] ",
            "Variant[Array[String], Array[Integer]]",
            "Array[Variant[String, Undef]]",
        ],
        "arr_e": [
            "Tuple",
            "Tuple[Integer]",
            "Tuple[Integer, 0]",
            "Array[String]",
            "Array[String, 1]",
            "Hash",
            "Array[0, 0]",
            "Tuple[0, 0]",
            "Collection[0, 0]",
        ],
        "str": [
            "Pattern",
            "Enum",
            "Pattern[/(?i)HELLO/]",
            "Pattern[/^[[:alpha:]]+$/]",
            "Pattern[/\\Ahello\\z/]",
            "Pattern[/h.l{2}o/]",
            "Pattern['hel+o']",
            "Pattern[hello]",
            "Pattern[/(?<n>h)ello/]",
            "Pattern[/\\p{Alpha}+/]",
            "Pattern[/HELLO/i]",
            "Pattern[/^$/]",
            "Pattern[//]",
            "Pattern[/hel\\/lo/]",
            "Pattern[/\\h+/]",
            "String[5, 5]",
            "String[5]",
            "String[default, 5]",
            "String[6]",
            "String[0x5]",
            "String[5, default]",
            "Enum[hello, 'world']",
            "Enum['Hello', true]",
            "Enum[hello, true]",
            "Enum[HELLO, true]",
            "Variant[Enum[a], Pattern[/^h/]]",
            "Optional[Pattern[/x/]]",
            "ScalarData",
            "Scalar",
            "Data",
            "Variant[Undef, String]",
            "String[1, 3]",
            "Regexp",
            "Stringdata",
            "Optional[Enum[hello]]",
            "Variant[Integer[1, 2], String[1, 2]]",
        ],
        "int": [
            "Integer[0x10, 0x30]",
            "Integer[010, 100]",
            "Integer[42]",
            "Integer[-0x10]",
            "Integer[1e1, 50]",
            "Integer[1.0, 50]",
            "Integer[40.5, 50]",
            "Float[42]",
            "Numeric",
            "Variant[Float, Integer[43]]",
            "Integer[default, 42]",
            "Integer[42, default]",
            "Enum[42]",
            "Integer[+1]",
            "Integer[ 1 , 50 ]",
            "Integer[1, 50, 3]",
            "Timestamp",
            "Timespan",
            "Integer[0b1]",
            "Integer['42']",
        ],
        "hash": [
            "Struct[{a => Integer, b => String}]",
            "Struct[{a => Integer, b => String, c => Optional[String]}]",
            "Struct[{a => Integer, b => String, c => Undef}]",
            "Struct[{a => Integer, b => String, c => Any}]",
            "Struct[{a => Integer, b => String, c => Variant[Undef, String]}]",
            "Struct[{a => Integer, b => String, c => Data}]",
            "Struct[{a => Integer, b => String, NotUndef[c] => Optional[String]}]",
            "Struct[{a => Integer, b => String, Optional[c] => NotUndef}]",
            "Struct[{Optional[a] => Integer, b => String}]",
            "Struct[{'a' => Integer, \"b\" => String}]",
            "Struct[{a => Integer, b => String,}]",
            "Struct[{a=>Integer,b=>String}]",
            "Struct[{a => Integer, a => Integer, b => String}]",
            "Hash[String, Variant[Integer, String]]",
            "Hash[String, Any, 2, 2]",
            "Hash[String, Any, default, 1]",
            "Hash[2, 2]",
            "Hash[Any, Any, 0x2]",
            "Hash[String[1, 1], Scalar]",
            "Variant[Struct[{a => String}], Hash]",
            "Hash[Pattern[/a|b/], Any]",
            "Struct",
            "Hash[String, ScalarData]",
            "Hash[Scalar, Data]",
        ],
        "hash_e": [
            "Struct[{a => Optional[Integer]}]",
            "Struct[{Optional[a] => Integer}]",
            "Struct[{a => Any}]",
            "Struct[{a => Integer}]",
            "Hash[String, Integer, 1]",
            "Struct",
            "Struct[{}]",
            "Hash[0, 0]",
        ],
        "svc": [
            "Struct[{name => String, port => Optional[Integer]}]",
            "Struct[{name => String, port => Integer}]",
            "Struct[{name => String, Optional[port] => Integer}]",
            "Struct[{name => String, port => Any}]",
            "Struct[{name => String, port => Variant[Integer, Undef]}]",
            "Struct[{name => String, port => Optional[Integer], extra => Optional[Hash]}]",
        ],
        "svc_full": [
            "Struct[{name => String, port => Optional[Integer]}]",
            "Struct[{name => String, port => Optional[String]}]",
        ],
        "svc_nil": [
            "Struct[{name => String, port => Optional[Integer]}]",
            "Struct[{name => String, port => Integer}]",
            "Struct[{name => String, Optional[port] => Integer}]",
            "Struct[{name => String, NotUndef[port] => Optional[Integer]}]",
        ],
        "nil": [
            "Optional[Struct[{a => Integer}]]",
            "Variant[Undef, Integer]",
            "Tuple",
            "Struct",
            "Collection",
            "Iterable",
        ],
    }.items():
        for t in ts:
            s.q(k, type=t)
    yield s


@scenario
def extra_interp_keys(ctx):
    s = Scn("extra-interp-keys", "interp", "hash keys that interpolate to non-strings")
    s.simple()
    s.file(
        "data/common.yaml",
        d("""\
            t_int: 7
            t_bool: true
            t_nil: ~
            t_arr: [1, 2]
            t_hash: {a: 1}
            t_flt: 1.5
            t_str: s
            k_int: {"%{alias('t_int')}": v}
            k_bool: {"%{alias('t_bool')}": v}
            k_nil: {"%{alias('t_nil')}": v}
            k_arr: {"%{alias('t_arr')}": v}
            k_hash: {"%{alias('t_hash')}": v}
            k_flt: {"%{alias('t_flt')}": v}
            k_str: {"%{alias('t_str')}": v}
            k_int_dup: {"%{alias('t_int')}": a, "7": b}
            k_two_same: {"%{facts.a}": a, "%{::a}": b}
            k_in_arr: [{"%{alias('t_arr')}": v}]
            k_nested: {outer: {"%{alias('t_int')}": {"%{alias('t_bool')}": deep}}}
            """),
    )
    for k in [
        "k_int",
        "k_bool",
        "k_nil",
        "k_arr",
        "k_hash",
        "k_flt",
        "k_str",
        "k_int_dup",
        "k_two_same",
        "k_in_arr",
        "k_nested",
        "k_int.7",
        "k_nested.outer.7",
    ]:
        s.q(k)
    s.q("k_int", merge="deep")
    s.q("k_arr", merge="hash")
    yield s


@scenario
def extra_yaml_neighbours(ctx):
    files = {
        "anchor-empty-alias": ("k: &a\nj: *a\n", ["k", "j"]),
        "anchor-empty-in-map": ("h: {a: &x , b: 1}\nk: 1\n", ["h", "k"]),
        "anchor-null-explicit": ("k: &a ~\nj: *a\n", ["k", "j"]),
        "anchor-empty-seq-item": ("k:\n  - &a\n  - *a\n  - x\n", ["k"]),
        "tag-str-empty": ("k: !!str\nj: 1\n", ["k", "j"]),
        "tag-null-empty": ("k: !!null\nj: 1\n", ["k", "j"]),
        "tag-str-anchor-empty": ("k: &a !!str\nj: *a\n", ["k", "j"]),
        "empty-plain": ("k:\nj: 1\n", ["k", "j"]),
        "anchor-reuse-three": (
            "a: &x 1\nb: &x 2\nc: &x 3\nk: *x\nl: [*x]\n",
            ["k", "l", "a"],
        ),
        "anchor-reuse-nested": (
            "d: &d {t: 1}\ne:\n  f: &d {t: 2}\nk: *d\n",
            ["k", "e"],
        ),
        "anchor-reuse-after-alias": ("a: &x 1\nk: *x\nb: &x 2\nl: *x\n", ["k", "l"]),
        "merge-after-two-keys": (
            "b: &b {x: 1, y: 2, z: 3}\nk:\n  x: 9\n  <<: *b\n  z: 8\n",
            ["k"],
        ),
        "merge-middle-order": ("b: &b {m: 1}\nk:\n  a: 1\n  <<: *b\n  z: 2\n", ["k"]),
        "merge-first-order": (
            "b: &b {m: 1, a: 0}\nk:\n  <<: *b\n  a: 1\n  z: 2\n",
            ["k"],
        ),
        "merge-list-after": (
            "x: &x {a: 1}\ny: &y {a: 2, b: 2}\nk:\n  a: 0\n  b: 0\n  <<: [*x, *y]\n",
            ["k"],
        ),
        "merge-inline-map": ("k:\n  a: 0\n  <<: {a: 1, b: 2}\n", ["k"]),
        "merge-inline-map-first": ("k:\n  <<: {a: 1, b: 2}\n  a: 0\n", ["k"]),
        "merge-str-tagged": ("b: &b {a: 1}\nk:\n  !!str <<: *b\n  c: 1\n", ["k"]),
        "merge-double-quoted": ('b: &b {a: 1}\nk:\n  "<<": *b\n  c: 1\n', ["k"]),
        "merge-alias-to-list": ("b: &b [1, 2]\nk:\n  <<: *b\n  c: 1\n", ["k"]),
        "merge-alias-to-scalar": ("b: &b str\nk:\n  <<: *b\n  c: 1\n", ["k"]),
        "merge-string-value": ("k:\n  <<: text\n  c: 1\n", ["k"]),
        "merge-list-mixed": ("b: &b {a: 1}\nk:\n  <<: [*b, text]\n  c: 1\n", ["k"]),
        "merge-in-flow": ("b: &b {a: 1}\nk: {<<: *b, c: 1}\n", ["k"]),
        "merge-top-level-after": (
            "defaults: &d\n  k: from_merge\n  j: from_merge\nk: explicit\n<<: *d\n",
            ["k", "j"],
        ),
        "merge-top-level-first": (
            "defaults: &d\n  k: from_merge\n  j: from_merge\n<<: *d\nk: explicit\n",
            ["k", "j"],
        ),
        "merge-nested-deep": ("d: &d {h: {x: 1}}\nk:\n  h: {y: 2}\n  <<: *d\n", ["k"]),
        "merge-as-value": ("k: <<\nl: [<<]\n", ["k", "l"]),
        "map-tag-on-seq": ("k: !!map [1]\nj: 1\n", ["j", "k"]),
        "seq-tag-on-map": ("k: !!seq {a: 1}\nj: 1\n", ["j", "k"]),
        "omap-plain": ("k: !!omap\n  - a: 1\n  - b: 2\n", ["k"]),
    }
    for name, (content, keys) in files.items():
        s = Scn("yamlx-" + name, "yaml-struct", "yaml neighbour: " + name)
        s.simple()
        s.file("data/common.yaml", content)
        s.qs(keys)
        yield s
