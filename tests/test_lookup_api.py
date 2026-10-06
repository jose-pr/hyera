"""The public ``lookup()`` call: the five call forms, ``value_type``,
``override``, ``default_values_hash``, ``block``, ``()``/``[]``/``in``, and
the precedence and messages Puppet's own ``pops/lookup.rb`` documents."""

import pytest

from hyera import Hiera, HieraLookupError, KeyNotFoundError


@pytest.fixture
def fn(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={
            "data/a.yaml": "u: [1]\nh: {x: {p: 1}}\n",
            "data/common.yaml": (
                "lookup_options:\n"
                "  u: {merge: unique}\n"
                "  h: {merge: deep}\n"
                "  cv: {convert_to: Integer}\n"
                "u: [2]\n"
                "h: {x: {q: 2}, y: 3}\n"
                's: "str"\n'
                "n: 5\n"
                "big: 11\n"
                'cv: "7"\n'
                "nilk: ~\n"
                'k: "kv"\n'
                "mixed: [a, 1]\n"
                "hsi: {a: 1, b: 2}\n"
                'ovr: "v=%{myvar}"\n'
            ),
        },
    )
    return Hiera(str(root / "hiera.yaml"))


def test_call_forms_are_equivalent(fn):
    h = fn
    assert h.lookup("u", merge="unique") == [1, 2]
    assert h.lookup("u", None, "unique") == [1, 2]
    assert h.lookup("u", merge="unique") == h.lookup(name="u", merge="unique")
    assert h.lookup({"name": "u", "merge": "unique"}) == [1, 2]
    assert h.lookup("u", {"merge": "unique"}) == [1, 2]
    assert h("u", merge="unique") == [1, 2]
    assert h["u", None, "unique"] == [1, 2]
    assert h["k", None, "first", "d"] == h.lookup("k", None, "first", "d")


def test_bad_call_shapes_raise_type_error(fn):
    h = fn
    with pytest.raises(TypeError):
        h.lookup({"name": "k", "bogus": 1})
    with pytest.raises(TypeError):
        h.lookup({"name": "k"}, "Integer")
    with pytest.raises(TypeError):
        h.lookup("k", {"merge": "first"}, merge="first")
    with pytest.raises(TypeError):
        h.lookup({"merge": "first"})  # form 4 without "name"
    with pytest.raises(TypeError):
        h.lookup(())  # an empty tuple path has no root
    with pytest.raises(TypeError):
        h.lookup((1, "b"))  # a tuple path's root (element 0) must be a str
    with pytest.raises(TypeError):
        h.lookup("k", merge="")
    with pytest.raises(TypeError):
        h.lookup("k", merge=5)
    with pytest.raises(TypeError):
        h.lookup("k", block=1)
    with pytest.raises(TypeError, match="a name hash must be a dict with str keys"):
        h.lookup({5: "x"})
    with pytest.raises(
        TypeError, match="each name in a list must be a str or a tuple key path"
    ):
        h.lookup([5])
    with pytest.raises(TypeError, match="override must be a dict with str keys"):
        h.lookup("k", override={5: "x"})
    with pytest.raises(
        TypeError,
        match="value_type must be a type object, a hyera.types class, or a str, not int",
    ):
        h.lookup("k", value_type=5)


def test_name_lists(fn):
    h = fn
    assert h.lookup(["missing1", "k"]) == "kv"
    with pytest.raises(
        KeyNotFoundError,
        match=r"for any of the names \['missing1', 'missing2'\]",
    ):
        h.lookup(["missing1", "missing2"])
    with pytest.raises(KeyNotFoundError, match=r"for any of the names \[\]"):
        h.lookup([])


def test_default_values_hash_beats_default_value(fn):
    assert (
        fn.lookup(
            "missing", default_value="DFLT", default_values_hash={"missing": "DVH"}
        )
        == "DVH"
    )
    # A name the hash lacks still falls back to default_value.
    assert (
        fn.lookup("missing", default_value="DFLT", default_values_hash={"other": "x"})
        == "DFLT"
    )


