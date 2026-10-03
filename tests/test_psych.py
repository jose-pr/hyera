"""``_psych``: Psych's scalar rules, BOM handling, and structure, as
measured against real Ruby 4.0.7 / Psych 5.3.1 via a dedicated oracle probe.
Parametrized over both loader backends where the C/pure distinction
matters (only the tab-after-colon row differs)."""

import math

import pytest
import yaml

from hyera.backends import BackendError, YAMLBackend
from hyera.exceptions import ConfigError
from hyera.backends._psych import (
    RubySymbol,
    _C_LOADER,
    _PURE_LOADER,
    _yaml_problem,
    safe_load,
    symkeys_to_string,
)
from hyera._config.hiera_config import _read_base_config

_LOADERS = [
    pytest.param(
        _C_LOADER,
        id="C",
        marks=pytest.mark.skipif(_C_LOADER is None, reason="no libyaml"),
    ),
    pytest.param(_PURE_LOADER, id="pure"),
]


def _load_with(loader_cls, text):
    """Load ``text`` through a specific loader class the same way
    :func:`safe_load` does (BOM swap, first document, ``None`` -> ``False``),
    without going through the process-wide ``_LOADER`` pick."""
    if text.startswith("﻿"):
        text = " " + text[1:]
    generator = yaml.load_all(text, loader_cls)
    try:
        result = next(generator)
    except StopIteration:
        result = None
    finally:
        generator.close()
    return False if result is None else result


# ---------------------------------------------------------------------------
# Booleans and null
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize(
    "text,expected",
    [
        ("yes", True),
        ("Yes", True),
        ("YES", True),
        ("on", True),
        ("TRUE", True),
        ("tRUE", True),
        ("Off", False),
        ("y", "y"),
        ("n", "n"),
    ],
)
def test_boolean_scalars(loader, text, expected):
    assert _load_with(loader, text) is expected or _load_with(loader, text) == expected


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize("text", ["~", "null", "Null", "NULL", "nUll", ""])
def test_null_scalars(loader, text):
    assert _load_with(loader, text) is None or _load_with(loader, text) is False


# ---------------------------------------------------------------------------
# Integers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize(
    "text,expected",
    [
        ("0b101", 5),
        ("0x_1F", 31),
        ("017", 15),
        ("+12", 12),
        ("-0x1F", -31),
        ("1,000", 1000),
        ("0x1,F", 31),
    ],
)
def test_integer_scalars(loader, text, expected):
    assert _load_with(loader, "k: {}\n".format(text))["k"] == expected


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize("text", ["0o17", "08", "0b"])
def test_integer_lookalikes_stay_strings(loader, text):
    assert _load_with(loader, "k: {}\n".format(text))["k"] == text


# ---------------------------------------------------------------------------
# Floats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.", 1.0),
        (".5", 0.5),
        ("1.0e+3", 1000.0),
        ("1_0.5", 10.5),
        ("1,5.5", 15.5),
    ],
)
def test_float_scalars(loader, text, expected):
    assert _load_with(loader, "k: {}\n".format(text))["k"] == expected


@pytest.mark.parametrize("loader", _LOADERS)
def test_float_specials(loader):
    assert math.isinf(_load_with(loader, "k: .inf\n")["k"])
    assert _load_with(loader, "k: .inf\n")["k"] > 0
    assert _load_with(loader, "k: -.Inf\n")["k"] < 0
    assert math.isnan(_load_with(loader, "k: .NaN\n")["k"])


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize("text", ["1e3", "1.0e3", "685.230_15e+03", ".", "+.", "-."])
def test_float_lookalikes_stay_strings(loader, text):
    assert _load_with(loader, "k: {}\n".format(text))["k"] == text


# ---------------------------------------------------------------------------
# Sexagesimal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize(
    "text,expected",
    [
        ("1:30", 5400),
        ("1:20", 4800),
        ("-1:30", -1800),
        ("1:30.5", 5430.0),
        ("190:20:30", 685230),
        ("1_0:30", 37800),
    ],
)
def test_sexagesimal(loader, text, expected):
    assert _load_with(loader, "k: {}\n".format(text))["k"] == expected


