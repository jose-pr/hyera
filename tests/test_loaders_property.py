"""Property tests for every text parser: any input returns a value or raises a
``HieraError``, and no raised error carries the document that was parsed.

The inputs come from a seeded stdlib loop (mutated seeds plus random strings
over each syntax's alphabet), so a failure reproduces from the printed input.
"""

import io
import logging
import random
import warnings

import pytest

from hyera import HieraError, HieraLookupError, load_facts
from hyera._lookup.navigation import split_key
from hyera._types.parser import parse_type
from hyera.backends import DotenvBackend, HOCONBackend, JSONBackend, YAMLBackend

_ALPHABETS = {
    "yaml": list("abc012 \t\n:-#&*!|>'\"%@`{}[],?~.\\")
    + [
        "---",
        "...",
        "!!str",
        "!!map",
        "!!omap",
        "!!seq",
        "!ruby/object:X",
        "!ruby/sym",
        "<<",
        "&a",
        "*a",
        ": ",
        "- ",
        "\n  ",
        "? ",
        "0x1F",
        "1_000",
        "1__0:30",
        ".inf",
        ".nan",
        "~",
        "null",
        "yes",
        "2001-01-01",
        "\u00e9",
        "\ufeff",
        "\x85",
        "\u2028",
        "\x00",
    ],
    "json": list('abc012 \t\n:,{}[]"\\-+.eE')
    + [
        "true",
        "false",
        "null",
        "NaN",
        "Infinity",
        "-Infinity",
        "\\u0041",
        "\\ud800",
        "\\ud83d\\ude00",
        "1e999",
        "-0",
        "\ufeff",
        "\u00e9",
        "\x00",
        "/*",
        "*/",
        "//",
    ],
    "hocon": list('abc012 \t\n:=,{}[]"\\-+.#/$?()')
    + [
        "include ",
        "include file(",
        "include url(",
        '"""',
        "${",
        "${?",
        "+=",
        "true",
        "null",
        "10s",
        "1.5",
        "\u00e9",
        "//",
        "a.b",
    ],
    "dotenv": list("abc012 \t\n=#'\"\\$:{}")
    + ["export ", "KEY=", "\r\n", "\\n", "\u00e9", "\ufeff", "\x00", "A=1\n"],
    "type": list("abcIS019 ,[]{}'\"/\\-.=>:?|*")
    + [
        "Integer",
        "String",
        "Array",
        "Hash",
        "Struct",
        "Tuple",
        "Variant",
        "Optional",
        "Enum",
        "Pattern",
        "Sensitive",
        "Data",
        "Float",
        "Boolean",
        "Undef",
        "default",
        "Type",
        "Regexp",
        "Callable",
        "Any",
        "Object",
        "=>",
        "::",
        "0x1",
        "1.5",
        "-1",
        "1e3",
    ],
    "key": list("abc01 .'\"-+_:\\\t\n")
    + ["::", "..", "''", '""', "0", "-1", "007", "a.b", "\u00e9", "\x00"]
    + ["7" * 5000],
}

_SEEDS = {
    "yaml": [
        "a: 1\nb: [1, 2]\n",
        "---\nk: &x {a: 1}\nj: *x\n",
        "k: |\n  text\n",
        "? a\n: b\n",
        "a: !!binary aGk=\n",
        "- 1\n- 2\n",
        "k: 'it''s'\n",
        "a: 1\na: 2\n",
        "b: &b {x: 1}\na: {<<: [*b], y: 2}\n",
        ":sym: :val\n",
        "k: 0b101\nj: 1:30\n",
        "k: !!omap [{x: 1}, {y: 2}]\n",
    ],
    "json": [
        '{"a": 1, "b": [1, 2, {"c": null}]}',
        '{"a": "\\u00e9"}',
        "[1, 2]",
        '{"a": 1.5e3}',
        '{"a": {"b": {"c": true}}}',
        '""',
        "{}",
    ],
    "hocon": [
        "a = 1\nb { c = [1, 2] }\n",
        "a.b.c = x\n",
        "a = ${b}\nb = 2\n",
        'a = """x"""\n',
        "a = 10s\n",
        "a : { b : 1 }, c = [ { d = 1 } ]",
    ],
    "dotenv": ["A=1\nB=two\n", "# c\nA='x y'\n", 'A="a\\nb"\n', "export A=1\n", "A=\n"],
    "type": [
        "Integer[1, 2]",
        "Hash[String, Array[Integer]]",
        "Struct[{'a' => Integer, Optional['b'] => String}]",
        "Variant[String, Undef]",
        "Enum['a', 'b']",
        "Pattern[/^a/]",
        "Optional[Tuple[Integer, String, 1, 3]]",
        "Float[1.0, default]",
    ],
    "key": ["a.b.c", "a.'b.c'.0", 'a."x"', "a", "mod::key.sub", "a.0.b", "a.-1"],
}


