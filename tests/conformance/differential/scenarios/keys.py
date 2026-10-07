"""Key syntax: dotted, quoted, namespaced, numeric, empty, odd characters;
--default, --type and several keys in one call."""

from __future__ import annotations

from ..scenario import Scn, V5, d, scenario

DATA = """\
h:
  a:
    b: hab
    "b.c": habc_quoted
  "0": str_zero
  l: [l0, [l10, l11], {k: lk}]
  "": empty_key
  " ": space_key
  "x y": spaced
  nil: ~
  f: false
  z: 0
  e: ""
  "a.b": flat_ab
  "'q'": single_quoted_key
  '"dq"': double_quoted_key
  "1": one
  "-1": minus_one
  "1.5": onefive
  "a::b": nsd
a.b: top_flat
arr: [a0, {k: v, l: [x, y]}, [n0, n1], ~, false]
s: string
i: 12
fl: 1.5
t: true
n: ~
"1": top_one
"1.2": top_onetwo
"0": top_zero
"x y": top_spaced
" ": top_space
"a::b": top_ns
"::lead": top_lead
"trail::": top_trail
mod::k: modk
mod::h: {a: {b: modhab}}
"mod::dotted.key": moddot
"'sq'": top_sq
'"dq"': top_dq
"quo'te": top_quote
UPPER: up
"dash-key": dash
"under_score": us
"k.": top_trail_dot
".k": top_lead_dot
"k..j": top_double_dot
"é": accent
"key with %{facts.a}": interp_key
"%{facts.a}": interp_key2
lookup_options:
  h: {merge: deep}
"""

KEYS = [
    "h",
    "h.a",
    "h.a.b",
    "h.a.'b.c'",
    'h.a."b.c"',
    "h.a.b.c",
    "h.a.0",
    "h.0",
    "h.'0'",
    'h."0"',
    "h.l",
    "h.l.0",
    "h.l.1",
    "h.l.1.0",
    "h.l.1.1",
    "h.l.2",
    "h.l.2.k",
    "h.l.3",
    "h.l.-1",
    "h.l.00",
    "h.l.01",
    "h.l.1.5",
    "h.l.x",
    "h.l. 1",
    "h.l.+1",
    "h.l.1 ",
    "h.l.0x1",
    "h.l.1e0",
    "h.",
    ".h",
    "h..a",
    "h. a",
    "h.a .b",
    " h",
    "h ",
    "h.''",
    'h.""',
    "h.' '",
    "h. ",
    "h.'x y'",
    "h.x y",
    "h.nil",
    "h.nil.x",
    "h.f",
    "h.f.x",
    "h.z",
    "h.z.x",
    "h.e",
    "h.e.x",
    "h.nope",
    "h.nope.x",
    "h.'a.b'",
    "h.a.b.",
    "'h'",
    '"h"',
    "'h'.a",
    "'h.a'",
    '"h.a"',
    "'h'.'a'.'b'",
    "'h.a",
    "h.a'",
    "h.'a",
    'h."a',
    "h.'a\"",
    "h.''q''",
    "h.'''q'''",
    "h.\"'q'\"",
    "h.'\"dq\"'",
    "h.1",
    "h.'1'",
    "h.-1",
    "h.'-1'",
    "h.1.5",
    "h.'1.5'",
    "h.a::b",
    "h.'a::b'",
    "a.b",
    "'a.b'",
    '"a.b"',
    "a",
    "arr",
    "arr.0",
    "arr.1",
    "arr.1.k",
    "arr.1.l.1",
    "arr.2.0",
    "arr.3",
    "arr.4",
    "arr.5",
    "arr.3.x",
    "arr.4.x",
    "arr.k",
    "arr.'0'",
    'arr."0"',
    "arr.-1",
    "arr.0.0",
    "arr.0.x",
    "s.x",
    "s.0",
    "i.x",
    "i.0",
    "fl.x",
    "t.x",
    "n.x",
    "n",
    "1",
    "1.2",
    "'1.2'",
    "0",
    "0.0",
    "x y",
    "'x y'",
    " ",
    "' '",
    "",
    "''",
    '""',
    ".",
    "..",
    "'",
    '"',
    "''.x",
    "a::b",
    "'a::b'",
    "::a::b",
    "::lead",
    "::::lead",
    "trail::",
    "::h",
    "::h.a.b",
    "::s",
    "h::a",
    "mod::k",
    "::mod::k",
    "mod::h.a.b",
    "mod::h.a",
    "mod::dotted.key",
    "'mod::dotted.key'",
    "mod::'dotted.key'",
    "mod::nope",
    "nomod::k",
    "'sq'",
    "''sq''",
    "\"'sq'\"",
    '"dq"',
    "'\"dq\"'",
    "quo'te",
    "UPPER",
    "upper",
    "Upper",
    "dash-key",
    "under_score",
    "k.",
    "'k.'",
    ".k",
    "'.k'",
    "k..j",
    "'k..j'",
    "é",
    "e",
    "key with alpha",
    "key with %{facts.a}",
    "'key with %{facts.a}'",
    "alpha",
    "%{facts.a}",
    "lookup_options",
    "lookup_options.h",
    "lookup_options.h.merge",
    "lookup_options.nope",
    "H",
    "h.A",
    "nope",
    "nope.x",
    "nope.'x.y'",
    "h.a.b.c.d.e",
    "h.l.2.k.x",
    "*",
    "?",
    "h.*",
    "[",
    "h[a]",
    "h['a']",
    "h/a",
    "h\\a",
    "h,a",
    "h a",
    "h\ta",
    "h\n",
    "\nh",
    "1e3",
    "0x1",
    "true",
    "null",
    "~",
    "nil",
    "h.l.9999999999999999999999",
    "h.l.0.0.0",
    "a.b.c",
]


