"""YAML typing in data files: where Ruby's Psych and PyYAML differ."""

from __future__ import annotations

from ..scenario import Scn, d, scenario

BOOLS = "yes Yes YES no No NO on On ON off Off OFF y Y n N true True TRUE false False FALSE tRue yEs oN fALSE".split()
NULLS = ["~", "null", "Null", "NULL", "", "nUll", "nil", "None", "~~", "undef"]
INTS = [
    "0",
    "00",
    "010",
    "0o10",
    "0O10",
    "0x1F",
    "0X1F",
    "0x1f",
    "0b101",
    "0B101",
    "1_000",
    "1__000",
    "_1",
    "1_",
    "+1",
    "-1",
    "+0",
    "-0",
    "08",
    "09",
    "089",
    "0_7",
    "1:30",
    "1:30:00",
    "190:20:30",
    "-1:30",
    "1:60",
    "01:30",
    "1:5",
    "1:05:5",
    "123456789012345678901234567890",
    "-123456789012345678901234567890",
    "9223372036854775807",
    "9223372036854775808",
    "18446744073709551616",
    "-0x1f",
    "+0x1f",
    "0x_1f",
    "0x1_f",
    "0b_1",
    "0b1_0",
    "0x",
    "0b",
    "0o",
    "0o8",
    "0b2",
    "0xg",
    "1,000",
    "1,000,000",
    "1,",
    "0,0",
    "+",
    "-",
    "1e",
    "12e03",
    "0_",
    "0__1",
    "+010",
    "-010",
    "0o17",
    "-0o17",
    "017",
    "0o_7",
    "0777",
    "0888",
    "1 000",
    "1e3",
    "+1_0",
    "0x-1",
    "0b-1",
    "00x1",
    "1:2:3:4",
    "0:0",
    "1:",
    "0xFFFFFFFFFFFFFFFFFF",
    "+12:30",
]
FLOATS = [
    ".5",
    "0.5",
    "1.",
    "1.0",
    "-.5",
    "+.5",
    "1E3",
    "1.5e3",
    "1.5E+3",
    "1.5e-3",
    "1e-3",
    "1.e3",
    ".5e3",
    ".5e+3",
    ".inf",
    ".Inf",
    ".INF",
    "+.inf",
    "-.inf",
    ".nan",
    ".NaN",
    ".NAN",
    ".iNf",
    "-.nan",
    "+.nan",
    "1_000.5",
    "1,000.5",
    "1:30.5",
    "1:30:45.5",
    ".",
    "..",
    "-.",
    "+.",
    "1.0.0",
    "0x1.8p3",
    "1.7976931348623157e+308",
    "5e-324",
    "0.1e1",
    "6.02E23",
    "6.8523015e+5",
    "685.230_15e+03",
    "685_230.15",
    "190:20:30.15",
    "inf",
    "nan",
    "Infinity",
    "NaN",
    "0.0",
    "-0.0",
    "1.0e+10",
    "100000000000000000000.0",
    "1.23456789012345678",
    "1e16",
    "1.0e16",
    "1.0e+16",
    "1.0e15",
    "1.0e-4",
    "1.0e-5",
    "0.00001",
    "0.0001",
    "123456789.123456789",
    "1.0e+20",
    "3.0",
    "3.10",
    "2.50",
    "1e+3",
    "1.e+3",
    "1.5e",
    "1.5e+",
    "._5",
    "1._5",
    "_.5",
    "0.1_",
    "-1.5",
    "+1.5",
    "1.0e3",
    "1.0E3",
    "0e0",
    "0.e0",
    "00.5",
    "1.5:30",
    ".5.",
    "4.",
    "-4.",
    "1.e",
    "1__2.5",
]
STRINGS = [
    "0123456789",
    "1.2.3",
    "0.",
    "http://x:80/y",
    "a:b",
    "a:1",
    "'single'",
    '"dq\\nx"',
    "plain text",
    "a # comment",
    "a#notcomment",
    "#only",
    "'#quoted'",
    "é",
    "日本語",
    '"\\x41\\u00e9\\U0001F600"',
    '"\\e\\0x"',
    '"a\\_b\\Nc\\Ld\\Pe"',
    '"a\\/b"',
    '"tab\\there"',
    "'it''s'",
    "a\tb",
    "trailing   ",
    "   leading",
    "- x",
    "? x",
    "a, b",
    "[a, b",
    "x]y",
    "{a",
    "a}",
    "a|b",
    "a>b",
    "<<",
    "=",
    "a=b",
    "!a",
    "a!",
    "a&b",
    "a*b",
    "-",
    "--",
    "---x",
    "...x",
    "x: y",
    "'x: y'",
    "x:y",
    "x:",
    ":",
    "::",
    "a::b",
    "~a",
    "a~",
    "null ",
    "truely",
    "yesterday",
    "nope",
    "onward",
    "offer",
    "0xZZ",
    "1 2",
    "1-2",
    "12-34-56",
    "+12",
    "+-1",
    "1+1",
    "$var",
    "\\backslash",
    "c:\\path",
    "'c:\\path'",
    '"c:\\\\path"',
    "100%",
    "%",
    "50%{x}",
    '"\\"quoted\\""',
    "'\"q\"'",
    "''",
    '""',
    "' '",
    '"\\t"',
    "a  b",
    '"a\\\n  b"',
]
SYMBOLS = [
    ":sym",
    ':"quoted sym"',
    ":'sq'",
    '":dq"',
    "':sq'",
    ":a:b",
    ":1",
    ":",
    "::a",
    ":a b",
    ":+",
    ":sym_bol",
    ":Sym",
    ":a.b",
    ":a-b",
    ": x",
    ":a?",
    ":a!",
    ":a=",
    ":[]",
    ':""',
    ":'x' y",
    ":a,b",
    ":123abc",
    ":true",
    ":nil",
]
DATES = [
    "2001-12-14",
    "2001-1-1",
    "2001-12-14t21:59:43.10-05:00",
    "2001-12-14 21:59:43.10 -5",
    "2001-12-15 2:59:43.10",
    "2001-12-14T21:59:43Z",
    "2001-13-45",
    "2001-02-30",
    "99-1-1",
    "'2001-12-14'",
    "12:30:45",
    "2001-12-14 21:59:43",
    "2001-12-14T21:59:43",
    "2001-12-14 21:59:43 +0530",
    "2001-12-14 21:59:43Z",
    "2001-12-14t21:59:43",
    "2001-12-14 21:59",
    "0000-00-00",
    "2001-12-32",
    "2001-00-10",
    "2001-12-14 ",
    "2001-12-14T21:59:43.123456789Z",
    "2001-12-14  21:59:43",
    "2001-12",
    "20011214",
    "2001-12-14T",
    "2001-2-3 4:5:6",
    "1970-01-01 00:00:00 +00:00",
    "2024-02-29",
    "2023-02-29",
    "2001-12-14 21:59:43 -05:00",
    "2001-12-14 24:00:00",
]
TAGS = [
    "!!str 123",
    '!!int "123"',
    "!!int 0x1f",
    "!!float 1",
    '!!float "1.5"',
    "!!bool yes",
    '!!bool "true"',
    "!!null ''",
    "!!null x",
    "!!binary aGVsbG8=",
    "!!set {a, b}",
    "!!omap [a: 1, b: 2]",
    "!!timestamp 2001-12-14",
    "!ruby/object:Foo {a: 1}",
    "!custom x",
    "!!python/object:os.system x",
    "! 12",
    "!!map {a: 1}",
    "!!seq [1, 2]",
    "!!str",
    "!!str ~",
    "!!str true",
    "!ruby/symbol foo",
    "!ruby/sym foo",
    "!ruby/regexp /a/",
    "!ruby/range 1..2",
    "!ruby/string foo",
    "!!int abc",
    "!!float abc",
    "!!bool maybe",
    "!!pairs [a: 1]",
    "!<tag:yaml.org,2002:str> 5",
    "!!merge x",
    "!!value x",
    "!ruby/hash:Foo {a: 1}",
    "!ruby/array:Foo [1]",
    "!ruby/struct:Foo {a: 1}",
    "!!int 1_000",
    "!!int '010'",
    "!!float .inf",
    "!str 5",
    "!int 5",
    "!float 5",
    "!bool yes",
    "!binary aGVsbG8=",
    "!!map [1]",
    "!!seq {a: 1}",
    "!!str [1, 2]",
    "!ruby/object:Set {}",
    "!!yaml x",
    "!ruby/exception:StandardError {message: x}",
    "!ruby/class String",
    "!ruby/module Kernel",
    "!ruby/encoding UTF-8",
    "!ruby/data:Foo {a: 1}",
    "!!int 1:30",
    "!!float 1:30.5",
    "!!str 2001-12-14",
    "!!timestamp x",
]