def _generate(rng, kind):
    alphabet = _ALPHABETS[kind]
    roll = rng.random()
    if roll < 0.45:
        text = rng.choice(_SEEDS[kind])
        for _ in range(rng.randint(1, 4)):
            op = rng.random()
            at = rng.randint(0, len(text))
            if op < 0.4:
                text = text[:at] + rng.choice(alphabet) + text[at:]
            elif op < 0.7 and text:
                text = text[:at] + text[min(len(text), at + rng.randint(1, 3)) :]
            elif op < 0.85 and text:
                end = min(len(text), at + rng.randint(1, 6))
                text = text[:at] + text[at:end] * rng.randint(2, 4) + text[end:]
            else:
                text = (
                    text[:at]
                    + chr(rng.choice([0, 9, 10, 0x85, 0xFEFF, 0x1F600]))
                    + text[at:]
                )
        return text
    if roll < 0.9:
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
    return "".join(chr(rng.randint(0, 0x2FF)) for _ in range(rng.randint(0, 12)))


def _split_key(text):
    return split_key(text, lambda message: HieraLookupError(message))


def _yaml_bytes(text):
    return YAMLBackend().load(io.BytesIO(text.encode("utf-8", "surrogatepass")))


def _json_bytes(text):
    encoding = "utf-16" if len(text) % 5 == 0 else "latin-1"
    return JSONBackend().load(io.BytesIO(text.encode(encoding, "replace")))


_TARGETS = [
    pytest.param("yaml", YAMLBackend().loads, 5000, id="yaml"),
    pytest.param("yaml", _yaml_bytes, 2000, id="yaml-bytes"),
    pytest.param("json", JSONBackend().loads, 5000, id="json"),
    pytest.param("json", _json_bytes, 2000, id="json-bytes"),
    pytest.param("dotenv", DotenvBackend().loads, 5000, id="dotenv"),
    pytest.param("type", parse_type, 5000, id="parse-type"),
    pytest.param("key", _split_key, 5000, id="split-key"),
    pytest.param("hocon", None, 500, id="hocon"),
]


@pytest.mark.parametrize("kind,parse,count", _TARGETS)
def test_any_input_returns_or_raises_hiera_error(kind, parse, count, request):
    if kind == "hocon":
        pytest.importorskip("pyhocon")
        parse = HOCONBackend().loads
    rng = random.Random("{}/{}".format(kind, request.node.callspec.id))
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for _ in range(count):
                text = _generate(rng, kind)
                try:
                    parse(text)
                except HieraError:
                    pass
                except Exception as error:  # noqa: BLE001
                    pytest.fail(
                        "{} raised {}: {!r}".format(
                            kind, type(error).__name__, text[:200]
                        )
                    )
    finally:
        logging.disable(logging.NOTSET)


def test_pattern_python_cannot_compile_is_not_a_valid_type_spec():
    with pytest.raises(HieraLookupError, match="not a valid type specification"):
        parse_type("Pattern[/^?a/]")


_SECRET = "HUNTER2SECRETVALUE"


