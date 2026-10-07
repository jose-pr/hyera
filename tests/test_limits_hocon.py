"""``Limits.hocon_substitution_size``: a HOCON substitution that would insert
more than the limit is refused before the value is copied."""

import pickle
import tracemalloc

import pytest

import hyera

pytest.importorskip("pyhocon")


def _doubling(levels, kind):
    first = {"str": '"0123456789"', "list": "[1, 2, 3, 4, 5]", "obj": "{ v = 1 }"}
    lines = ["a0 = " + first[kind]]
    for i in range(1, levels + 1):
        if kind == "str":
            lines.append("a{0} = ${{a{1}}}${{a{1}}}".format(i, i - 1))
        elif kind == "list":
            lines.append("a{0} = ${{a{1}}} ${{a{1}}}".format(i, i - 1))
        else:
            lines.append("a{0} = {{ x = ${{a{1}}}, y = ${{a{1}}} }}".format(i, i - 1))
    return "\n".join(lines) + "\n"


def _hocon_tree(make_tree, text, **options):
    root = make_tree(
        {
            "hierarchy": [{"name": "c", "path": "c.conf"}],
            "defaults": {"data_hash": "hocon_data"},
        },
        files={"data/c.conf": text},
    )
    return hyera.Hiera(root / "hiera.yaml", **options)


@pytest.mark.parametrize("kind", ["str", "list", "obj"])
def test_a_doubling_document_is_refused_with_bounded_memory(make_tree, kind):
    limits = hyera.Limits(hocon_substitution_size=10_000)
    h = _hocon_tree(make_tree, _doubling(40, kind), limits=limits)
    tracemalloc.start()
    try:
        with pytest.raises(hyera.BackendError) as raised:
            h.lookup("a40")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert "limits.hocon_substitution_size" in str(raised.value)
    assert "10000" in str(raised.value)
    # Unbounded, forty doublings of ten characters would need about 10**13 bytes.
    assert peak < 64 * 1024 * 1024


@pytest.mark.parametrize("kind", ["str", "list", "obj"])
def test_a_document_under_the_limit_loads_as_without_limits(make_tree, kind):
    text = _doubling(4, kind)
    plain = _hocon_tree(make_tree, text).lookup("a4")
    limits = hyera.Limits(hocon_substitution_size=10_000)
    assert _hocon_tree(make_tree, text, limits=limits).lookup("a4") == plain


def test_the_limit_counts_what_one_substitution_inserts(make_tree):
    text = 'a = "0123456789"\nb = ${a}\n'
    at = hyera.Limits(hocon_substitution_size=10)
    under = hyera.Limits(hocon_substitution_size=9)
    assert _hocon_tree(make_tree, text, limits=at).lookup("b") == "0123456789"
    with pytest.raises(hyera.BackendError):
        _hocon_tree(make_tree, text, limits=under).lookup("b")


def test_without_the_field_a_modest_doubling_is_unchanged(make_tree):
    text = _doubling(8, "str")
    other = hyera.Limits(yaml_alias_nodes=5)
    assert len(_hocon_tree(make_tree, text).lookup("a8")) == 10 * 2**8
    assert len(_hocon_tree(make_tree, text, limits=other).lookup("a8")) == 10 * 2**8


def test_the_field_is_validated_compared_and_pickled():
    limits = hyera.Limits(hocon_substitution_size=7)
    assert limits.hocon_substitution_size == 7
    assert limits != hyera.Limits()
    assert pickle.loads(pickle.dumps(limits)) == limits
    assert "hocon_substitution_size=7" in repr(limits)
    with pytest.raises(ValueError):
        hyera.Limits(hocon_substitution_size=0)
    with pytest.raises(TypeError):
        hyera.Limits(hocon_substitution_size="7")