# Whole files whose shape (not one value) is the point.
STRUCT = {
    "dup-keys": ("k: first\nother: 1\nk: second\n", ["k", "other"]),
    "dup-keys-nested": ("h:\n  a: 1\n  b: 2\n  a: 3\n", ["h"]),
    "dup-keys-flow": ("h: {a: 1, a: 2}\n", ["h"]),
    "merge-key": (
        "base: &b {a: 1, b: 2}\nk:\n  <<: *b\n  b: 3\n  c: 4\n",
        ["k", "base"],
    ),
    "merge-key-list": (
        "x: &x {a: 1, z: x}\ny: &y {a: 2, b: 2, z: y}\nk:\n  <<: [*x, *y]\n  c: 3\n",
        ["k"],
    ),
    "merge-key-after": ("base: &b {a: 1, b: 2}\nk:\n  b: 3\n  <<: *b\n", ["k"]),
    "merge-key-twice": (
        "x: &x {a: 1}\ny: &y {a: 2, b: 2}\nk:\n  <<: *x\n  <<: *y\n",
        ["k"],
    ),
    "merge-key-scalar": ("k:\n  <<: notamap\n  a: 1\n", ["k"]),
    "merge-key-seq-scalar": ("k:\n  <<: [1, 2]\n  a: 1\n", ["k"]),
    "merge-key-quoted": ("base: &b {a: 1}\nk:\n  '<<': *b\n  c: 4\n", ["k"]),
    "merge-key-top": ("base: &b\n  a: 1\n<<: *b\nk: v\n", ["k", "a", "base"]),
    "merge-key-nested-override": (
        "d: &d {h: {x: 1, y: 2}}\nk:\n  <<: *d\n  h: {x: 9}\n",
        ["k"],
    ),
    "merge-key-null": ("k:\n  <<: ~\n  a: 1\n", ["k"]),
    "merge-key-empty-list": ("k:\n  <<: []\n  a: 1\n", ["k"]),
    "anchor-scalar": ("a: &a hello\nk: *a\nl: [*a, *a]\n", ["k", "l"]),
    "anchor-seq": ("a: &a [1, 2]\nk: *a\nh: {x: *a}\n", ["k", "h"]),
    "anchor-redefine": ("a: &a 1\nb: &a 2\nk: *a\n", ["k"]),
    "anchor-undefined": ("k: *nope\n", ["k"]),
    "anchor-recursive": ("k: &k [1, *k]\nother: 1\n", ["other", "k"]),
    "anchor-recursive-map": ("k: &k {self: *k}\nother: 1\n", ["other"]),
    "anchor-on-key": ("&k key: v\nk2: *k\n", ["key", "k2"]),
    "alias-as-key": ("a: &a name\n*a : v\nk: 1\n", ["name", "k"]),
    "anchor-interp": ('a: &a "%{facts.a}"\nk: *a\nl: {x: *a}\n', ["k", "l"]),
    "bom": (b"\xef\xbb\xbfk: v\nn: 1\n", ["k", "n"]),
    "bom-doc-start": (b"\xef\xbb\xbf---\nk: v\n", ["k"]),
    "utf16le-bom": ("k: v\n".encode("utf-16"), ["k"]),
    "utf16be-bom": (b"\xfe\xff" + "k: v\n".encode("utf-16-be"), ["k"]),
    "utf16le-nobom": ("k: v\n".encode("utf-16-le"), ["k"]),
    "utf32": ("k: v\n".encode("utf-32"), ["k"]),
    "latin1": (b"k: caf\xe9\n", ["k"]),
    "invalid-utf8-comment": (b"# \xff\xfe\nk: v\n", ["k"]),
    "nul-byte": (b"k: a\x00b\n", ["k"]),
    "control-char": (b"k: a\x01b\n", ["k"]),
    "del-char": (b"k: a\x7fb\n", ["k"]),
    "nel": ("k: a\u0085b\nn: 1\n".encode("utf-8"), ["k", "n"]),
    "ls-ps": ("k: a\u2028b\nm: c\u2029d\nn: 1\n".encode("utf-8"), ["k", "m", "n"]),
    "nbsp": ("k: a\u00a0b\n\u00a0x: 1\n".encode("utf-8"), ["k"]),
    "crlf": (
        b"k: v\r\nn: 1\r\nblock: |\r\n  line1\r\n  line2\r\nfolded: >\r\n  a\r\n  b\r\n",
        ["k", "n", "block", "folded"],
    ),
    "cr-only": (b"k: v\rn: 1\r", ["k", "n"]),
    "mixed-eol": (b"k: v\r\nn: 1\nm: 2\r\n", ["k", "n", "m"]),
    "tabs-indent": ("k:\n\ta: 1\n", ["k"]),
    "tab-after-colon": ("k:\tv\nh:\n  a:\t1\n", ["k", "h"]),
    "tab-in-flow": ("k: [a,\tb]\nh: {a:\t1}\n", ["k", "h"]),
    "tab-before-comment": ("k: v\t# c\n", ["k"]),
    "trailing-tab": ("k: v\t\n", ["k"]),
    "multi-doc": ("---\nk: first\n---\nk: second\nj: 2\n", ["k", "j"]),
    "multi-doc-end": ("k: first\n...\n---\nk: second\n", ["k"]),
    "doc-end-only": ("k: v\n...\n", ["k"]),
    "doc-end-junk": ("k: v\n...\njunk\n", ["k"]),
    "empty-file": ("", ["k"]),
    "only-comments": ("# nothing\n# here\n", ["k"]),
    "only-doc-start": ("---\n", ["k"]),
    "only-doc-start-null": ("--- ~\n", ["k"]),
    "only-whitespace": ("   \n\n", ["k"]),
    "only-newline": ("\n", ["k"]),
    "top-list": ("- a\n- b\n", ["k", "0"]),
    "top-scalar": ("just a string\n", ["k"]),
    "top-int": ("42\n", ["k"]),
    "top-false": ("false\n", ["k"]),
    "top-empty-map": ("{}\n", ["k"]),
    "top-empty-list": ("[]\n", ["k"]),
    "top-list-of-maps": ("- k: v\n", ["k"]),
    "top-string-quoted": ("'k: v'\n", ["k"]),
    "invalid-indent": ("k:\n  a: 1\n b: 2\n", ["k"]),
    "invalid-unclosed-flow": ("k: [a, b\n", ["k"]),
    "invalid-unclosed-quote": ("k: 'abc\n", ["k"]),
    "invalid-colon": ("k: a: b\n", ["k"]),
    "invalid-tab-key": ("\tk: v\n", ["k"]),
    "invalid-directive": ("%FOO bar\n---\nk: v\n", ["k"]),
    "yaml-1.1-directive": ("%YAML 1.1\n---\nk: yes\nn: 010\n", ["k", "n"]),
    "yaml-1.2-directive": (
        "%YAML 1.2\n---\nk: yes\nn: 010\no: 0o10\n",
        ["k", "n", "o"],
    ),
    "yaml-2.0-directive": ("%YAML 2.0\n---\nk: v\n", ["k"]),
    "tag-directive": ("%TAG !e! tag:example.com,2000:\n---\nk: !e!foo bar\n", ["k"]),
    "reserved-at": ("k: @at\n", ["k"]),
    "reserved-backtick": ("k: `bt\n", ["k"]),
    "pct-start": ("k: %pct\n", ["k"]),
    "key-int": ("1: one\nk: v\n", ["1", "k"]),
    "key-int-nested": ("h: {1: one, 2: two}\n", ["h", "h.1"]),
    "key-bool": ("true: t\nyes: y\nk: v\n", ["true", "yes", "k"]),
    "key-bool-nested": ("h: {true: t, no: n, on: o}\n", ["h", "h.true"]),
    "key-null": ("~: nul\nk: v\n", ["k", "", "~"]),
    "key-null-nested": ("h: {~: nul, null: n2}\n", ["h"]),
    "key-float-nested": ("h: {1.5: f, .inf: i}\n", ["h", "h.1.5"]),
    "key-empty-str": ('"": empty\nk: v\n', ["", "k"]),
    "key-empty-nested": ('h: {"": empty, " ": space}\n', ["h", "h."]),
    "key-space": ("a b: spaced\n'c d': q\n", ["a b", "c d"]),
    "key-symbol": (":sym: s\nk: v\n", ["sym", ":sym", "k"]),
    "key-symbol-nested": (
        "h: {:sym: s, :other: {:deep: 1}}\n",
        ["h", "h.sym", "h.:sym"],
    ),
    "key-symbol-in-arr": ("a: [{:sym: s}]\n", ["a"]),
    "key-symbol-dup": ("h: {:a: sym, a: str}\n", ["h"]),
    "key-list-nested": ("h: {[a, b]: v}\n", ["h"]),
    "key-map-nested": ("h: {{a: 1}: v}\n", ["h"]),
    "key-complex": ("? [a, b]\n: v\nk: 1\n", ["k"]),
    "key-date-nested": ("h: {2001-12-14: v}\n", ["h"]),
    "key-long": ("k" * 1100 + ": v\n", ["k" * 1100]),
    "key-question": ("? k\n: v\n", ["k"]),
    "key-dotted": ("a.b: flat\na:\n  b: nested\n", ["a.b", "'a.b'", '"a.b"', "a"]),
    "key-colons": ("a::b: v\n'::c': w\n", ["a::b", "::c", "c"]),
    "key-quoted-num": ("\"1\": one\n'2': two\n", ["1", "2"]),
    "key-unicode": ("clé: v\n日本: w\n", ["clé", "日本"]),
    "key-case": ("Key: upper\nkey: lower\n", ["Key", "key", "KEY"]),
    "value-symbol-nested": ("h: {a: :sym, b: [:x, :y]}\n", ["h", "h.a"]),
    "value-date-nested": ("h: {a: 2001-12-14}\nk: v\n", ["k", "h"]),
    "value-set": ("k: !!set\n  ? a\n  ? b\n", ["k"]),
    "value-nan-nested": ("h: {a: .nan, b: .inf}\nk: v\n", ["h", "k"]),
    "block-scalars": (
        "lit: |\n  a\n  b\nlit_strip: |-\n  a\n  b\nlit_keep: |+\n  a\n  b\n\nfold: >\n  a\n  b\n\n  c\n   more\n  d\nfold_strip: >-\n  a\n  b\nindent: |2\n    two extra\n  base\nlit_empty: |\nnext: x\n",
        [
            "lit",
            "lit_strip",
            "lit_keep",
            "fold",
            "fold_strip",
            "indent",
            "lit_empty",
            "next",
        ],
    ),
    "plain-multiline": (
        "k: a\n  b\n\n  c\nj: 'a\n  b'\ni: \"a\n  b\\\n  c\"\n",
        ["k", "j", "i"],
    ),
    "flow-multiline": ("k: [a,\n  b,\n  ]\nh: {a: 1,\n  b: 2,}\n", ["k", "h"]),
    "flow-implicit-null": ("k: [a, , b]\n", ["k"]),
    "flow-single-pair": ("k: [a: 1, b]\nh: {a, b: 1}\n", ["k", "h"]),
    "flow-no-space": ('k: {a:1}\nj: [a:1]\ni: {"a":1}\n', ["k", "j", "i"]),
    "flow-adjacent": ('k: {"a":1,"b":[1,2],"c":{"d":null}}\n', ["k"]),
    "seq-indent-zero": ("k:\n- a\n- b\nj: 1\n", ["k", "j"]),
    "seq-of-seq": ("k:\n  - - a\n    - b\n  - - c\n", ["k"]),
    "seq-of-map-compact": ("k:\n  - a: 1\n    b: 2\n  - c: 3\n", ["k", "k.0.b", "k.1"]),
    "empty-values": (
        "a:\nb: ''\nc: []\nd: {}\ne: ~\nf: ' '\ng:\n  -\n  - x\nh:\n  x:\n",
        list("abcdefgh"),
    ),
    "comments-everywhere": (
        "# c\nk: v # c\n# c\nh: # c\n  a: 1 # c\n  # c\nl: [a, # c\n  b]\n",
        ["k", "h", "l"],
    ),
    "long-line": ("k: " + "x" * 5000 + "\n", ["k"]),
    "deep-nesting": ("k: " + "[" * 60 + "x" + "]" * 60 + "\n", ["k"]),
    "big-int-keys": ("h: {123456789012345678901234567890: v}\n", ["h"]),
    "lookup-options-non-hash": (
        "lookup_options: notahash\nk: v\n",
        ["k", "lookup_options"],
    ),
    "lookup-options-list": ("lookup_options: [a]\nk: v\n", ["k"]),
    "lookup-options-null": ("lookup_options: ~\nk: v\n", ["k", "lookup_options"]),
    "nonstring-top-keys-mixed": (
        "1: a\n1.5: b\ntrue: c\n~: d\nk: v\n",
        ["k", "1", "1.5", "true"],
    ),
    "non-data-value": ("k: !!binary aGVsbG8=\nother: 1\n", ["other", "k"]),
    "octal-strings": (
        "mode: 0644\nmode2: '0644'\nmode3: 0o644\nmode4: 644\nmode5: 0755\nmode6: 0855\n",
        ["mode", "mode2", "mode3", "mode4", "mode5", "mode6"],
    ),
    "versions": (
        "v1: 1.10\nv2: 1.2.3\nv3: 10\nv4: 1.0\nv5: 1.10.0\nv6: 3.10\nv7: '3.10'\nv8: 2e5\nv9: 1_1\nv10: 0.10\n",
        ["v%d" % i for i in range(1, 11)],
    ),
    "mac-ip-time": (
        "mac: 00:11:22:33:44:55\nmac2: 0a:1b:2c:3d:4e:5f\nip: 10.0.0.1\nip6: ::1\nip6b: fe80::1\ntime: 12:30\ntime2: 12:30:00\ntime3: 1:2\ntime4: 00:00\ntime5: 59:59\ntime6: 60:60\nport: 0:80\nratio: 16:9\nver: 1:1.2-3\n",
        [
            "mac",
            "mac2",
            "ip",
            "ip6",
            "ip6b",
            "time",
            "time2",
            "time3",
            "time4",
            "time5",
            "time6",
            "port",
            "ratio",
            "ver",
        ],
    ),
    "norway": (
        "country: NO\nanswer: y\nflag: n\nswitch: on\nmode: off\nok: Yes\nlist: [yes, no, on, off, y, n, true, false, ~, null]\nmap: {yes: 1, no: 2, on: 3, off: 4, y: 5, n: 6}\n",
        ["country", "answer", "flag", "switch", "mode", "ok", "list", "map"],
    ),
}


