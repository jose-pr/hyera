"""Interpolation sweep: one data key per expression shape.

Every case is a key ``tNNN`` whose value holds an interpolation (variable
references, ``lookup``, ``alias``, ``literal`` and ``scope`` calls, in several
positions and with malformed syntax); the lookup key is ``tNNN`` or a dotted
form of it. The cases are cut into scenarios of fifty so a run can sample them.
"""

from __future__ import annotations

import json
import re

from ..scenario import Scn, scenario

CHUNK = 50

FACTS = {
    "fa": "FA",
    "os": {"family": "RedHat", "release": {"major": "9", "minor": 3}},
    "list": ["a", "b", ["c", "d"]],
    "n": 7,
    "flt": 1.5,
    "big": 12345678901234567890123,
    "flag": True,
    "off": False,
    "dot.ted": "DT",
    "h": {"k.k": "v", "0": "szero", " sp ": "spaced", "q'q": "quote"},
    "empty": "",
    "pct": "%{fa}",
    "selfref": "%{selfref}",
    "mut1": "%{mut2}",
    "mut2": "%{mut1}",
    "uni": "é\U0001f600",
    "tricky": 'a"b\\c\n#{d}\t ',
}

SUPPORT = {
    "s": "str",
    "n": 42,
    "f": 1.5,
    "big": 12345678901234567890123,
    "t": True,
    "fl": False,
    "nil": None,
    "empty": "",
    "arr": ["a", 1, ["x", "y"], {"k": "v"}, None, True, 1.5],
    "h": {
        "a": "A",
        "b": {"c": "C", "d.e": "DE", "0": "szero"},
        "list": ["l0", "l1", {"x": "X"}],
        "nilv": None,
        "falsev": False,
    },
    "uni": "é\U0001f600",
    "tricky": ['a"b\\c\n#{d}\t ', "#$x #@y \x1b\x7f\u0085"],
    "pct": "100%",
    "chain1": "%{lookup('chain2')}",
    "chain2": "%{lookup('s')}-2",
    "rec1": "%{lookup('rec2')}",
    "rec2": "%{lookup('rec1')}",
    "self": "%{lookup('self')}",
    "al_arr": "%{alias('arr')}",
    "al_h": "%{alias('h')}",
    "al_al": "%{alias('al_arr')}",
    "lit": "%{literal('%')}{lookup('s')}",
    "viafact": "%{fa}",
    "nested_interp": {"%{fa}": ["%{lookup('s')}", {"k%{n}": "%{os.family}"}]},
    "a.b": "dotted-root",
    "sp ace": "spaced-root",
}