# ---------------------------------------------------------------------------
# Dates and times: disallowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize(
    "text",
    ["2024-01-15", "!!timestamp 2024-01-15", "!foo 2024-01-15"],
)
def test_date_shaped_scalar_is_disallowed(loader, text):
    with pytest.raises(BackendError, match="unspecified class: Date"):
        _load_with(loader, "k: {}\n".format(text))


@pytest.mark.parametrize("loader", _LOADERS)
def test_datetime_shaped_scalar_is_disallowed(loader):
    with pytest.raises(BackendError, match="unspecified class: Time"):
        _load_with(loader, "k: 2024-01-15 10:00:00\n")


@pytest.mark.parametrize("loader", _LOADERS)
def test_quoted_date_stays_a_string(loader):
    assert _load_with(loader, "k: '2024-01-15'\n")["k"] == "2024-01-15"


# ---------------------------------------------------------------------------
# Symbols
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
def test_symbol_scalars(loader):
    assert _load_with(loader, "k: :foo\n")["k"] == RubySymbol("foo")
    assert _load_with(loader, 'k: :"foo bar"\n')["k"] == RubySymbol("foo bar")
    assert _load_with(loader, "k: :'q'\n")["k"] == RubySymbol("q")
    assert _load_with(loader, "k: !ruby/symbol foo\n")["k"] == RubySymbol("foo")
    assert _load_with(loader, "k: !ruby/sym foo\n")["k"] == RubySymbol("foo")
    # A double-colon prefix (Ruby's own "::Foo"-shaped symbol) strips both.
    assert _load_with(loader, "k: ::foo\n")["k"] == RubySymbol("foo")


def test_ruby_symbol_ne_and_repr():
    assert RubySymbol("a") != RubySymbol("b")
    assert not (RubySymbol("a") != RubySymbol("a"))
    assert repr(RubySymbol("a")) == ":a"


@pytest.mark.parametrize("loader", _LOADERS)
def test_quoted_symbol_looking_scalar_stays_a_string(loader):
    assert _load_with(loader, "k: ':foo'\n")["k"] == ":foo"


def test_symkeys_to_string():
    data = {RubySymbol("a"): 1, "b": [{RubySymbol("c"): 2}]}
    assert symkeys_to_string(data) == {"a": 1, "b": [{"c": 2}]}
    # A symbol *value* (not a key) is unchanged.
    assert symkeys_to_string({"a": RubySymbol("x")}) == {"a": RubySymbol("x")}


# ---------------------------------------------------------------------------
# Unknown-tag scalars: tokenized, quoted or not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize(
    "text,expected",
    [
        ("!foo bar", "bar"),
        ("!foo 12", 12),
        ("!foo '12'", 12),
        ("!<tag:example.com,2000:x> y", "y"),
    ],
)
def test_unknown_tag_scalars_are_tokenized(loader, text, expected):
    assert _load_with(loader, "k: {}\n".format(text))["k"] == expected


@pytest.mark.parametrize("loader", _LOADERS)
def test_unknown_tag_symbol_scalar(loader):
    assert _load_with(loader, "k: !foo :x\n")["k"] == RubySymbol("x")


# ---------------------------------------------------------------------------
# Core tags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
def test_core_tags(loader):
    assert _load_with(loader, "k: !!str 12\n")["k"] == "12"
    assert _load_with(loader, "k: !!int '12'\n")["k"] == 12
    assert _load_with(loader, "k: !!int abc\n")["k"] == "abc"
    assert _load_with(loader, "k: !!float '1'\n")["k"] == 1.0
    assert _load_with(loader, "k: !!bool 'yes'\n")["k"] is True
    assert _load_with(loader, "k: !!null ''\n")["k"] is None
    assert _load_with(loader, "k: !!value x\n")["k"] == "x"
    assert _load_with(loader, "k: !!merge x\n")["k"] == "x"
    assert _load_with(loader, "k: !ruby/string foo\n")["k"] == "foo"
    # A "str"-family tag on a non-scalar node (an obscure Ruby
    # ivars-on-a-String encoding with no fixture to model exactly) falls
    # back to ordinary construction by node kind instead of crashing.
    assert _load_with(loader, "k: !str\n  a: 1\n")["k"] == {"a": 1}
    assert _load_with(loader, "k: !ruby/string [1, 2]\n")["k"] == [1, 2]