#: Plain scalars that make a whole file unparseable (PyYAML's BaseLoader refuses
#: ``k: V``, ``- V`` or ``m: V`` with them), in the order the groups hold them.
#: They are tested one per file instead, see yaml_groups.
UNPARSEABLE = [
    "-",
    "1:",
    "a\tb",
    "- x",
    "? x",
    "[a, b",
    "{a",
    "-",
    "x: y",
    "x:",
    ":",
    "::",
    "%",
]


def _group(name, values, desc):
    """One file holding every value under its own key, plus the same values
    as list items and map values. A value that cannot be parsed structurally would
    take the whole file down, so those in UNPARSEABLE are left out here."""
    s = Scn("yaml-" + name, "yaml-typing", desc)
    s.simple()
    body = ""
    keys = []
    good = [v for v in values if v not in UNPARSEABLE]
    for i, v in enumerate(good):
        k = "k%02d" % i
        keys.append(k)
        body += "{}: {}\n".format(k, v)
    body += "as_list:\n" + "".join("  - {}\n".format(v) for v in good)
    body += "as_map:\n" + "".join("  m%02d: %s\n" % (i, v) for i, v in enumerate(good))
    s.file("data/common.yaml", body)
    s.qs(keys)
    s.q("as_list")
    s.q("as_map")
    return s


