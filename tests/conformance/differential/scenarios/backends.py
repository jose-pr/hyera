"""json_data and hocon_data: file shapes and value typing; output rendering."""

from __future__ import annotations

from ..scenario import Scn, d, scenario

JSON_FILES = {
    "basic": (
        '{"k": "v", "i": 1, "f": 1.5, "t": true, "n": null, "a": [1, "x", null], "h": {"a": {"b": 1}}}',
        ["k", "i", "f", "t", "n", "a", "h", "h.a.b"],
    ),
    "floats": (
        '{"f1": 1.0, "f2": 1e3, "f3": 1E3, "f4": 1.5e-7, "f5": 100000000000000000000.0, "f6": 1e16, "f7": 1e15, "f8": 0.0001, "f9": 0.00001, "f10": -0.0, "f11": 1.7976931348623157e308, "f12": 5e-324, "f13": 1e400, "f14": 123456789.123456789, "f15": 2.50, "f16": 1e0, "f17": 0.1, "f18": 1e-7, "f19": 12345678901234567.0, "f20": 0.30000000000000004}',
        ["f%d" % i for i in range(1, 21)],
    ),
    "ints": (
        '{"i1": 0, "i2": -0, "i3": 9223372036854775807, "i4": 9223372036854775808, "i5": 123456789012345678901234567890, "i6": -1, "i7": 1E2, "i8": 10e-1}',
        ["i%d" % i for i in range(1, 9)],
    ),
    "strings": (
        '{"s1": "\\u00e9", "s2": "\\ud83d\\ude00", "s3": "a\\nb", "s4": "\\/", "s5": "\\u0000", "s6": "tab\\t", "s7": "\\"q\\"", "s8": "\\\\", "s9": "%{facts.a}", "s10": "\\u2028", "s11": "é", "s12": "\\u007f", "s13": "\\u001b[0m", "s14": "<>&\'", "s15": "\\ud83d"}',
        ["s%d" % i for i in range(1, 16)],
    ),
    "dup-keys": ('{"k": "first", "k": "second", "h": {"a": 1, "a": 2}}', ["k", "h"]),
    "nan": ('{"k": NaN, "o": 1}', ["k", "o"]),
    "infinity": ('{"k": Infinity, "o": 1}', ["k", "o"]),
    "neg-infinity": ('{"k": -Infinity, "o": 1}', ["k", "o"]),
    "trailing-comma": ('{"k": "v",}', ["k"]),
    "trailing-comma-arr": ('{"k": [1, 2,]}', ["k"]),
    "comments": ('{"k": "v" /* c */, "o": 1 // c\n}', ["k", "o"]),
    "comment-hash": ('# c\n{"k": "v"}', ["k"]),
    "single-quotes": ("{'k': 'v'}", ["k"]),
    "unquoted-key": ("{k: 1}", ["k"]),
    "top-array": ("[1, 2]", ["k", "0"]),
    "top-string": ('"str"', ["k"]),
    "top-null": ("null", ["k"]),
    "top-number": ("42", ["k"]),
    "top-true": ("true", ["k"]),
    "empty": ("", ["k"]),
    "whitespace": ("  \n", ["k"]),
    "empty-obj": ("{}", ["k"]),
    "bom": (b'\xef\xbb\xbf{"k": "v"}', ["k"]),
    "utf16": ('{"k": "v"}'.encode("utf-16"), ["k"]),
    "latin1": (b'{"k": "caf\xe9"}', ["k"]),
    "trailing-junk": ('{"k": "v"} junk', ["k"]),
    "two-docs": ('{"k": "v"}{"k": "w"}', ["k"]),
    "two-docs-nl": ('{"k": "v"}\n{"k": "w"}\n', ["k"]),
    "leading-zero": ('{"k": 010}', ["k"]),
    "plus-number": ('{"k": +1}', ["k"]),
    "hex-number": ('{"k": 0x10}', ["k"]),
    "dot-number": ('{"k": .5}', ["k"]),
    "trailing-dot-number": ('{"k": 5.}', ["k"]),
    "control-in-string": ('{"k": "a\tb"}', ["k"]),
    "newline-in-string": ('{"k": "a\nb"}', ["k"]),
    "bad-escape": ('{"k": "\\q"}', ["k"]),
    "bad-unicode-escape": ('{"k": "\\u12"}', ["k"]),
    "deep-nesting": ('{"k": ' + "[" * 120 + "]" * 120 + "}", ["k"]),
    "non-string-key-like": (
        '{"1": "one", "true": "t", "null": "n", "": "empty", " ": "sp"}',
        ["1", "true", "null", "", " "],
    ),
    "lookup-options": (
        '{"k": [1], "lookup_options": {"k": {"merge": "unique"}}}',
        ["k", "lookup_options"],
    ),
    "lookup-options-bad": ('{"k": [1], "lookup_options": "x"}', ["k"]),
    "interp-key": ('{"%{facts.a}": "v", "h": {"%{facts.role}": 1}}', ["alpha", "h"]),
    "crlf": (b'{\r\n"k": "v"\r\n}\r\n', ["k"]),
    "big-exp": ('{"k": 1e999999, "o": 1}', ["k", "o"]),
    "neg-exp-big": ('{"k": 1e-999999, "o": 1}', ["k", "o"]),
    "true-caps": ('{"k": True}', ["k"]),
    "null-caps": ('{"k": NULL}', ["k"]),
    "undefined": ('{"k": undefined}', ["k"]),
    "json-class": ('{"k": {"json_class": "String", "raw": [97]}, "o": 1}', ["k", "o"]),
    "json-class-top": ('{"json_class": "Foo", "k": "v"}', ["k"]),
    "unterminated": ('{"k": "v"', ["k"]),
    "nul-in-string": (b'{"k": "a\x00b"}', ["k"]),
}