@scenario
def keys_dotted(ctx):
    for strict in ("warning", "error"):
        s = Scn(
            "keys-dotted-" + strict,
            "keys",
            "key syntax against hashes, arrays, scalars",
        )
        s.simple()
        s.file("data/common.yaml", DATA)
        for k in KEYS:
            s.q(k, args=["--strict", strict] if strict != "warning" else None)
        yield s
    s = Scn("keys-dotted-merge", "keys", "dotted keys with merges and defaults")
    s.simple(["top", "common"])
    s.file(
        "data/top.yaml",
        "h:\n  a: {c: top_c}\n  l: [t0]\n  only_top: 1\narr: [t]\ns: {x: 1}\n",
    )
    s.file("data/common.yaml", DATA)
    for k in [
        "h.a",
        "h.a.b",
        "h.a.c",
        "h.l",
        "h.l.0",
        "h.l.1",
        "h.only_top",
        "h.z",
        "arr.0",
        "arr.1",
        "arr.1.k",
        "s.x",
        "s",
        "h.nope",
        "h.a.nope",
        "h.nil",
        "h.f",
        "h.e",
        "h.l.5",
    ]:
        for m in [None, "first", "deep", "unique", "hash"]:
            s.q(k, merge=m)
        s.q(k, default="D")
        s.q(k, default="D", merge="deep")
    yield s