@pytest.mark.parametrize("loader", _LOADERS)
def test_float_tag_invalid_value_raises(loader):
    with pytest.raises(BackendError, match=r'invalid value for Float\(\): "abc"'):
        _load_with(loader, "k: !!float abc\n")


@pytest.mark.parametrize("loader", _LOADERS)
def test_implicit_float_lookalike_invalid_value_raises(loader):
    # Distinct from the explicit !!float case above: a bare scalar that
    # the FLOAT_RE regex accepts (a sign plus a bare "." plus an exponent,
    # no digits at all) but Python's own float() still rejects after
    # cleaning -- the auto-detection path through _tokenize, not
    # _construct_float's tagged one.
    with pytest.raises(BackendError, match=r'invalid value for Float\(\): "-\.e\+1"'):
        _load_with(loader, "k: -.e+1\n")


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
def test_omap(loader):
    assert _load_with(loader, "k: !!omap\n  - x: 1\n  - y: 2\n")["k"] == {
        "x": 1,
        "y": 2,
    }


@pytest.mark.parametrize("loader", _LOADERS)
def test_pairs_is_a_list_of_hashes(loader):
    assert _load_with(loader, "k: !!pairs\n  - x: 1\n  - x: 2\n")["k"] == [
        {"x": 1},
        {"x": 2},
    ]


@pytest.mark.parametrize("loader", _LOADERS)
def test_set_is_disallowed(loader):
    with pytest.raises(BackendError, match="Psych::Set"):
        _load_with(loader, "k: !!set\n  p: null\n  q: null\n")


@pytest.mark.parametrize("loader", _LOADERS)
def test_ruby_object_is_disallowed(loader):
    with pytest.raises(BackendError, match="unspecified class: Object"):
        _load_with(loader, "k: !ruby/object:Object\n  b: 1\n")
    # The bare "!ruby/object" tag (no ":Class" suffix at all) and any
    # other bare "!ruby/<kind>" tag both name themselves.
    with pytest.raises(BackendError, match="unspecified class: Object"):
        _load_with(loader, "k: !ruby/object\n  b: 1\n")
    with pytest.raises(BackendError, match="unspecified class: Regexp"):
        _load_with(loader, "k: !ruby/regexp foo\n")


@pytest.mark.parametrize("loader", _LOADERS)
def test_python_tuple_is_a_plain_list(loader):
    assert _load_with(loader, "k: !!python/tuple\n  - 1\n  - 2\n")["k"] == [1, 2]


@pytest.mark.parametrize("loader", _LOADERS)
def test_python_object_apply_is_a_plain_list(loader):
    assert _load_with(loader, "!!python/object/apply:os.system ['echo hi']\n") == [
        "echo hi"
    ]


@pytest.mark.parametrize("loader", _LOADERS)
def test_python_object_mapping_is_a_plain_dict(loader):
    assert _load_with(loader, "!!python/object:os.x\n  b: 1\n") == {"b": 1}


# ---------------------------------------------------------------------------
# Binary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
def test_binary(loader):
    assert _load_with(loader, "k: !!binary aGVsbG8=\n")["k"] == "hello"
    assert _load_with(loader, "k: !!binary /w==\n")["k"] == "\udcff"
    assert _load_with(loader, "k: !!binary '%%%'\n")["k"] == ""
    # "%%%" above is silently stripped down to nothing by b64decode's own
    # lenient (validate=False) mode -- never raises. A single valid-
    # alphabet character with impossible padding does raise, exercising
    # the except-and-treat-as-empty fallback for real.
    assert _load_with(loader, "k: !!binary a\n")["k"] == ""


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
def test_only_first_document_is_read(loader):
    assert _load_with(loader, "plain: p\n---\n: : bad\n") == {"plain": "p"}


@pytest.mark.parametrize("loader", _LOADERS)
def test_eq_scalar(loader):
    assert _load_with(loader, "eq: =\n") == {"eq": "="}


def test_tab_after_colon_c_loader_accepts():
    if _C_LOADER is None:
        pytest.skip("no libyaml")
    assert _load_with(_C_LOADER, "plain:\tp\n") == {"plain": "p"}