def _exception_texts(error):
    """Every string reachable from ``error``: message, args, attributes, and
    the same for each chained exception."""
    seen = set()
    pending = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield str(current)
        yield repr(current)
        yield repr(current.args)
        yield repr(vars(current))
        pending.extend((current.__cause__, current.__context__))


def _facts(name, content):
    def run(tmp_path):
        path = tmp_path / name
        path.write_bytes(
            content.encode("utf-8") if isinstance(content, str) else content
        )
        return load_facts(path)

    return run


def _hiera_yaml(content):
    def run(tmp_path):
        from hyera._config.hiera_config import _read_base_config

        path = tmp_path / "hiera.yaml"
        path.write_bytes(content)
        return _read_base_config(str(path), None)

    return run


_SECRET_DOCUMENTS = [
    pytest.param(
        lambda _t: YAMLBackend().loads('k: "%s\n' % _SECRET), id="yaml-unclosed-quote"
    ),
    pytest.param(
        lambda _t: YAMLBackend().loads("a:\n  b: 1\n %s: [\n" % _SECRET),
        id="yaml-bad-indent",
    ),
    pytest.param(
        lambda _t: YAMLBackend().loads("a: *%s\n" % _SECRET), id="yaml-undefined-alias"
    ),
    pytest.param(
        lambda _t: YAMLBackend().loads("{%s: [1, 2\n" % _SECRET),
        id="yaml-unclosed-flow",
    ),
    pytest.param(
        lambda _t: YAMLBackend().loads("k: 1\n%s: [" % _SECRET + "[" * 600),
        id="yaml-too-deep",
    ),
    pytest.param(
        lambda _t: YAMLBackend().loads('a: "%s\\q"\n' % _SECRET), id="yaml-bad-escape"
    ),
    pytest.param(
        lambda _t: JSONBackend().loads('{"%s": [1, 2' % _SECRET), id="json-unterminated"
    ),
    pytest.param(
        lambda _t: JSONBackend().loads('{"%s": NaN}' % _SECRET), id="json-nan"
    ),
    pytest.param(
        lambda _t: JSONBackend().loads('{"%s": ' % _SECRET + "[" * 600),
        id="json-too-deep",
    ),
    pytest.param(
        lambda _t: JSONBackend().load(
            io.BytesIO(b'{"' + _SECRET.encode() + b'": "\xff"}')
        ),
        id="json-invalid-utf8",
    ),
    pytest.param(
        lambda _t: YAMLBackend().load(io.BytesIO(_SECRET.encode() + b": \xff\n")),
        id="yaml-invalid-utf8",
    ),
    pytest.param(
        lambda _t: DotenvBackend().loads("A=1\n%s\n" % _SECRET), id="dotenv-no-equals"
    ),
    pytest.param(_facts("f.yaml", "%s: [1\n" % _SECRET), id="facts-yaml"),
    pytest.param(
        _facts("f.yaml", _SECRET.encode() + b": \xff\n"), id="facts-yaml-invalid-utf8"
    ),
    pytest.param(_facts("f.json", '{"%s": [1' % _SECRET), id="facts-json"),
    pytest.param(
        _facts("f.json", b'{"' + _SECRET.encode() + b'": "\xff"}'),
        id="facts-json-invalid-utf8",
    ),
    pytest.param(_facts("f.json", '{"%s": NaN}' % _SECRET), id="facts-json-nan"),
    pytest.param(
        _hiera_yaml(("%s: [1\n" % _SECRET).encode()), id="hiera-yaml-unclosed-flow"
    ),
    pytest.param(
        _hiera_yaml(_SECRET.encode() + b": \xff\n"), id="hiera-yaml-invalid-utf8"
    ),
]


@pytest.mark.parametrize("run", _SECRET_DOCUMENTS)
def test_error_carries_no_part_of_the_document(run, tmp_path):
    with pytest.raises(HieraError) as excinfo:
        run(tmp_path)
    for text in _exception_texts(excinfo.value):
        assert _SECRET not in text


