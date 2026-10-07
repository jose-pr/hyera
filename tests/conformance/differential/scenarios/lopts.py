"""lookup_options: placement, patterns, precedence, bad shapes, convert_to."""

from __future__ import annotations

from ..scenario import Scn, d, jdump, scenario


@scenario
def lopts_precedence(ctx):
    s = Scn(
        "lopts-precedence",
        "lopts",
        "exact vs pattern, level precedence, --merge override",
    )
    s.simple(["top", "mid", "common"])
    s.file(
        "data/top.yaml",
        d("""\
            app::a: [t]
            app::h: {t: 1, l: [t]}
            other: [t]
            exact_wins: [t]
            both_levels: [t]
            pat_order: {t: 1, l: [t]}
            str_entry: [t]
            only_low: [t]
            dotted: {a: [t]}
            "we.ird": [t]
            CaseKey: [t]
            lookup_options:
              "^app::": {merge: unique}
              both_levels: {merge: first}
              "^pat_": {merge: {strategy: deep, merge_hash_arrays: true}}
              "we.ird": {merge: unique}
              "^casekey$": {merge: unique}
            """),
    )
    s.file(
        "data/mid.yaml",
        d("""\
            app::a: [m, t]
            app::h: {m: 1, l: [m]}
            other: [m]
            exact_wins: [m]
            both_levels: [m]
            pat_order: {m: 1, l: [m]}
            str_entry: [m]
            only_low: [m]
            dotted: {a: [m]}
            "we.ird": [m]
            weXird: [m]
            CaseKey: [m]
            lookup_options:
              "^app::h$": {merge: hash}
              exact_wins: {merge: unique}
              "^exact_": {merge: first}
              both_levels: {merge: unique}
              "^pat_order": {merge: first}
              str_entry: "not a hash"
              "^str_": {merge: unique}
            """),
    )
    s.file(
        "data/common.yaml",
        d("""\
            app::a: [c, t]
            app::h: {c: 1, l: [c]}
            app::s: scalar
            other: [c]
            exact_wins: [c]
            both_levels: [c]
            pat_order: {c: 1, l: [c]}
            str_entry: [c]
            only_low: [c]
            dotted: {a: [c]}
            "we.ird": [c]
            weXird: [c]
            CaseKey: [c]
            lookup_options:
              "^app::": {merge: deep}
              app::a: {merge: first}
              other: {merge: {strategy: unique}}
              only_low: {merge: unique}
              dotted.a: {merge: unique}
              dotted: {merge: deep}
              "^.*$": {merge: unique}
              "we.ird": {merge: first}
            """),
    )
    keys = [
        "app::a",
        "app::h",
        "app::s",
        "other",
        "exact_wins",
        "both_levels",
        "pat_order",
        "str_entry",
        "only_low",
        "dotted",
        "dotted.a",
        "we.ird",
        "'we.ird'",
        "weXird",
        "CaseKey",
        "lookup_options",
        "nope",
    ]
    for k in keys:
        s.q(k)
        s.q(k, merge="first")
        s.q(k, merge="deep")
    s.q("lookup_options", merge="hash")
    s.q("lookup_options.other")
    yield s

    # bad lookup_options shapes, one per scenario (an error takes every lookup)
    bad = {
        "value-string": "lookup_options: nope\n",
        "value-list": "lookup_options: [a, b]\n",
        "value-int": "lookup_options: 5\n",
        "value-null": "lookup_options: ~\n",
        "value-empty": "lookup_options: {}\n",
        "entry-list": "lookup_options:\n  k: [merge, unique]\n",
        "entry-null": "lookup_options:\n  k: ~\n",
        "entry-int": "lookup_options:\n  k: 1\n",
        "entry-empty": "lookup_options:\n  k: {}\n",
        "entry-string": "lookup_options:\n  k: unique\n",
        "merge-bogus": "lookup_options:\n  k: {merge: bogus}\n",
        "merge-null": "lookup_options:\n  k: {merge: ~}\n",
        "merge-int": "lookup_options:\n  k: {merge: 1}\n",
        "merge-list": "lookup_options:\n  k: {merge: [deep]}\n",
        "merge-hash-nostrategy": "lookup_options:\n  k: {merge: {knockout_prefix: '--'}}\n",
        "merge-hash-bogus-strategy": "lookup_options:\n  k: {merge: {strategy: bogus}}\n",
        "merge-hash-extra": "lookup_options:\n  k: {merge: {strategy: deep, bogus: 1}}\n",
        "merge-unique-with-opts": "lookup_options:\n  k: {merge: {strategy: unique, knockout_prefix: '--'}}\n",
        "merge-first-with-opts": "lookup_options:\n  k: {merge: {strategy: first, sort_merged_arrays: true}}\n",
        "merge-hash-with-opts": "lookup_options:\n  k: {merge: {strategy: hash, merge_hash_arrays: true}}\n",
        "merge-deep-bad-ko": "lookup_options:\n  k: {merge: {strategy: deep, knockout_prefix: 5}}\n",
        "merge-deep-bad-sort": "lookup_options:\n  k: {merge: {strategy: deep, sort_merged_arrays: 'yes'}}\n",
        "merge-deep-null-ko": "lookup_options:\n  k: {merge: {strategy: deep, knockout_prefix: ~}}\n",
        "merge-deep-empty-ko": "lookup_options:\n  k: {merge: {strategy: deep, knockout_prefix: ''}}\n",
        "merge-deep-unpack": "lookup_options:\n  k: {merge: {strategy: deep, unpack_arrays: ','}}\n",
        "merge-deep-merge-debug": "lookup_options:\n  k: {merge: {strategy: deep, merge_debug: true}}\n",
        "merge-deep-preserve": "lookup_options:\n  k: {merge: {strategy: deep, preserve_unmergeables: true}}\n",
        "merge-deep-nil-values": "lookup_options:\n  k: {merge: {strategy: deep, merge_nil_values: true}}\n",
        "merge-deep-keep-dups": "lookup_options:\n  k: {merge: {strategy: deep, keep_array_duplicates: true}}\n",
        "merge-deep-overwrite": "lookup_options:\n  k: {merge: {strategy: deep, overwrite_arrays: true}}\n",
        "merge-deep-extend": "lookup_options:\n  k: {merge: {strategy: deep, extend_existing_arrays: true}}\n",
        "merge-reverse-deep": "lookup_options:\n  k: {merge: reverse_deep}\n",
        "merge-unconstrained": "lookup_options:\n  k: {merge: unconstrained_deep}\n",
        "merge-default": "lookup_options:\n  k: {merge: default}\n",
        "merge-upper": "lookup_options:\n  k: {merge: Deep}\n",
        "merge-symbolish": "lookup_options:\n  k: {merge: ':deep'}\n",
        "strategy-reverse-deep-hash": "lookup_options:\n  k: {merge: {strategy: reverse_deep, knockout_prefix: '--'}}\n",
        "unknown-option": "lookup_options:\n  k: {bogus: 1}\n",
        "unknown-plus-merge": "lookup_options:\n  k: {merge: deep, bogus: 1}\n",
        "bad-regex": "lookup_options:\n  '^k(': {merge: deep}\n",
        "bad-regex-nomatch": "lookup_options:\n  '^zz(': {merge: deep}\n",
        "regex-ruby-only": "lookup_options:\n  '^k\\z': {merge: deep}\n",
        "regex-named-group": "lookup_options:\n  '^(?<n>k)$': {merge: deep}\n",
        "regex-posix-class": "lookup_options:\n  '^[[:alpha:]]$': {merge: deep}\n",
        "regex-possessive": "lookup_options:\n  '^k++$': {merge: deep}\n",
        "regex-atomic": "lookup_options:\n  '^(?>k)$': {merge: deep}\n",
        "regex-x-flag": "lookup_options:\n  '^(?x) k $': {merge: deep}\n",
        "regex-i-flag": "lookup_options:\n  '^(?i)K$': {merge: deep}\n",
        "regex-i-flag-mid": "lookup_options:\n  '^k(?i)$': {merge: deep}\n",
        "regex-m-flag": "lookup_options:\n  '^(?m)k.?$': {merge: deep}\n",
        "regex-dollar-newline": "lookup_options:\n  '^k$': {merge: deep}\n  '^h$': {merge: hash}\n",
        "regex-backref": "lookup_options:\n  '^(k)\\1?$': {merge: deep}\n",
        "regex-unicode-prop": "lookup_options:\n  '^\\p{Alpha}$': {merge: deep}\n",
        "regex-h-class": "lookup_options:\n  '^[\\h]?k$': {merge: deep}\n",
        "regex-caret-only": "lookup_options:\n  '^': {merge: deep}\n",
        "regex-absent": "lookup_options:\n  'k|h': {merge: deep}\n",
        "regex-lookbehind": "lookup_options:\n  '^(?<=)k$': {merge: deep}\n",
        "regex-G-anchor": "lookup_options:\n  '^\\Gk': {merge: deep}\n",
        "regex-quantified-anchor": "lookup_options:\n  '^^k': {merge: deep}\n",
        "regex-interval-open": "lookup_options:\n  '^k{,2}$': {merge: deep}\n",
        "regex-brace-literal": "lookup_options:\n  '^k{$': {merge: deep}\n",
        "regex-R": "lookup_options:\n  '^k\\R?$': {merge: deep}\n",
        "regex-K": "lookup_options:\n  '^\\Kk$': {merge: deep}\n",
        "regex-conditional": "lookup_options:\n  '^(k)(?(1)|x)$': {merge: deep}\n",
        "regex-set-intersection": "lookup_options:\n  '^[a-z&&[^a-j]]$': {merge: deep}\n",
        "regex-nested-class": "lookup_options:\n  '^[a-j[k]]$': {merge: deep}\n",
        "regex-escape-e": "lookup_options:\n  '^k\\e?$': {merge: deep}\n",
        "regex-octal": "lookup_options:\n  '^\\153$': {merge: deep}\n",
        "regex-hex-brace": "lookup_options:\n  '^\\x{6b}$': {merge: deep}\n",
        "regex-u-brace": "lookup_options:\n  '^\\u{6b}$': {merge: deep}\n",
        "regex-u4": "lookup_options:\n  '^\\u006b$': {merge: deep}\n",
        "regex-A-Z": "lookup_options:\n  '^\\Ak\\Z': {merge: deep}\n",
        "regex-comment-group": "lookup_options:\n  '^k(?#c)$': {merge: deep}\n",
        "regex-subexp-call": "lookup_options:\n  '^(?<x>k)\\g<x>?$': {merge: deep}\n",
        "regex-X": "lookup_options:\n  '^\\X$': {merge: deep}\n",
        "regex-star-star": "lookup_options:\n  '^k**$': {merge: deep}\n",
        "regex-lazy-possessive": "lookup_options:\n  '^k?+$': {merge: deep}\n",
        "interp-in-key": "lookup_options:\n  '%{facts.kname}': {merge: deep}\n",
        "interp-in-merge": 'lookup_options:\n  k: {merge: "%{facts.mergename}"}\n',
        "alias-entry": "opts: {merge: deep}\nlookup_options:\n  k: \"%{alias('opts')}\"\n",
        "alias-whole": "opts: {k: {merge: deep}}\nlookup_options: \"%{alias('opts')}\"\n",
        "key-nonstring": "lookup_options:\n  1: {merge: deep}\n  k: {merge: deep}\n",
        "convert-and-merge": "lookup_options:\n  k: {merge: deep, convert_to: Hash}\n",
        "self-options": "lookup_options:\n  lookup_options: {merge: first}\n  k: {merge: deep}\n",
    }
    for name, lo in bad.items():
        s = Scn("lopts-shape-" + name, "lopts-shapes", "lookup_options shape: " + name)
        # Python 3.9 and 3.10 cannot compile possessive quantifiers or atomic groups
        s.volatile = name in (
            "regex-possessive",
            "regex-atomic",
            "regex-lazy-possessive",
        )
        s.facts("a: alpha\nkname: k\nmergename: deep\n")
        s.simple(["top", "common"])
        s.file("data/top.yaml", "k: {a: 1, l: [t]}\nh: {a: 1}\nplain: p\n")
        s.file("data/common.yaml", "k: {b: 2, l: [c]}\nh: {b: 2}\n" + lo)
        s.q("k")
        s.q("h")
        s.q("plain")
        s.q("k", merge="first")
        s.q("lookup_options")
        s.q("nope")
        yield s