@scenario
def keys_cli_forms(ctx):
    s = Scn("keys-default-type", "cli", "--default, --type and name lists")
    s.simple(["top", "common"])
    s.file(
        "data/top.yaml",
        "t_nil: ~\nt_false: false\nt_empty: ''\nt_arr: []\nt_hash: {}\nt_zero: 0\n",
    )
    s.file(
        "data/common.yaml",
        d("""\
            str: hello
            int: 42
            neg: -3
            flt: 1.5
            flt_int: 2.0
            bool: true
            nil: ~
            arr: [1, 2, 3]
            arr_s: [a, b]
            arr_mixed: [1, a, ~, 2.5, true]
            hash: {a: 1, b: two}
            hash_nested: {a: {b: [1, 2]}}
            empty_s: ""
            num_s: "42"
            big: 12345678901234567890
            t_nil: common
            t_false: common
            t_empty: common
            t_arr: [common]
            t_hash: {c: 1}
            t_zero: 9
            sens: secret
            lookup_options:
              sens: {convert_to: Sensitive}
            """),
    )
    types = {
        "str": [
            "String",
            "String[1]",
            "String[6]",
            "String[1,3]",
            "Integer",
            "Enum[hello, bye]",
            "Enum[bye]",
            "Pattern[/^h/]",
            "Pattern[/^x/]",
            "Variant[Integer, String]",
            "Data",
            "Scalar",
            "Any",
            "Optional[String]",
            "NotUndef",
            "Undef",
            "Array",
            "Array[String]",
            "Hash",
            "Boolean",
            "Numeric",
            "Float",
            "ScalarData",
            "RichData",
            "Collection",
            "Iterable",
            "Type",
            "Default",
            "Sensitive",
            "Sensitive[String]",
            "Regexp",
            "Callable",
            "String[]",
            "string",
            "Strin",
            "",
            "Integer[",
            "Enum",
            "Variant",
            "Struct",
            "Tuple",
            "Hash[String]",
            "Optional",
            "NotUndef[String]",
            "Stdlib::Absolutepath",
            "Foo",
            "Integer[1,2,3]",
            "String[a]",
            "Array[1]",
            "Variant[String",
            "'String'",
            "String ",
            " String",
            "Enum['hello']",
            'Enum["hello"]',
            "Enum[Hello]",
            "Pattern['^h']",
            "Pattern[/^H/]",
            "Pattern[/^h/, /x/]",
            "Optional['hello']",
            "Optional[hello]",
            "Timestamp",
            "SemVer",
            "Binary",
            "URI",
            "Init",
            "Iterator",
            "Runtime",
            "Object",
            "TypeSet",
            "Target",
            "Error",
            "Deferred",
            "Type[String]",
            "Type[Integer]",
            "CatalogEntry",
            "Resource",
            "Class",
            "Variant[]",
            "Enum[]",
            "Struct[{}]",
            "Tuple[]",
        ],
        "int": [
            "Integer",
            "Integer[1]",
            "Integer[1,10]",
            "Integer[42,42]",
            "Integer[43]",
            "Integer[default,41]",
            "Integer[10,1]",
            "Float",
            "Numeric",
            "String",
            "Scalar",
            "Variant[String, Float]",
            "Integer[1.5]",
            "Integer['1']",
            "Integer[-1, default]",
            "Integer[default, default]",
            "Enum[42]",
            "Enum['42']",
            "Optional[Integer[100]]",
            "NotUndef[Integer]",
            "Integer[0x10]",
            "Integer[1,]",
            "Positive",
            "Boolean",
        ],
        "neg": ["Integer[0]", "Integer[-5,0]", "Float[-5.0, 0.0]"],
        "flt": [
            "Float",
            "Float[1.0]",
            "Float[1.0, 2.0]",
            "Float[2.0]",
            "Float[1]",
            "Float[1, 2]",
            "Integer",
            "Numeric",
            "Scalar",
            "String",
            "Float[default, 1.0]",
            "Float['1.0']",
        ],
        "flt_int": ["Integer", "Float", "Numeric"],
        "bool": [
            "Boolean",
            "Boolean[true]",
            "Boolean[false]",
            "String",
            "Scalar",
            "Integer",
            "Enum[true]",
            "Boolean[1]",
            "Boolean['true']",
        ],
        "nil": [
            "Undef",
            "Optional[String]",
            "String",
            "Any",
            "Data",
            "NotUndef",
            "Scalar",
            "Variant[Undef, Integer]",
            "Optional",
            "Array",
            "Default",
        ],
        "arr": [
            "Array",
            "Array[Integer]",
            "Array[String]",
            "Array[Integer, 1]",
            "Array[Integer, 4]",
            "Array[Integer, 1, 2]",
            "Array[Integer[1,2]]",
            "Tuple[Integer, Integer, Integer]",
            "Tuple[Integer, Integer]",
            "Tuple[Integer, 3]",
            "Tuple[Integer, 1, 2]",
            "Tuple[Integer, String, Integer]",
            "Hash",
            "Collection",
            "Iterable",
            "Data",
            "Array[Any, 3, 3]",
            "Array[Scalar]",
            "Tuple[Integer, 1, default]",
            "Tuple[Integer, Optional[Integer], Integer, Integer]",
            "Array[1, 2]",
            "Array[Integer, default, 2]",
            "Collection[3]",
            "Collection[4]",
            "Collection[1, 2]",
            "Iterable[Integer]",
            "Iterable[String]",
            "Array[Variant[Integer, String]]",
        ],
        "arr_s": [
            "Array[String]",
            "Array[Enum[a, b]]",
            "Array[Enum[a]]",
            "Array[Pattern[/a/]]",
            "Tuple[String, String]",
            "Tuple[Enum[a], Enum[c]]",
            "Array[String[2]]",
        ],
        "arr_mixed": [
            "Array",
            "Array[Data]",
            "Array[Scalar]",
            "Array[Optional[Scalar]]",
            "Array[Variant[Integer, String]]",
            "Tuple[Integer, String, Undef, Float, Boolean]",
        ],
        "hash": [
            "Hash",
            "Hash[String, Data]",
            "Hash[String, Integer]",
            "Hash[String, String]",
            "Hash[Integer, Data]",
            "Hash[String, Data, 2, 2]",
            "Hash[String, Data, 3]",
            "Struct[{a => Integer, b => String}]",
            "Struct[{a => Integer}]",
            "Struct[{a => Integer, b => String, c => String}]",
            "Struct[{a => Integer, b => String, Optional[c] => String}]",
            "Struct[{a => String, b => String}]",
            "Struct[{'a' => Integer, 'b' => String}]",
            "Struct[{a => Integer, b => Integer}]",
            "Struct[{a => Integer, NotUndef[b] => String}]",
            "Struct[{a => Integer, b => Optional[String], c => Optional[String]}]",
            "Array",
            "Collection",
            "Hash[Enum[a, b], Data]",
            "Hash[Enum[a], Data]",
            "Hash[Pattern[/^[ab]$/], Variant[Integer, String]]",
            "Struct[a => Integer]",
            "Struct[{a => Integer, b => Enum[two, three]}]",
            "Struct[{a => Integer[2], b => String}]",
            "Hash[String]",
            "Hash[String, Data, 1, 1]",
            "Hash[Scalar, Scalar]",
            "Struct[{Optional[a] => Integer, Optional[b] => String}]",
            "Struct[{}]",
        ],
        "hash_nested": [
            "Hash[String, Hash[String, Array[Integer]]]",
            "Hash[String, Hash[String, Array[String]]]",
            "Struct[{a => Struct[{b => Tuple[Integer, Integer]}]}]",
            "Struct[{a => Struct[{b => Tuple[Integer, String]}]}]",
            "Struct[{a => Struct[{c => Integer}]}]",
            "Hash[String, Hash]",
            "Hash[String, Array]",
        ],
        "empty_s": [
            "String",
            "String[1]",
            "String[0,0]",
            "Enum['']",
            "Optional[String[1]]",
            "NotUndef",
        ],
        "num_s": ["Integer", "String", "Numeric", "Pattern[/^\\d+$/]", "Scalar"],
        "big": ["Integer", "Float", "Integer[0]", "String"],
        "sens": [
            "Sensitive",
            "Sensitive[String]",
            "Sensitive[Integer]",
            "String",
            "Any",
            "Data",
            "RichData",
        ],
        "nope": ["String", "Undef", "Optional[String]", "Any"],
    }
    for k, ts in types.items():
        for t in ts:
            s.q(k, type=t)
    for k in [
        "str",
        "int",
        "flt",
        "bool",
        "nil",
        "arr",
        "hash",
        "empty_s",
        "nope",
        "t_nil",
        "t_false",
        "t_empty",
        "t_arr",
        "t_hash",
        "t_zero",
        "sens",
    ]:
        s.q(k)
        s.q(k, default="D")
        s.q(k, default="")
    for dflt, t in [
        ("D", "String"),
        ("D", "Integer"),
        ("5", "Integer"),
        ("5", "String"),
        ("", "String[1]"),
        ("true", "Boolean"),
        ("[1]", "Array"),
        ("{}", "Hash"),
        ("~", "Undef"),
        ("D", "Enum[D]"),
        ("D", "Enum[E]"),
        ("1.5", "Float"),
        ("D", "Any"),
        ("D", "Optional[Integer]"),
        ("5", "Variant[Integer, String]"),
        ("null", "Undef"),
        ("nil", "Optional[String]"),
    ]:
        s.q("nope", default=dflt, type=t)
        s.q("int", default=dflt, type=t)
    for keys in (
        ["nope", "str"],
        ["str", "int"],
        ["nope", "nope2"],
        ["nope", "nil", "str"],
        ["t_nil", "str"],
        ["nope", "hash.a"],
        ["hash.nope", "arr.0"],
        ["nope", "t_false"],
        ["nope", "nope", "int"],
        ["", "str"],
        ["str", ""],
        ["h.x", "str"],
        ["str.x", "int"],
    ):
        s.q(keys)
        s.q(keys, default="D")
        s.q(keys, merge="unique")
        s.q(keys, type="String")
    # option spelling
    for args in (
        ["--default=D"],
        ["--default", "D", "--default", "E"],
        ["--type=String"],
        ["--type", "String", "--type", "Integer"],
        ["--default", "-x"],
        ["--default=-x"],
        ["--default", "--x"],
        ["--default", "a b"],
        ["--default", "%{facts.a}"],
        ["--default", "%{lookup('str')}"],
        ["--default", "%{nope}"],
        ["--default", "1", "--type", "Integer"],
        ["--strict", "bogus"],
        ["--strict=off"],
        ["--explain-options"],
        ["--explain", "--render-as", "json"],
        ["--node"],
        ["--environment", "nope"],
        ["--environment", "production"],
        ["--compile"],
        ["--bogus"],
        ["-d"],
        ["--debug"],
        ["--verbose"],
        ["-v"],
        ["--render-as", "bogus"],
        ["--render-as", "yaml"],
        ["--render-as", "s"],
        ["--merge", "deep", "--default", "D"],
        ["--trusted"],
        ["--facts", "nope.yaml"],
        ["--hiera_config", "nope.yaml"],
        ["--environmentpath", "/nonexistent"],
    ):
        s.q("nope", args=args)
        s.q("str", args=args)
    yield s


@scenario
def keys_no_args(ctx):
    s = Scn("keys-no-key", "cli", "no key / odd argv")
    s.simple()
    s.file("data/common.yaml", "k: v\n")
    # a query always has a key, so an empty key list is not expressible
    s.q("k")
    s.q(["k", "k"])
    s.q(["k"] * 5 + ["nope"])
    yield s


V5_ENV_LIKE = V5