# ---------------------------------------------------------------------------
# The cheap nesting bound that lets ordinary YAML skip the event walk
# ---------------------------------------------------------------------------


def _measured_depth(text):
    """Deepest collection nesting of the first document, from parse events
    (None when the text is not parseable)."""
    import yaml

    depth = deepest = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                depth += 1
                deepest = max(deepest, depth)
            elif isinstance(event, (yaml.SequenceEndEvent, yaml.MappingEndEvent)):
                depth -= 1
            elif isinstance(event, yaml.DocumentEndEvent):
                break
    except yaml.YAMLError:
        return None
    return deepest


def _adversarial_documents(rng):
    n = rng.randint(1, 40)
    step = rng.choice([1, 2, 3, 4])
    yield "k: " + "[" * n + "]" * n
    yield "k: " + "{a: " * n + "1" + "}" * n
    yield "".join(" " * (step * i) + "k:\n" for i in range(n))
    yield "".join(" " * (step * i) + "- k:\n" for i in range(n))
    yield "- " * n + "x"
    yield "? " * n + "x\n" + ": " * n + "y\n"
    yield "- ? " * n + "x\n"
    yield "".join(" " * (2 * i) + "k:\n" + " " * (2 * i) + "- \n" for i in range(n))
    yield "a:\n" + "".join("- b:\n" if i % 2 else "  - c:\n" for i in range(n))
    yield "k: '" + "[" * n + "'\n# " + "[" * n + "\nj: |\n  " + "{" * n + "\n"
    yield "- - - [[[{a: [b]}]]]\n- - \n  - x\n"
    yield "k:\r\n" + "".join(" " * (2 * i + 2) + "k:\r\n" for i in range(n))
    yield "a:\r  b:\r    c: [d]\r" + "".join(
        " " * (2 * i + 2) + "e:\r" for i in range(n)
    )
    yield "\ufeffa:\n  b:\n" + "    c:\n" * n
    yield "a:\n\tb: [[c]]\n"
    yield "---\n" + "- " * n + "x\n---\n" + "- " * (n + 5) + "y\n"


def test_nesting_bound_never_undercounts():
    from hyera.backends._psych import _nesting_bound

    rng = random.Random("bound")
    checked = 0
    for _ in range(300):
        documents = list(_adversarial_documents(rng))
        documents += [_generate(rng, "yaml") for _ in range(40)]
        for text in documents:
            measured = _measured_depth(text.replace("\ufeff", " ", 1))
            if measured is not None:
                assert _nesting_bound(text) >= measured, repr(text[:200])
                checked += 1
    assert checked > 5000


def test_limit_check_agrees_with_the_bound():
    from hyera.backends._psych import _MAX_NESTING, _nesting_bound
    from hyera.backends._psych import _within_nesting_limit

    for blocks in (0, 100, 247, 248, 249, 250, 251, 400):
        for flows in (0, 1, 3, 100, 499, 500, 501):
            text = "- " * blocks + "x" + "[" * flows + "]" * flows
            assert _within_nesting_limit(text) == (
                _nesting_bound(text) <= _MAX_NESTING
            ), (blocks, flows)


def test_ordinary_block_documents_skip_the_event_walk(monkeypatch):
    from hyera.backends import _psych

    text = "".join(
        "svc{0}:\n  name: h\n  ports:\n    - 80\n    - 443\n".format(i)
        + ("  opts: [a, b]\n" if i % 10 == 0 else "")
        for i in range(2000)
    )

    def boom(*_args):
        raise AssertionError("the event walk ran")

    monkeypatch.setattr(_psych, "_LOADER", boom)
    assert _psych._scan_structure(text) is boom


def test_repeated_anchor_names_are_still_found():
    from hyera.backends._psych import _anchors_may_repeat

    assert not _anchors_may_repeat("a: &x 1\nb: *x\n")
    assert not _anchors_may_repeat("a: &x 1\nb: &y 2\n")
    assert _anchors_may_repeat("a: &x 1\nb: &x 2\n")