CONVERT = [
    # (value yaml, convert_to yaml)
    ("'42'", "Integer"),
    ("'0x1f'", "Integer"),
    ("'010'", "Integer"),
    ("'0b11'", "Integer"),
    ("' 42 '", "Integer"),
    ("'4_2'", "Integer"),
    ("'42.9'", "Integer"),
    ("42.9", "Integer"),
    ("-42.9", "Integer"),
    ("true", "Integer"),
    ("false", "Integer"),
    ("~", "Integer"),
    ("'abc'", "Integer"),
    ("''", "Integer"),
    ("[1]", "Integer"),
    ("{a: 1}", "Integer"),
    ("'ff'", "[Integer, 16]"),
    ("'0xff'", "[Integer, 16]"),
    ("'11'", "[Integer, 2]"),
    ("'z'", "[Integer, 36]"),
    ("'8'", "[Integer, 8]"),
    ("'11'", "[Integer, 37]"),
    ("'-5'", "[Integer, 10, true]"),
    ("-5", "[Integer, 10, true]"),
    ("'-5'", "[Integer, default, true]"),
    ("'10'", "[Integer, '10']"),
    ("'10'", "[Integer, 1]"),
    ("'+7'", "Integer"),
    ("'-0x10'", "Integer"),
    ("'1e3'", "Integer"),
    ("1.0e+3", "Integer"),
    ("'12abc'", "Integer"),
    ("'0o17'", "Integer"),
    ("'0B11'", "Integer"),
    ("'0X1F'", "Integer"),
    ("123456789012345678901234567890", "Integer"),
    ("'1 000'", "Integer"),
    ("' 0x1f'", "Integer"),
    ("'5'", "'Integer[1, 10]'"),
    ("'50'", "'Integer[1, 10]'"),
    ("5", "'Integer[10]'"),
    ("'1.5'", "Float"),
    ("'1'", "Float"),
    ("1", "Float"),
    ("'abc'", "Float"),
    ("true", "Float"),
    ("'1e3'", "Float"),
    ("'.5'", "Float"),
    ("'0x10'", "Float"),
    ("'010'", "Float"),
    ("' 1.5 '", "Float"),
    ("'1_0.5'", "Float"),
    ("~", "Float"),
    ("'inf'", "Float"),
    ("'NaN'", "Float"),
    ("'-1.5e-3'", "Float"),
    ("'5.'", "Float"),
    ("'0b11'", "Float"),
    ("[1.5]", "Float"),
    ("'1,5'", "Float"),
    ("'42'", "Numeric"),
    ("'4.2'", "Numeric"),
    ("'0x1f'", "Numeric"),
    ("'abc'", "Numeric"),
    ("true", "Numeric"),
    ("'1e2'", "Numeric"),
    ("'-7'", "Numeric"),
    ("'-7'", "[Numeric, true]"),
    ("'010'", "Numeric"),
    ("'0.10'", "Numeric"),
    ("''", "Numeric"),
    ("42", "String"),
    ("4.2", "String"),
    ("true", "String"),
    ("~", "String"),
    ("[1, a]", "String"),
    ("{a: 1}", "String"),
    ("255", "[String, '%x']"),
    ("255", "[String, '%#x']"),
    ("255", "[String, '%X']"),
    ("255", "[String, '%o']"),
    ("255", "[String, '%b']"),
    ("255", "[String, '%e']"),
    ("255", "[String, '%5d']"),
    ("255", "[String, '%-5d|']"),
    ("255", "[String, '%05d']"),
    ("255", "[String, '%+d']"),
    ("255", "[String, '%s']"),
    ("255", "[String, '%p']"),
    ("255", "[String, '%c']"),
    ("255", "[String, '%z']"),
    ("1.5", "[String, '%.3f']"),
    ("1.5", "[String, '%e']"),
    ("1.5", "[String, '%g']"),
    ("1.5", "[String, '%d']"),
    ("1.5", "[String, '%a']"),
    ("1.5", "[String, '%s']"),
    ("1.5", "[String, '%10.2f|']"),
    ("abc", "[String, '%5s|']"),
    ("abc", "[String, '%-5s|']"),
    ("abc", "[String, '%.2s']"),
    ("abc", "[String, '%p']"),
    ("abc", "[String, '%c']"),
    ("abc", "[String, '%C']"),
    ("abc", "[String, '%u']"),
    ("abc", "[String, '%U']"),
    ("'a b'", "[String, '%t']"),
    ("abc", "[String, '%d']"),
    ("'a\"b'", "[String, '%p']"),
    ('"a\'b"', "[String, '%p']"),
    ("'a\\b'", "[String, '%p']"),
    ("abc", "[String, '%#p']"),
    ("true", "[String, '%y']"),
    ("true", "[String, '%Y']"),
    ("true", "[String, '%t']"),
    ("true", "[String, '%T']"),
    ("true", "[String, '%d']"),
    ("true", "[String, '%s']"),
    ("false", "[String, '%y']"),
    ("true", "[String, '%p']"),
    ("true", "[String, '%x']"),
    ("~", "[String, '%s']"),
    ("~", "[String, '%p']"),
    ("~", "[String, '%d']"),
    ("~", "[String, '%n']"),
    ("~", "[String, '%u']"),
    ("~", "[String, '%10s|']"),
    ("[1, a, [2]]", "[String, '%p']"),
    ("[1, a, [2]]", "[String, '%s']"),
    ("[1, a, [2]]", "[String, '%a']"),
    ("[1, a, [2]]", "[String, '%#a']"),
    ("[1, a]", "[String, '% a']"),
    ("[1, a]", "[String, '%(a']"),
    ("[1, a]", "[String, '%<a']"),
    ("[1, a]", "[String, '%|a']"),
    ("[]", "[String, '%p']"),
    ("{a: 1, b: [2]}", "[String, '%p']"),
    ("{a: 1, b: [2]}", "[String, '%s']"),
    ("{a: 1}", "[String, '%h']"),
    ("{a: 1, b: {c: 2}}", "[String, '%#h']"),
    ("{a: 1}", "[String, '%<h']"),
    ("{a: 1}", "[String, '% h']"),
    ("{a: 1}", "[String, '%a']"),
    ("{}", "[String, '%p']"),
    ("{a: 1}", "[String, {Hash: '%a'}]"),
    ("255", "[String, {Integer: '%x'}]"),
    ("[255, a]", "[String, {Array: {format: '%a', string_formats: {Integer: '%x'}}}]"),
    ("[255, a]", "[String, {Array: {format: '%a', separator: ' |'}}]"),
    ("{a: 255}", "[String, {Hash: {format: '%h', separator2: ': '}}]"),
    ("255", "[String, '%x', extra]"),
    ("255", "[String, 5]"),
    ("255", "[String, '%']"),
    ("255", "[String, 'x']"),
    ("255", "[String, '%%']"),
    ("255", "[String, '%10']"),
    ("255", "[String, '%dd']"),
    ("abc", "'String[5]'"),
    ("abc", "'String[1, 2]'"),
    ("abc", "'Enum[abc]'"),
    ("abc", "'Enum[x]'"),
    ("abc", "'Pattern[/a/]'"),
    ("'true'", "Boolean"),
    ("'false'", "Boolean"),
    ("'yes'", "Boolean"),
    ("'no'", "Boolean"),
    ("'y'", "Boolean"),
    ("'n'", "Boolean"),
    ("'TRUE'", "Boolean"),
    ("'True'", "Boolean"),
    ("'Yes'", "Boolean"),
    ("'on'", "Boolean"),
    ("'off'", "Boolean"),
    ("'1'", "Boolean"),
    ("'0'", "Boolean"),
    ("1", "Boolean"),
    ("0", "Boolean"),
    ("2", "Boolean"),
    ("-1", "Boolean"),
    ("0.0", "Boolean"),
    ("0.1", "Boolean"),
    ("''", "Boolean"),
    ("'abc'", "Boolean"),
    ("~", "Boolean"),
    ("[]", "Boolean"),
    ("{}", "Boolean"),
    ("true", "Boolean"),
    ("' true'", "Boolean"),
    ("'t'", "Boolean"),
    ("'f'", "Boolean"),
    ("'true'", "'Boolean[true]'"),
    ("'false'", "'Boolean[true]'"),
    ("abc", "Array"),
    ("''", "Array"),
    ("42", "Array"),
    ("~", "Array"),
    ("true", "Array"),
    ("[1, 2]", "Array"),
    ("{a: 1, b: 2}", "Array"),
    ("{}", "Array"),
    ("abc", "[Array, true]"),
    ("abc", "[Array, false]"),
    ("[1]", "[Array, true]"),
    ("{a: 1}", "[Array, true]"),
    ("~", "[Array, true]"),
    ("abc", "[Array, 5]"),
    ("abc", "'Array[String]'"),
    ("[1, 2]", "'Array[String]'"),
    ("[1, 2]", "'Array[Integer, 3]'"),
    ("1.5", "Array"),
    ("abc", "'Array[String, 1, 2]'"),
    ("[[a, 1], [b, 2]]", "Hash"),
    ("[a, 1, b, 2]", "Hash"),
    ("[a, 1, b]", "Hash"),
    ("[]", "Hash"),
    ("{a: 1}", "Hash"),
    ("abc", "Hash"),
    ("42", "Hash"),
    ("~", "Hash"),
    ("[[a, 1], [b]]", "Hash"),
    ("[[a, 1, 2]]", "Hash"),
    ("[[a, 1], x]", "Hash"),
    ("[[[a, b], c, d]]", "[Hash, tree]"),
    ("[[[0], x], [[1, a], y]]", "[Hash, tree]"),
    ("[[[0], x], [[1, a], y]]", "[Hash, hash_tree]"),
    ("[[[a], 1]]", "[Hash, bogus]"),
    ("[[a, 1]]", "'Hash[String, Integer]'"),
    ("[[a, 1]]", "'Hash[String, String]'"),
    ("[[1, 2]]", "Hash"),
    ("[[~, 2]]", "Hash"),
    ("[a, b]", "Tuple"),
    ("abc", "Tuple"),
    ("[a, 1]", "'Tuple[String, Integer]'"),
    ("[a, b]", "'Tuple[String, Integer]'"),
    ("{a: 1}", "Tuple"),
    ("abc", "[Tuple, true]"),
    ("[[a, 1]]", "Struct"),
    ("{a: 1}", "Struct"),
    ("{a: 1}", "'Struct[{a => Integer}]'"),
    ("[[a, 1]]", "'Struct[{a => Integer}]'"),
    ("{a: x}", "'Struct[{a => Integer}]'"),
    ("{a: 1, b: 2}", "'Struct[{a => Integer}]'"),
    ("abc", "Struct"),
    ("'42'", "'Optional[Integer]'"),
    ("~", "'Optional[Integer]'"),
    ("abc", "'Optional[Integer]'"),
    ("'42'", "Optional"),
    ("~", "Optional"),
    ("'42'", "'Optional[Optional[Integer]]'"),
    ("'42'", "'NotUndef[Integer]'"),
    ("~", "'NotUndef[Integer]'"),
    ("'42'", "NotUndef"),
    ("~", "NotUndef"),
    ("abc", "Sensitive"),
    ("42", "Sensitive"),
    ("~", "Sensitive"),
    ("[a]", "Sensitive"),
    ("{a: 1}", "Sensitive"),
    ("abc", "'Sensitive[String]'"),
    ("42", "'Sensitive[String]'"),
    ("abc", "[Sensitive, extra]"),
    ("'42'", "'Variant[Integer, String]'"),
    ("abc", "'Variant[Integer, String]'"),
    ("'42'", "'Variant[String, Integer]'"),
    ("abc", "Data"),
    ("abc", "Any"),
    ("abc", "Scalar"),
    ("'42'", "Scalar"),
    ("abc", "ScalarData"),
    ("abc", "Undef"),
    ("~", "Undef"),
    ("abc", "Default"),
    ("abc", "Collection"),
    ("abc", "Iterable"),
    ("abc", "RichData"),
    ("abc", "Enum"),
    ("abc", "Pattern"),
    ("abc", "Variant"),
    ("abc", "Type"),
    ("abc", "Callable"),
    ("abc", "Runtime"),
    ("abc", "Bogus"),
    ("abc", "bogus"),
    ("abc", "''"),
    ("abc", "~"),
    ("abc", "5"),
    ("abc", "[]"),
    ("abc", "{}"),
    ("abc", "[String]"),
    ("abc", "[[String]]"),
    ("abc", "'Integer['"),
    ("abc", "'String[1'"),
    ("abc", "' String '"),
    ("abc", "'string'"),
    ("abc", "[Bogus, 1]"),
    ("abc", "[~, 1]"),
    ("abc", "[5, 1]"),
    ("'42'", "[Integer]"),
    ("'42'", "'Init[Integer]'"),
    ("'42'", "Init"),
    ("'2001-01-01'", "Timestamp"),
    ("'1.2.3'", "SemVer"),
    ("'abc'", "Regexp"),
    ("'aGVsbG8='", "Binary"),
    ("'http://x'", "URI"),
    ("'1-00:00:00'", "Timespan"),
    ("'>=1.0.0'", "SemVerRange"),
    ("'Integer'", "Type"),
    ("'x'", "Object"),
    ('"%{facts.n}"', "Integer"),
    ('"%{facts.b}"', "Boolean"),
    ("\"%{lookup('other')}\"", "Integer"),
    ("\"%{alias('otherlist')}\"", "Hash"),
    ('["%{facts.n}", 1]', "'Array[Integer]'"),
]