@scenario
def yaml_groups(ctx):
    yield _group("bools", BOOLS, "boolean-like plain scalars")
    yield _group("nulls", NULLS, "null-like plain scalars")
    for i in range(0, len(INTS), 26):
        yield _group(
            "ints-%d" % (i // 26), INTS[i : i + 26], "integer-like plain scalars"
        )
    for i in range(0, len(FLOATS), 26):
        yield _group(
            "floats-%d" % (i // 26), FLOATS[i : i + 26], "float-like plain scalars"
        )
    for i in range(0, len(STRINGS), 26):
        yield _group(
            "strings-%d" % (i // 26), STRINGS[i : i + 26], "strings and quoting"
        )
    yield from _micro("yamlrej", "yaml-risky", UNPARSEABLE)


def _micro(prefix, family, values):
    for i, v in enumerate(values):
        s = Scn("%s-%02d" % (prefix, i), family, "k: " + v)
        s.simple()
        s.file("data/common.yaml", "k: {}\nother: 1\n".format(v))
        s.q("k")
        s.q("other")
        s.q("k", default="dflt")
        yield s


@scenario
def yaml_micro(ctx):
    # one value per file: a value that fails to load takes the whole file with it
    yield from _micro("yamlsym", "yaml-symbols", SYMBOLS)
    yield from _micro("yamldate", "yaml-dates", DATES)
    yield from _micro("yamltag", "yaml-tags", TAGS)
    # values that make a whole group file fail to load are isolated here as well
    risky = [
        "@at",
        "`bt",
        "*a",
        "&a",
        "&a x",
        "!",
        "!!",
        "|",
        ">",
        "[",
        "{",
        "]",
        "}",
        ",",
        "?",
        "- ",
        "? ",
        "%",
        "'",
        '"',
        "a: b",
        '"\\q"',
        '"\\x4"',
        '"\\u12"',
        "1e400",
        "-1e400",
        "1.0e400",
        "0x1p3",
        "1e-400",
        '"\\ud83d\\ude00"',
        '"\\ud83d"',
        "#",
        "' #",
        "<<",
        "=",
        "*",
        "&",
        "!!str",
        '"\\U00110000"',
        '"\\xff"',
        "? a",
        "- a",
        "-\ta",
        "[a]: b",
        "{a: b}: c",
        "''''",
        "'''",
        '"""',
    ]
    yield from _micro("yamlrisky", "yaml-risky", risky)


@scenario
def yaml_struct(ctx):
    for name, (content, keys) in STRUCT.items():
        s = Scn("yamlstruct-" + name, "yaml-struct", "file shape: " + name)
        s.hiera(d("""\
                version: 5
                defaults:
                  datadir: data
                  data_hash: yaml_data
                hierarchy:
                  - name: subject
                    path: subject.yaml
                  - name: fallback
                    path: fallback.yaml
                """))
        s.file("data/subject.yaml", content)
        s.file("data/fallback.yaml", "fb: fallback\nk: fallback_k\n")
        s.qs(keys)
        s.q("fb")
        s.q("k", merge="unique")
        yield s


@scenario
def yaml_facts_typing(ctx):
    """The --facts file goes through a different loader than data files."""
    facts = d("""\
        a: alpha
        yes_f: yes
        no_f: no
        on_f: on
        y_f: y
        null_f: ~
        oct_f: 010
        oct2_f: 0o10
        hex_f: 0x1F
        bin_f: 0b11
        us_f: 1_000
        sex_f: 1:30
        fl_f: 1.0
        fl2_f: 1e3
        fl3_f: .5
        big_f: 123456789012345678901234567890
        e16_f: 1.0e+16
        e20_f: 1.0e+20
        small_f: 0.00001
        small2_f: 0.0001
        neg0_f: -0.0
        lead0_f: 0123456789
        ver_f: 1.10
        colon_f: a:b
        list_f: [1, 2.0, true, ~, x]
        map_f: {i: 1, f: 2.5, t: true, n: ~, s: x}
        str_num_f: "42"
        mac_f: 00:11:22:33:44:55
        """)
    names = [l.split(":")[0] for l in facts.splitlines()]
    s = Scn("yaml-facts-typing", "yaml-facts", "typed facts interpolated into values")
    s.facts(facts)
    s.simple()
    body = ""
    for n in names:
        body += 'i_{0}: "%{{facts.{0}}}"\n'.format(n)
        s.q("i_" + n)
    body += 'whole_list: "x%{facts.list_f}"\nwhole_map: "x%{facts.map_f}"\n'
    s.q("whole_list")
    s.q("whole_map")
    s.file("data/common.yaml", body)
    yield s

    for name, facts in {
        "sym": "a: :sym\nb: 1\n",
        "date": "a: 2001-12-14\nb: 1\n",
        "time": "a: 2001-12-14 21:59:43 +00:00\nb: 1\n",
        "tag": "a: !ruby/object:Foo {}\nb: 1\n",
        "inf": "a: .inf\nb: 1\n",
        "nan": "a: .nan\nb: 1\n",
        "intkey": "1: one\na: x\n",
        "symkey": ":a: x\nb: 1\n",
        "list": "- a\n- b\n",
        "empty": "",
        "emptymap": "{}\n",
        "null": "~\n",
        "anchor": "base: &b {x: 1}\na: *b\nb: 1\n",
        "merge": "base: &b {x: 1}\na:\n  <<: *b\n  y: 2\nb: 1\n",
        "dup": "a: 1\na: 2\nb: 1\n",
        "bom": b"\xef\xbb\xbfa: bomval\nb: 1\n",
        "crlf": b"a: crlfval\r\nb: 1\r\n",
        "nested-nil": "a: {x: ~, y: [~]}\nb: 1\n",
        "trusted-like": "trusted: {certname: fake}\na: x\nb: 1\n",
        "facts-like": "facts: {a: inner}\na: outer\nb: 1\n",
        "env-like": "environment: staging\na: x\nb: 1\n",
        "server-facts-like": "server_facts: {serverversion: fake}\nserverversion: f2\na: x\nb: 1\n",
        "clientcert-partial": "clientcert: foo\na: x\nb: 1\n",
        "identity-all": "clientcert: cc\nhostname: hh\ndomain: dd.example\nfqdn: hh.dd.example\na: x\nb: 1\n",
        "dotted-name": "a.b: dotted\na: {b: nested}\nb: 1\n",
        "colon-name": "x::y: ns\na: x\nb: 1\n",
        "upper-name": "A: upper\na: lower\nb: 1\n",
        "underscore-name": "_a: under\na: x\nb: 1\n",
        "numeric-name": "'0': zero\n'1': one\na: x\nb: 1\n",
        "space-name": "a b: spaced\na: x\nb: 1\n",
        "name-title": "name: nm\ntitle: tt\nmodule_name: mm\na: x\nb: 1\n",
    }.items():
        s = Scn("yaml-facts-" + name, "yaml-facts", "facts file shape: " + name)
        s.facts(facts)
        s.simple()
        s.file(
            "data/common.yaml",
            d("""\
                plain: p
                ia: "[%{facts.a}]"
                itop: "[%{::a}]"
                ib: "[%{facts.b}]"
                iax: "[%{facts.a.x}]"
                itrusted: "[%{trusted.certname}]"
                itrusted_all: "[%{trusted}]"
                ifacts_a: "[%{facts.facts.a}]"
                ienv: "[%{environment}]"
                ienv_f: "[%{facts.environment}]"
                isv: "[%{server_facts.serverversion}]"
                isv2: "[%{serverversion}]"
                icc: "[%{clientcert}|%{facts.clientcert}|%{fqdn}]"
                idotted: "[%{a.b}|%{facts.a.b}|%{facts.'a.b'}]"
                ins: "[%{x::y}|%{facts.x::y}]"
                iup: "[%{A}|%{facts.A}]"
                iund: "[%{_a}|%{facts._a}]"
                inum: "[%{0}|%{facts.0}|%{facts.1}]"
                isp: "[%{a b}|%{facts.a b}]"
                int: "[%{name}|%{title}|%{module_name}|%{facts.name}]"
                """),
        )
        s.qs(
            [
                "plain",
                "ia",
                "itop",
                "ib",
                "iax",
                "itrusted_all",
                "ifacts_a",
                "ienv",
                "ienv_f",
                "isv",
                "isv2",
                "icc",
                "idotted",
                "ins",
                "iup",
                "iund",
                "inum",
                "isp",
                "int",
            ]
        )
        yield s