def test_default_precedence(fn):
    h = fn
    # override beats a found value.
    assert h.lookup("k", override={"k": "OVR"}) == "OVR"
    # a found value beats default_values_hash.
    assert h.lookup("k", default_values_hash={"k": "DVH"}) == "kv"
    # default_values_hash beats a block.
    assert (
        h.lookup(
            "missing", default_values_hash={"missing": "DVH"}, block=lambda n: "BLK"
        )
        == "DVH"
    )
    # a block beats default_value.
    assert h.lookup("missing", block=lambda n: "BLK", default_value="DFLT") == "BLK"
    # default_value=None is a real default -- no KeyNotFoundError.
    assert h.lookup("missing", default_value=None) is None
    # a found None beats a default entirely.
    assert h.lookup("nilk", default_value="DFLT") is None
    # the block is called with the name argument exactly as given -- a list
    # stays a list, never flattened to a single name.
    assert h.lookup(["m1", "m2"], block=lambda n: n) == ["m1", "m2"]


def test_value_type_subjects(fn):
    h = fn
    with pytest.raises(
        HieraLookupError,
        match=r"Found value has wrong type, expects an Integer value, got String",
    ):
        h.lookup("s", "Integer")
    with pytest.raises(
        HieraLookupError,
        match=r"Default value has wrong type, expects an Integer value, got String",
    ):
        h.lookup("missing", "Integer", None, "x")
    with pytest.raises(
        HieraLookupError,
        match=(
            r"Value found for key 'k' in override hash has wrong type, "
            r"expects an Integer value, got String"
        ),
    ):
        h.lookup("k", "Integer", override={"k": "str_val"})
    with pytest.raises(
        HieraLookupError,
        match=(
            r"Value found for key 'missing' in default values hash has wrong "
            r"type, expects an Integer value, got String"
        ),
    ):
        h.lookup("missing", "Integer", default_values_hash={"missing": "str_val"})
    with pytest.raises(
        HieraLookupError,
        match=(
            r"Value returned from default block has wrong type, expects an "
            r"Integer value, got String"
        ),
    ):
        h.lookup("missing", "Integer", block=lambda n: "str_val")
    # Optional[String] accepts the found None.
    assert h.lookup("nilk", "Optional[String]") is None


def test_value_type_accepts_type_objects(fn):
    """``value_type`` takes a ``hyera.types`` object (bare class or
    subscripted) anywhere it takes a Puppet type-expression string, with
    identical results -- including the raised error text -- across every
    entry point that has a ``value_type``."""
    from hyera import types

    h = fn
    assert h.lookup("n", types.Integer) == h.lookup("n", "Integer") == 5
    assert h.lookup("n", types.Integer[1, 10]) == 5
    assert h("n", types.Integer) == 5
    assert h["n", types.Integer] == 5
    # dig()/get() assert the value_type against the *dug-out* result, so
    # the fixture's "hsi" hash (digging into key "a") exercises them the
    # same way the plain "n" key exercises lookup/()/[] above.
    assert h.dig("hsi", "a", value_type=types.Integer) == 1
    assert h.get("hsi.a", value_type=types.Integer) == 1
    explained = h.explain("n", types.Integer)
    assert explained.error is None

    def _message(value_type):
        with pytest.raises(HieraLookupError) as exc_info:
            h.lookup("s", value_type)
        return str(exc_info.value)

    assert _message(types.Integer) == _message("Integer")
    assert _message(types.Integer[1, 10]) == _message("Integer[1, 10]")

    def _dig_message(value_type):
        with pytest.raises(HieraLookupError) as exc_info:
            h.dig("s", value_type=value_type)
        return str(exc_info.value)

    assert _dig_message(types.Integer) == _dig_message("Integer")

    def _get_message(value_type):
        with pytest.raises(HieraLookupError) as exc_info:
            h.get("s", value_type=value_type)
        return str(exc_info.value)

    assert _get_message(types.Integer) == _get_message("Integer")