# Expressions that go inside %{ ... }.
VARS = [
    "fa",
    "::fa",
    "facts.fa",
    "::facts.fa",
    "facts.os.family",
    "os.family",
    "::os.family",
    "facts.os.release.major",
    "facts.os.release.minor",
    "facts.list.0",
    "facts.list.1",
    "facts.list.2",
    "facts.list.2.1",
    "facts.list.5",
    "facts.list.-1",
    "facts.list.+1",
    "facts.list.01",
    "facts.list.1.0",
    "facts.list.x",
    "facts.'dot.ted'",
    'facts."dot.ted"',
    "facts.dot.ted",
    "'dot.ted'",
    "dot.ted",
    "undefined_var",
    "facts.nope",
    "facts.os.nope.deeper",
    "facts.fa.x",
    "facts.fa.0",
    "facts.n.x",
    "facts.n.0",
    "facts.flag.x",
    " fa ",
    "\tfa\n",
    "fa.",
    ".fa",
    "fa..x",
    "'fa'",
    '"fa"',
    "'facts'.os.family",
    "facts . os . family",
    "facts.os .family",
    "0",
    "0.a",
    "1",
    "::",
    "",
    " ",
    '""',
    "''",
    '"::"',
    "'::'",
    " '' ",
    "trusted.certname",
    "trusted.authenticated",
    "trusted.nope",
    "environment",
    "::environment",
    "server_facts.environment",
    "facts",
    "os",
    "facts.list",
    "list",
    "facts.flag",
    "facts.off",
    "off",
    "facts.n",
    "n",
    "facts.flt",
    "flt",
    "big",
    "facts.big",
    "facts.h.'k.k'",
    "facts.h.0",
    "facts.h.'0'",
    "facts.h.' sp '",
    "facts.h. sp ",
    'facts.h."q\'q"',
    "facts.empty",
    "empty",
    "pct",
    "facts.pct",
    "selfref",
    "mut1",
    "uni",
    "tricky",
    "Fa",
    "FACTS.fa",
    "fa::x",
    "::::fa",
    ":fa",
    "fa:",
    "facts.os.family.",
    "facts..os",
    "facts.os.release",
    "h",
    "module_name",
    "clientcert",
    "facts.os.'family'",
    "facts.'os'.family",
    "'facts.os.family'",
    "fa\nfa",
    "fa}x",
    "%{fa",
    "fa%",
    "f a",
]
METHODS = [
    "lookup('s')",
    'lookup("s")',
    "hiera('s')",
    "lookup('h.a')",
    "lookup('h.b.c')",
    "lookup('h.b.\"d.e\"')",
    "lookup(\"h.b.'d.e'\")",
    "lookup('h.b.d.e')",
    "lookup('h.b.0')",
    "lookup(\"h.b.'0'\")",
    "lookup('h.list.0')",
    "lookup('h.list.2.x')",
    "lookup('h.list.9')",
    "lookup('h.list.-1')",
    "lookup('h.list.x')",
    "lookup('h.nilv')",
    "lookup('h.nilv.x')",
    "lookup('h.falsev')",
    "lookup('h.nope')",
    "lookup('h.a.x')",
    "lookup('arr')",
    "lookup('arr.2')",
    "lookup('arr.2.0')",
    "lookup('arr.3.k')",
    "lookup('arr.4')",
    "lookup('h')",
    "lookup('n')",
    "lookup('f')",
    "lookup('big')",
    "lookup('t')",
    "lookup('fl')",
    "lookup('nil')",
    "lookup('empty')",
    "lookup('nope')",
    "lookup('s.x')",
    "lookup('n.x')",
    "lookup('')",
    "lookup( 's' )",
    "lookup ('s')",
    " lookup('s') ",
    "lookup('s'",
    "lookup(s)",
    "lookup('s')x",
    "xlookup('s')",
    "LOOKUP('s')",
    "nosuch('s')",
    "alias('s')",
    "alias('arr')",
    "alias('nope')",
    "literal('%')",
    "literal('%{s}')",
    'literal("x")',
    "literal('')",
    "literal(' ')",
    "scope('fa')",
    "scope('facts.os.family')",
    'scope("::fa")',
    "scope('nope')",
    "scope('facts.list.0')",
    "scope('selfref')",
    "lookup('chain1')",
    "lookup('rec1')",
    "lookup('self')",
    "lookup('pct')",
    "lookup('lit')",
    "lookup('al_arr')",
    "lookup('al_h')",
    "lookup('al_al')",
    "lookup('viafact')",
    "lookup('uni')",
    "lookup('tricky')",
    "lookup('nested_interp')",
    "lookup('a.b')",
    "lookup('\"a.b\"')",
    "lookup(\"'a.b'\")",
    "lookup('sp ace')",
    "lookup(' s ')",
    "lookup('s ')",
    "lookup('::s')",
    "lookup('s\"')",
    "lookup('h.')",
    "lookup('.h')",
    "lookup('h..a')",
    "lookup('0')",
    "lookup('0.a')",
    "lookup('lookup_options')",
    "lookup('s')\nlookup('n')",
    "x\nlookup('s')",
    "lookup('s')\n",
    "hiera_array('s')",
    "hiera_hash('h')",
    "lookup('s', 'x')",
    "lookup('s')lookup('n')",
    "lookup('h.b')",
    "lookup(\"s')",
    "scope('lookup('s')')",
    "alias('h.b')",
    "alias('arr.0')",
    'literal("%{fa}")',
    "scope(\"facts.'dot.ted'\")",
    "lookup('%{fa}')",
]