HOCON_FILES = {
    "basic": (
        "k = v\ni = 1\nf = 1.5\nt = true\nn = null\na = [1, x, null]\nh { a { b = 1 } }\n",
        ["k", "i", "f", "t", "n", "a", "h", "h.a.b"],
    ),
    "json-style": (
        '{"k": "v", "i": 1, "a": [1, 2], "h": {"a": 1}}',
        ["k", "i", "a", "h"],
    ),
    "colon-sep": ("k: v\nh: { a: 1, b: 2 }\n", ["k", "h"]),
    "path-keys": (
        'a.b.c = 1\na.b.d = 2\na.e = 3\n"q.r" = 4\n',
        ["a", "a.b", "a.b.c", "q.r", "'q.r'"],
    ),
    "path-keys-ns": (
        'mod::k = 1\n"mod::q" = 2\nmod.x = 3\n',
        ["mod::k", "mod::q", "mod", "mod.x"],
    ),
    "dup-merge": (
        "h { a = 1 }\nh { b = 2 }\nk = first\nk = second\nl = [1]\nl = [2]\n",
        ["h", "k", "l"],
    ),
    "dup-override-obj": ("h { a = 1 }\nh = 5\nh2 = 5\nh2 { a = 1 }\n", ["h", "h2"]),
    "subst": (
        'x = base\nk = ${x}\nj = "pre-"${x}"-post"\nh { a = ${x} }\nopt = ${?nope}\nl = [${x}, lit]\n',
        ["k", "j", "h", "opt", "l", "x"],
    ),
    "subst-missing": ("k = ${nope}\no = 1\n", ["k", "o"]),
    "subst-self": (
        'p = [a]\np = ${p} [b]\npath = /a\npath = ${path}":/b"\n',
        ["p", "path"],
    ),
    "subst-cycle": ("a = ${b}\nb = ${a}\no = 1\n", ["o", "a"]),
    "subst-env": ("k = ${HOME}\nj = ${?HF_NOPE}\no = 1\n", ["o", "j"]),
    "subst-path": ("h { a { b = deep } }\nk = ${h.a.b}\nj = ${h.a}\n", ["k", "j"]),
    "subst-in-quoted": ('x = 1\nk = "${x}"\n', ["k"]),
    "plus-equals": ("l = [1]\nl += 2\nm += 1\n", ["l", "m"]),
    "concat-str": (
        'k = a b  c\nj = a "b" c\ni = 1 2\ng = true false\nf = foo.bar\n',
        ["k", "j", "i", "g", "f"],
    ),
    "concat-arr": ("l = [1] [2]\nm = [1, 2] [3]\n", ["l", "m"]),
    "concat-obj": ("h = { a = 1 } { b = 2 }\n", ["h"]),
    "numbers": (
        "i1 = 010\ni2 = 0x10\ni3 = 1e3\ni4 = 1.0\ni5 = -1\ni6 = +1\ni7 = .5\ni8 = 5.\ni9 = 123456789012345678901234567890\ni10 = 1_000\ni11 = 1.5e-7\ni12 = 100000000000000000000.0\ni13 = -0.0\ni14 = 00\ni15 = 1E3\ni16 = 0.10\n",
        ["i%d" % i for i in range(1, 17)],
    ),
    "bool-null-like": (
        'b1 = yes\nb2 = no\nb3 = on\nb4 = off\nb5 = True\nb6 = TRUE\nb7 = Null\nb8 = "true"\nb9 = truex\nb10 = nullx\nb11 = true x\n',
        ["b%d" % i for i in range(1, 12)],
    ),
    "durations": (
        "d1 = 10 seconds\nd2 = 5s\nd3 = 1 minute\nd4 = 10ms\nd5 = 2 days\nd6 = 1h\nd7 = 3 weeks\nd8 = 1 year\nd9 = 500 nanoseconds\nd10 = 10 s\nd11 = 1d\nd12 = 5 m\nd13 = 2 hours\nd14 = 1.5 seconds\nd15 = 10 microseconds\nd16 = 3 months\n",
        ["d%d" % i for i in range(1, 17)],
    ),
    "sizes": (
        "s1 = 10 kB\ns2 = 512K\ns3 = 1 MiB\ns4 = 10 bytes\ns5 = 1g\ns6 = 100%\ns7 = 10B\n",
        ["s%d" % i for i in range(1, 8)],
    ),
    "unquoted-special": (
        "k1 = a/b\nk2 = a:b\nk3 = http://x.y/z\nk4 = a-b_c\nk5 = /path/to\nk6 = 1.2.3\nk7 = a.b\nk8 = foo@bar\nk9 = a*b\nk10 = c:\\path\nk11 = a;b\nk12 = ~user\nk13 = -dash\nk14 = a|b\nk15 = 10.0.0.1\nk16 = a=b\n",
        ["k%d" % i for i in range(1, 17)],
    ),
    "triple-quote": (
        'k = """multi\nline "quoted" \\n raw"""\nj = """x"""" \n',
        ["k", "j"],
    ),
    "escapes": ('k = "a\\nb\\tc\\u00e9\\\\"\nj = "\\/"\ni = "\\q"\n', ["k", "j"]),
    "comments": (
        '# c\n// c2\nk = v # c\nj = w // c\nu = "a # b"\nv = a#b\n',
        ["k", "j", "u", "v"],
    ),
    "no-commas": ("a = [1\n2\n3]\nh {\n a = 1\n b = 2\n}\n", ["a", "h"]),
    "trailing-commas": ("a = [1, 2,]\nh { a = 1, b = 2, }\n", ["a", "h"]),
    "double-comma": ("a = [1,, 2]\n", ["a"]),
    "leading-comma": ("a = [, 1]\n", ["a"]),
    "root-braces-missing-close": ("{ k = v\n", ["k"]),
    "empty": ("", ["k"]),
    "only-comment": ("# nothing\n", ["k"]),
    "top-array": ("[1, 2]", ["k"]),
    "top-scalar": ("justastring", ["k"]),
    "empty-obj": ("{}", ["k"]),
    "null-value": ("k = null\nh { a = null }\nl = [null]\n", ["k", "h", "l"]),
    "empty-values": ('k = ""\nl = []\nh {}\n', ["k", "l", "h"]),
    "no-value": ("k =\no = 1\n", ["k", "o"]),
    "no-sep-obj": ("h { a = 1 }\nk { }\nj{a=1}\n", ["h", "k", "j"]),
    "include-plain": ('include "inc.conf"\nk = v\n', ["k", "inc"]),
    "include-file": ('include file("data/inc.conf")\nk = v\n', ["k", "inc"]),
    "include-file-rel": ('include file("inc.conf")\nk = v\n', ["k", "inc"]),
    "include-missing": ('include file("nope.conf")\nk = v\n', ["k"]),
    "include-nested-obj": ('h { include file("data/inc.conf") }\nk = v\n', ["k", "h"]),
    "include-as-key": ("include = 5\nk = v\n", ["k", "include"]),
    "include-required": (
        'include required(file("data/inc.conf"))\nk = v\n',
        ["k", "inc"],
    ),
    "bom": (b"\xef\xbb\xbfk = v\n", ["k"]),
    "crlf": (b"k = v\r\nh {\r\n a = 1\r\n}\r\n", ["k", "h"]),
    "unicode": ('k = café\nj = "日本"\nclé = v\n'.encode("utf-8"), ["k", "j", "clé"]),
    "latin1": (b"k = caf\xe9\n", ["k"]),
    "interp": (
        'k = "%{facts.a}"\nj = %{facts.a}\nh { "%{facts.role}" = 1 }\ni = "%{lookup(\'k\')}"\n',
        ["k", "j", "h", "i"],
    ),
    "lookup-options": (
        "k = [c]\nlookup_options { k { merge = unique } }\n",
        ["k", "lookup_options"],
    ),
    "lookup-options-path": (
        "k = [c]\nlookup_options.k.merge = unique\n",
        ["k", "lookup_options"],
    ),
    "key-quoted-forms": (
        '"a b" = 1\n"" = 2\na b = 3\n"x"."y" = 4\n"p.q".r = 5\n',
        ["a b", "", "x", "p.q", "a"],
    ),
    "key-numeric": (
        "1 = one\n2.5 = twofive\n10 { a = 1 }\ntrue = t\nnull = n\n",
        ["1", "2", "2.5", "10", "true", "null"],
    ),
    "key-dash-dollar": ("a-b = 1\na_b = 2\n$x = 3\n", ["a-b", "a_b", "$x"]),
    "array-of-obj": ("l = [{a = 1}, {b = 2}]\nm = [{a = 1} {b = 2}]\n", ["l", "m"]),
    "nested-arrays": ("l = [[1, 2], [3], []]\n", ["l"]),
    "multiline-array-concat": ("l = [1, 2\n  3 4]\n", ["l"]),
    "whitespace-forms": ("k=v\n  j   =    w   \nh{a=1,b=2}\n", ["k", "j", "h"]),
    "equals-in-obj-key": ("h = { a : 1, b = 2, c { d = 3 } }\n", ["h"]),
    "unterminated-string": ('k = "abc\n', ["k"]),
    "bad-token": ("k = }\n", ["k"]),
    "reserved-chars": ("k = a$b\no = 1\n", ["k", "o"]),
    "question-key": ("k? = 1\no = 1\n", ["o"]),
    "backtick": ("k = `x`\no = 1\n", ["k", "o"]),
    "at-sign": ("k = @x\no = 1\n", ["k", "o"]),
    "ampersand": ("k = a&b\no = 1\n", ["k", "o"]),
    "caret": ("k = a^b\no = 1\n", ["k", "o"]),
    "plus": ("k = a+b\no = 1\n", ["k", "o"]),
    "bang": ("k = a!b\no = 1\n", ["k", "o"]),
    "properties-like": ("a.b=1\na.c=2\nx.y.z=deep\n", ["a", "x"]),
    "obj-then-path": ("a { b = 1 }\na.c = 2\na.b = 9\n", ["a"]),
    "null-then-obj": ("a = null\na { b = 1 }\n", ["a"]),
    "obj-null-obj": ("a { b = 1 }\na = null\na { c = 2 }\n", ["a"]),
    "subst-merge-obj": ("base { a = 1, b = 2 }\nk = ${base} { b = 3, c = 4 }\n", ["k"]),
    "subst-optional-in-concat": (
        "k = pre ${?nope} post\nl = [1, ${?nope}, 2]\nh { a = ${?nope} }\n",
        ["k", "l", "h"],
    ),
    "subst-delayed": ("k = ${later}\nlater = 5\n", ["k"]),
    "subst-self-missing": ("k = ${k}\no = 1\n", ["o", "k"]),
    "subst-quoted-path": ('"a.b" = 1\nk = ${"a.b"}\n', ["k"]),
    "subst-number-concat": (
        "n = 5\nk = ${n}px\nj = ${n} ${n}\ni = ${n}\n",
        ["k", "j", "i"],
    ),
}