def test_override_and_defaults_feed_value_interpolation(fn, make_tree):
    h = fn
    assert h.lookup("ovr", override={"myvar": "from_override"}) == "v=from_override"
    assert (
        h.lookup("ovr", default_values_hash={"myvar": "from_defaults"})
        == "v=from_defaults"
    )
    # A defined scope variable beats default_values_hash.
    scoped = h.scoped(variables={"myvar": "from_scope"})
    assert (
        scoped.lookup("ovr", default_values_hash={"myvar": "from_defaults"})
        == "v=from_scope"
    )

    # An override never changes which hierarchy locations are read.
    root = make_tree(
        {"hierarchy": [{"name": "role", "path": "%{role}.yaml"}]},
        files={"data/web.yaml": 'pick: web\necho: "r=%{role}"\n'},
    )
    from hyera import Scope

    h2 = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"role": "web"}))
    assert h2.lookup("pick", override={"role": "db"}) == "web"
    assert h2.lookup("echo", override={"role": "db"}) == "r=db"


def test_override_and_defaults_propagate_into_nested_lookup(make_tree):
    # nested_lookup's own override/default_values check (for a %{lookup()}/%{hiera()}
    # call in an interpolated value), distinct from the outer lookup's handling that
    # test_override_and_defaults_feed_value_interpolation covers via a scope variable.
    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml"}]},
        files={
            "data/a.yaml": (
                "outer: \"x%{lookup('inner')}y\"\n"
                "outer_missing: \"x%{lookup('missingkey')}y\"\n"
            )
        },
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.lookup("outer", override={"inner": "OVR"}) == "xOVRy"
    assert (
        h.lookup("outer_missing", default_values_hash={"missingkey": "DVH"}) == "xDVHy"
    )


def test_override_is_returned_unconverted(fn):
    h = fn
    # pops/lookup.rb:33 -- an override hit returns before search_and_merge,
    # so cv's own convert_to (Integer) never runs on it.
    assert h.lookup("cv", override={"cv": "9"}) == "9"
    assert h.lookup("cv") == 7


def test_contains(fn):
    h = fn
    assert "k" in h
    assert "nilk" in h
    assert "missing" not in h
    assert "lookup_options" not in h
    with pytest.raises(HieraLookupError):
        "a..b" in h


def test_legacy_function_mapping(fn):
    # hiera(k[, default]) -> lookup(k, None, "first"[, default])
    # hiera_array(k) -> lookup(k, None, "unique")
    # hiera_hash(k) -> lookup(k, None, "hash")
    h = fn
    assert h.lookup("cv", None, "first") == 7
    assert h.lookup("u", None, "unique") == [1, 2]
    assert h.lookup("h", None, "hash") == {"x": {"p": 1}, "y": 3}
    assert h.lookup("missing", None, "first", "hdflt") == "hdflt"


def test_hiera_is_not_iterable(fn):
    with pytest.raises(TypeError):
        iter(fn)


def test_a_tuple_name_matches_override_through_its_dotted_form(fn):
    assert fn.lookup(("k",), override={"k": "OVR"}) == "OVR"
    assert fn.lookup(("hsi", "a"), override={"hsi.a": 99}) == fn.lookup(
        "hsi.a", override={"hsi.a": 99}
    )
    assert fn.lookup(("hsi", "a"), override={"hsi.a": 99}) == 99


def test_a_tuple_name_matches_default_values_hash_through_its_dotted_form(fn):
    assert fn.lookup(("zz",), default_values_hash={"zz": "D"}) == "D"
    assert fn.lookup(("zz", 0), default_values_hash={"zz.0": "D"}) == "D"