@scenario
def lopts_convert(ctx):
    per = 30
    for start in range(0, len(CONVERT), per):
        chunk = CONVERT[start : start + per]
        s = Scn(
            "lopts-convert-%02d" % (start // per),
            "convert",
            "convert_to on values that do and do not convert",
        )
        s.simple(["top", "common"])
        body = "other: '77'\notherlist: [[a, 1]]\n"
        lo = "lookup_options:\n"
        for i, (v, t) in enumerate(chunk):
            body += "c%02d: %s\n" % (i, v)
            lo += "  c%02d: {convert_to: %s}\n" % (i, t)
            s.q("c%02d" % i)
        import yaml

        yaml.safe_load(body + lo)  # generator self-check
        s.file("data/top.yaml", "dummy: 1\n")
        s.file("data/common.yaml", body + lo)
        yield s

    s = Scn(
        "lopts-convert-combos",
        "convert",
        "convert_to with merges, defaults, --type, patterns, levels",
    )
    s.simple(["top", "common"])
    s.file(
        "data/top.yaml",
        d("""\
            nums: ['1', '2']
            port: '8080'
            h: {a: '1'}
            pat_a: '5'
            both: '7'
            top_only_opt: '9'
            sens_h: {user: u}
            lookup_options:
              top_only_opt: {convert_to: Integer}
              both: {convert_to: Float}
            """),
    )
    s.file(
        "data/common.yaml",
        d("""\
            nums: ['2', '3']
            port: '80'
            h: {b: '2'}
            pat_a: '6'
            pat_b: 'x'
            both: '8'
            top_only_opt: '10'
            sens_h: {pass: p}
            pairs: [[a, 1], [b, 2]]
            dotted: {port: '99'}
            ref: "%{lookup('port')}"
            ref_alias: "%{alias('port')}"
            ref_sens: "x%{lookup('sens_h')}"
            ref_sens_alias: "%{alias('sens_h')}"
            lookup_options:
              nums: {merge: unique, convert_to: 'Array[Integer]'}
              port: {convert_to: Integer}
              h: {merge: hash, convert_to: Array}
              "^pat_": {convert_to: Integer}
              both: {convert_to: Integer, merge: unique}
              sens_h: {merge: deep, convert_to: Sensitive}
              pairs: {convert_to: Hash}
              dotted: {convert_to: Array}
              dotted.port: {convert_to: Integer}
              nope: {convert_to: Integer}
            """),
    )
    for k in [
        "nums",
        "port",
        "h",
        "pat_a",
        "pat_b",
        "both",
        "top_only_opt",
        "sens_h",
        "pairs",
        "dotted",
        "dotted.port",
        "ref",
        "ref_alias",
        "ref_sens",
        "ref_sens_alias",
        "nope",
        "sens_h.user",
        "pairs.a",
        "nums.0",
        "h.0",
        "port.x",
    ]:
        s.q(k)
        s.q(k, merge="first")
        s.q(k, merge="deep")
        s.q(k, default="5")
        s.q(k, type="Integer")
    s.q("port", type="String")
    s.q("sens_h", type="Sensitive")
    s.q("sens_h", type="Hash")
    s.q(["nope", "port"])
    s.q(["port", "nums"])
    yield s