RENDER_VALUES = d("""\
    r_str: hello
    r_empty: ""
    r_space: " lead and trail "
    r_multiline: "line1\\nline2\\n"
    r_multiline2: "line1\\n\\nline3"
    r_tab: "a\\tb"
    r_colon: "a: b"
    r_hash_c: "a #b"
    r_quote: "it's \\"q\\""
    r_bslash: "c:\\\\path"
    r_unicode: "café 日本 😀"
    r_ctrl: "a\\u0001b\\u007f"
    r_ls: "a\\u2028b"
    r_nel: "a\\u0085b"
    r_bom: "\\uFEFFx"
    r_numstr: "42"
    r_fltstr: "1.5"
    r_boolstr: "true"
    r_nullstr: "null"
    r_tilde: "~"
    r_yes: "yes"
    r_no: "No"
    r_on: "on"
    r_y: "y"
    r_octstr: "010"
    r_hexstr: "0x1f"
    r_sexa: "1:30"
    r_date: "2001-12-14"
    r_time: "2001-12-14 21:59:43"
    r_symstr: ":sym"
    r_dash: "- x"
    r_dashonly: "-"
    r_qmark: "? x"
    r_bang: "!tag"
    r_amp: "&a"
    r_star: "*a"
    r_pipe: "|"
    r_gt: ">"
    r_pct: "%x"
    r_at: "@x"
    r_btick: "`x"
    r_brace: "{a}"
    r_bracket: "[a]"
    r_comma: "a, b"
    r_eq: "="
    r_merge: "<<"
    r_dots: "..."
    r_dashes: "---"
    r_e: "1e3"
    r_inf: ".inf"
    r_nan: ".NaN"
    r_under: "1_000"
    r_comma_num: "1,000"
    r_trail_colon: "a:"
    r_lead_colon: ":a"
    r_long: "{long}"
    r_int: 42
    r_neg: -1
    r_zero: 0
    r_big: 123456789012345678901234567890
    r_flt: 1.5
    r_flt_int: 2.0
    r_flt_e16: 1.0e+16
    r_flt_e20: 1.0e+20
    r_flt_small: 0.00001
    r_flt_small2: 0.0001
    r_flt_neg0: -0.0
    r_flt_third: 0.1
    r_flt_long: 0.30000000000000004
    r_flt_max: 1.7976931348623157e+308
    r_flt_min: 5.0e-324
    r_flt_e15: 1.0e+15
    r_flt_123: 123456789.12345679
    r_true: true
    r_false: false
    r_nil: ~
    r_arr: [1, two, 3.0, true, ~]
    r_arr_empty: []
    r_arr_nested: [[1, 2], [], [[3]]]
    r_arr_hash: [{a: 1}, {b: [2]}, {}]
    r_arr_str: ["", " ", "a: b", "- x", "yes", "42", "multi\\nline"]
    r_hash: {b: 1, a: two, c: ~}
    r_hash_empty: {}
    r_hash_nested: {z: {y: {x: [1, {w: v}]}}, a: []}
    r_hash_keys: {"": e, " ": s, "a b": sp, "a: b": c, "42": n, "yes": y, "~": t, "true": b, "- x": d, "é": u, "multi\\nline": m, "#c": h, "'q'": q, "1.5": f, ":s": sym, "null": nul}
    r_hash_order: {zeta: 1, alpha: 2, Mid: 3, "10": 4, "9": 5}
    r_sens: secret
    r_sens_h: {user: u, pass: p}
    r_deep: {a: {b: {c: {d: {e: {f: [1, [2, [3, [4]]]]}}}}}}
    lookup_options:
      r_sens: {convert_to: Sensitive}
      r_sens_h: {convert_to: Sensitive}
    """).replace("{long}", "word " * 40)