def test_tab_after_colon_pure_loader_rejects():
    with pytest.raises(yaml.YAMLError):
        _load_with(_PURE_LOADER, "plain:\tp\n")


@pytest.mark.parametrize("loader", _LOADERS)
def test_complex_keys(loader):
    assert _load_with(loader, "? [a, b]\n: 1\n") == {("a", "b"): 1}
    assert _load_with(loader, "? {a: 1}\n: 1\n") == {(("a", 1),): 1}


@pytest.mark.parametrize("loader", _LOADERS)
def test_scalar_keys(loader):
    result = _load_with(loader, "1.5: a\n1: b\n~: e\n")
    assert result[1.5] == "a"
    assert result[1] == "b"
    assert result[None] == "e"

    # "true" and "yes" both tokenize to the same Python key (True), so the
    # later one wins -- exercised separately to keep the collision explicit.
    dup = _load_with(loader, "true: c\nyes: d\n")
    assert dup == {True: "d"}


@pytest.mark.parametrize("loader", _LOADERS)
def test_alias_as_a_key(loader):
    assert _load_with(loader, "a: &x 1\nb:\n  *x: v\n") == {"a": 1, "b": {1: "v"}}


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loader", _LOADERS)
def test_top_level_shapes(loader):
    assert _load_with(loader, "- a\n- b\n") == ["a", "b"]
    assert _load_with(loader, "hello\n") == "hello"
    assert _load_with(loader, "true\n") is True


@pytest.mark.parametrize("loader", _LOADERS)
@pytest.mark.parametrize("text", ["--- ~\n", "", "# just a comment\n"])
def test_empty_or_null_document_is_false(loader, text):
    assert _load_with(loader, text) is False


# ---------------------------------------------------------------------------
# BOM (data-file path: safe_load keeps a literal BOM char and swaps it)
# ---------------------------------------------------------------------------


def test_bom_variants_load():
    for text in ("﻿k: v\n", "﻿# c\n---\nk: v\n", "﻿{k: v}\n", '﻿"k": v\n'):
        assert safe_load(text)["k"] == "v"


def test_bom_then_document_marker_errors():
    with pytest.raises(BackendError, match="mapping values are not allowed"):
        safe_load("﻿---\nk: v\n")


def test_bom_then_directive_errors():
    with pytest.raises(BackendError):
        safe_load("﻿%YAML 1.1\n---\nk: v\n")


# ---------------------------------------------------------------------------
# safe_load itself: no-chain errors, False mapping, YAML error formatting
# ---------------------------------------------------------------------------


def test_safe_load_empty_and_null_become_false():
    assert safe_load("") is False
    assert safe_load("~\n") is False


def test_safe_load_parse_error_is_one_line_chain_free():
    with pytest.raises(BackendError) as excinfo:
        safe_load("a: b: c: :::\n")
    e = excinfo.value
    assert e.__cause__ is None
    assert e.__context__ is None
    assert "\n" not in str(e)


def test_safe_load_disallowed_class_chain_free():
    with pytest.raises(BackendError) as excinfo:
        safe_load("k: !ruby/object:Foo\n  a: 1\n")
    e = excinfo.value
    assert e.__cause__ is None
    assert e.__context__ is None


# ---------------------------------------------------------------------------
# ``_yaml_problem``: hand-built error shapes real PyYAML parsing never
# raises in its current code paths (every real ``MarkedYAMLError`` seen from
# ``yaml.load_all`` carries a ``problem`` alongside any mark), but which the
# function's own docstring commits to handling defensively for *any*
# ``yaml.YAMLError`` -- not just the handful of shapes a single PyYAML
# version's scanner/parser happens to raise today.
# ---------------------------------------------------------------------------


def _mark(line=4, column=7):
    return yaml.error.Mark("<test>", 0, line, column, None, None)


def test_yaml_problem_mark_without_text_skips_redaction():
    # `problem`/`context` both unset (`text` is falsy) but a mark is still
    # present: the redaction step is skipped and the position-only message
    # strips its own leading space.
    exc = yaml.error.MarkedYAMLError(
        context=None, context_mark=None, problem=None, problem_mark=_mark()
    )
    assert _yaml_problem(exc) == "at line 5 column 8"