def cases():
    out = []

    def add(value, key_suffix="", why=""):
        out.append({"value": value, "suffix": key_suffix, "why": why})

    for e in VARS + METHODS:
        add("%{" + e + "}", why="alone")
        add("pre-%{" + e + "}-post", why="embedded")
    for e in [
        "fa",
        "lookup('s')",
        "alias('s')",
        "literal('%')",
        "nope",
        "lookup('arr')",
        "lookup('nil')",
        "facts.list",
        "lookup('h')",
        "lookup('n')",
        "",
    ]:
        add("%{" + e + "}%{" + e + "}", why="twice")
        add("%%{" + e + "}", why="leading percent")
        add("%{" + e + "}}", why="trailing brace")
        add("%{%{" + e + "}}", why="nested open")
        add("%{" + e, why="unterminated")
        add("{" + e + "}%", why="no percent")
        add(["%{" + e + "}", "x%{" + e + "}"], why="in array")
        add({"k": "%{" + e + "}"}, why="hash value")
        add({"%{" + e + "}": "v"}, why="hash key")
        add({"k": "%{" + e + "}"}, ".k", why="dig after interp")
        add(["%{" + e + "}"], ".0", why="index after interp")
        add("%{" + e + "}", ".x", why="dig into interpolated string")
    # hash keys colliding after interpolation; non-string results as keys
    add({"%{fa}": 1, "FA": 2}, why="key collision, later wins")
    add({"FA": 1, "%{fa}": 2}, why="key collision, later wins (2)")
    add({"%{alias('arr')}": 1}, why="alias array as key")
    add({"%{alias('h')}": 1}, why="alias hash as key")
    add({"%{alias('n')}": 1, "42": 2}, why="alias int as key beside string 42")
    add({"%{alias('nil')}": 1}, why="alias nil as key")
    add({"%{alias('t')}": 1}, why="alias bool as key")
    add({"%{alias('f')}": 1}, why="alias float as key")
    add("%{alias('n')}", why="alias int alone")
    add("%{alias('nil')}", why="alias nil alone")
    add("%{alias('fl')}", why="alias false alone")
    add(" %{alias('s')}", why="alias with leading space")
    add("%{alias('s')} ", why="alias with trailing space")
    add("%{ alias('s') }", why="alias with inner spaces")
    add("%{alias('s')}\n", why="alias with trailing newline")
    add("%{literal('%')}{literal('%')}{fa}", why="literal chain")
    add("%{literal('%')}{fa}", why="literal then brace")
    add("100%", why="bare percent")
    add("%", why="percent")
    add("%{", why="open only")
    add("%{}", why="empty")
    add("%{}}", why="empty then brace")
    add("%}{", why="reversed")
    add("a%{\n}b", why="newline expr")
    add("%{fa}é%{fa}\U0001f600", why="non-BMP between")
    add("%{lookup('s')}" * 50, why="50 exprs")
    add("x" * 5000 + "%{fa}", why="long prefix")
    return out


def yj(value):
    """JSON text that is also YAML: a surrogate pair becomes one 8-digit escape."""
    bs = chr(92)

    def pair(m):
        hi, lo = int(m.group(1), 16), int(m.group(2), 16)
        return bs + "U%08X" % (0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))

    pattern = (
        re.escape(bs) + "u(d[89ab][0-9a-f]{2})" + re.escape(bs) + "u(d[c-f][0-9a-f]{2})"
    )
    return re.sub(pattern, pair, json.dumps(value))


@scenario
def interp_sweep(ctx):
    facts = "".join(yj(k) + ": " + yj(v) + "\n" for k, v in FACTS.items())
    support = "".join(yj(k) + ": " + yj(v) + "\n" for k, v in SUPPORT.items())
    every = cases()
    for start in range(0, len(every), CHUNK):
        part = every[start : start + CHUNK]
        s = Scn(
            "interp-sweep-%02d" % (start // CHUNK),
            "interp-sweep",
            "interpolation expression shapes %d to %d" % (start, start + len(part) - 1),
        )
        s.simple()
        s.facts(facts)
        lines = [support]
        for offset, case in enumerate(part):
            ident = "t%03d" % (start + offset)
            lines.append(yj(ident) + ": " + yj(case["value"]) + "\n")
            shown = case["value"] if isinstance(case["value"], str) else ""
            label = ident + " " + case["why"]
            if 0 < len(shown) <= 60 and shown.isprintable():
                label += ": " + shown
            s.q(ident + case["suffix"], id=label)
        s.file("data/common.yaml", "".join(lines))
        yield s