@scenario
def backends(ctx):
    for name, (content, keys) in JSON_FILES.items():
        s = Scn("json-" + name, "json", "json_data file: " + name)
        # the answer depends on how deep the interpreter's stack already is
        s.volatile = name == "deep-nesting"
        s.hiera(
            "version: 5\ndefaults:\n  datadir: data\n  data_hash: json_data\nhierarchy:\n  - name: subject\n    path: subject.json\n  - name: fallback\n    path: fallback.json\n"
        )
        s.file("data/subject.json", content)
        s.file("data/fallback.json", '{"fb": "fallback", "k": "fallback_k"}\n')
        s.qs(keys)
        s.q("fb")
        yield s
    for name, (content, keys) in HOCON_FILES.items():
        s = Scn("hocon-" + name, "hocon", "hocon_data file: " + name)
        # ${HOME} is set on some platforms and not on others
        s.volatile = name == "subst-env"
        s.hiera(
            "version: 5\ndefaults:\n  datadir: data\n  data_hash: hocon_data\nhierarchy:\n  - name: subject\n    path: subject.conf\n  - name: fallback\n    path: fallback.conf\n"
        )
        s.file("data/subject.conf", content)
        s.file("data/fallback.conf", "fb = fallback\nk = fallback_k\n")
        s.file("data/inc.conf", "inc = from_data_inc\n")
        s.file("inc.conf", "inc = from_root_inc\n")
        s.qs(keys)
        s.q("fb")
        yield s


@scenario
def rendering(ctx):
    s = Scn(
        "render-values", "render", "every value kind through --render-as json/yaml/s"
    )
    s.simple()
    s.file("data/common.yaml", RENDER_VALUES)
    import yaml

    keys = [k for k in yaml.safe_load(RENDER_VALUES) if k != "lookup_options"]
    s.qs(keys)
    s.render = ("yaml", "s")
    yield s