def test_yaml_problem_text_without_mark():
    # `problem` set, no mark at all on either side.
    exc = yaml.error.MarkedYAMLError(
        context=None, context_mark=None, problem="unexpected token", problem_mark=None
    )
    assert _yaml_problem(exc) == "unexpected token"


def test_yaml_problem_marked_error_with_neither_text_nor_mark():
    # Both empty: falls past both `if`s to the same class-name fallback as
    # an unmatched `yaml.YAMLError` subtype.
    exc = yaml.error.MarkedYAMLError(
        context=None, context_mark=None, problem=None, problem_mark=None
    )
    assert _yaml_problem(exc) == "MarkedYAMLError"


def test_yaml_problem_unhandled_yaml_error_falls_back_to_type_name():
    # Neither a `MarkedYAMLError` nor a `ReaderError`: the function falls
    # back to the exception's own class name rather than raising or
    # returning nothing.
    assert _yaml_problem(yaml.YAMLError("oops")) == "YAMLError"


# ---------------------------------------------------------------------------
# YAMLBackend integration: non-hash rule through safe_load
# ---------------------------------------------------------------------------


def test_yaml_backend_non_hash_via_safe_load():
    backend = YAMLBackend()
    assert backend._as_data_hash(backend.loads("- a\n- b\n"), "p") == {}
    assert backend._as_data_hash(backend.loads(""), "p") == {}


def test_yaml_backend_symbol_keys_become_strings():
    backend = YAMLBackend()
    parsed = backend.loads(":a: 1\n:b: 2\n")
    assert backend._as_data_hash(parsed, "p") == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# hiera.yaml: BOM is stripped outright (unlike a data file), and symbol keys
# normalize the same way.
# ---------------------------------------------------------------------------


def test_hiera_yaml_bom_behaves_like_a_data_file_bom(tmp_path):
    # `puppet lookup` reads hiera.yaml via `HieraConfig.create` ->
    # `cached_file_data` -> `Puppet::Util::Yaml.safe_load(content, ...)`
    # directly -- *not* through `safe_load_file`'s BOM-stripping file read
    # -- so a BOM reaches YAML.safe_load exactly as it does for a data file
    # (measured against real Puppet 8.10.0 in `config-hiera-yaml-bom`: a
    # `<BOM>---\nversion: 5\n...` config errors identically either way).
    # A `---` document marker right after the BOM (no leading blank line)
    # therefore still errors for hiera.yaml too.
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        "﻿---\nversion: 5\ndefaults: {data_hash: yaml_data}\n"
        "hierarchy:\n  - {name: c, path: common.yaml}\n".encode("utf-8")
    )
    with pytest.raises(ConfigError, match="mapping values are not allowed"):
        _read_base_config(str(config), None)


def test_hiera_yaml_bom_without_document_marker_loads_only_the_first_key(tmp_path):
    # No leading "---": the BOM-swapped-for-a-space character shifts only
    # the first line's column, so a multi-key top-level mapping still
    # parses as *one* document ending where the second key's indentation
    # (column 0) no longer matches the first key's (column 1) -- the same
    # quirk a data file has, reproduced faithfully rather than "fixed".
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        "﻿version: 5\ndefaults: {data_hash: yaml_data}\n".encode("utf-8")
    )
    _source, base = _read_base_config(str(config), None)
    assert base == {"version": 5}


def test_hiera_yaml_symbol_keys_normalize(tmp_path):
    config = tmp_path / "hiera.yaml"
    config.write_bytes(
        (
            ":version: 5\n:defaults:\n  :data_hash: yaml_data\n:hierarchy:\n"
            "  - :name: c\n    :path: common.yaml\n"
        ).encode("utf-8")
    )
    _source, base = _read_base_config(str(config), None)
    assert base["version"] == 5
    assert base["hierarchy"][0]["name"] == "c"


def test_hiera_yaml_parse_error_raises_config_error(tmp_path):
    config = tmp_path / "hiera.yaml"
    config.write_bytes(b"a: b: c: :::\n")
    with pytest.raises(ConfigError):
        _read_base_config(str(config), None)
