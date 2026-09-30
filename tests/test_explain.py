"""``Hiera.explain()``: the engine hooks wired through the lookup pipeline,
and the public method built on top of them.

The renderer itself (``hyera._explain``) has its own byte-exact tests
against recorded Puppet goldens (``tests/test_explain_render.py``); these
exercise the *hooks* -- that a real lookup across merges, interpolation,
overrides/defaults, layers and the default config produces a sensible,
well-formed report, and that explaining never changes what an ordinary
lookup sees.
"""

import hyera
import hyera._explain
from hyera import BackendError, Hiera, HieraLookupError, KeyNotFoundError, Scope


def test_explain_call_forms_agree(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "b", "path": "b.yaml"},
            ]
        },
        files={"data/a.yaml": "k: {x: 1}\n", "data/b.yaml": "k: {y: 2}\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    a = h.explain("k", None, "deep").to_hash()
    b = h.explain({"name": "k", "merge": "deep"}).to_hash()
    c = h.explain("k", {"merge": "deep"}).to_hash()
    d = h.explain("k", merge="deep").to_hash()
    assert a == b == c == d


def test_explain_options_is_not_a_lookup_option(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    try:
        h.explain({"name": "k", "explain_options": True})
        raised_by_explain = None
    except TypeError as e:
        raised_by_explain = str(e)
    try:
        h.lookup({"name": "k", "explain_options": True})
        raised_by_lookup = None
    except TypeError as e:
        raised_by_lookup = str(e)
    assert raised_by_explain is not None
    assert raised_by_explain == raised_by_lookup


def test_explain_miss_is_last_line_and_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    result = h.explain("nope")
    assert isinstance(result.error, KeyNotFoundError)
    assert result.text().endswith(
        "Function lookup() did not find a value for the name 'nope'\n"
    )


def test_explain_reraises_data_file_errors(tmp_path, make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    mod_dir = tmp_path / "modules" / "m"
    (mod_dir / "data").mkdir(parents=True)
    (mod_dir / "hiera.yaml").write_bytes(
        b"version: 5\nhierarchy:\n  - name: c\n    path: c.yaml\n"
    )
    # A malformed module data file is never touched by construction (which
    # reads no data file at all), so it fails at explain/lookup time,
    # exactly what this test needs to exercise. Genuine
    # YAML syntax breakage (not just a non-hash top level, which the default
    # strict="warning" only warns about) always raises.
    (mod_dir / "data" / "c.yaml").write_bytes(b"k: [1, 2\n")
    h = Hiera(str(root / "hiera.yaml"), basemodulepath=[str(tmp_path / "modules")])
    lookup_error = None
    try:
        h.lookup("m::k")
    except BackendError as e:
        lookup_error = str(e)
    assert lookup_error is not None
    explain_error = None
    try:
        h.explain("m::k")
    except BackendError as e:
        explain_error = str(e)
    assert explain_error == lookup_error


def test_explain_reports_override_and_default_values_hits(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": 'v: "%{x}"\nw: "%{y}"\n'},
    )
    h = Hiera(str(root / "hiera.yaml"))
    overrides_text = h.explain("v", override={"x": "o"}).text()
    assert 'Found key: "x" value: "o" in overrides' in overrides_text
    defaults_text = h.explain("w", default_values_hash={"y": "d"}).text()
    assert 'Found key: "y" value: "d" in defaults' in defaults_text


def test_explain_after_lookup_still_searches_lookup_options(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    h.lookup("k")
    assert h.explain("k").text().startswith('Searching for "lookup_options"')


def test_explain_on_scoped_view(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "os", "path": "os/%{facts.os.family}.yaml"}]},
        files={
            "data/os/RedHat.yaml": "k: redhat\n",
            "data/os/Debian.yaml": "k: debian\n",
        },
        facts={"os": {"family": "RedHat"}},
    )
    h = Hiera(str(root / "hiera.yaml"))
    view = h.scoped(facts={"os": {"family": "Debian"}})
    text = view.explain("k").text()
    assert "Debian.yaml" in text
    assert "RedHat.yaml" not in text


def test_default_config_prunes_missing_paths(tmp_path):
    with_file = tmp_path / "with_file"
    (with_file / "data").mkdir(parents=True)
    (with_file / "data" / "common.yaml").write_bytes(b"k: v\n")
    h = Hiera(None, base_path=with_file)
    text = h.explain("k").text()
    assert "Using configuration" not in text
    assert 'Hierarchy entry "Common"' in text

    without_file = tmp_path / "without_file"
    without_file.mkdir()
    h2 = Hiera(None, base_path=without_file)
    text2 = h2.explain("k").text()
    assert not any("Common" in line for line in text2.splitlines())
    assert (
        'Searching for "k"\n'
        "  Global Data Provider (hiera configuration version 5)\n"
        '    No such key: "k"\n'
    ) in text2


def test_explain_leaves_lookups_and_sources_unchanged(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    before_value = h.lookup("k")
    before_sources = h.sources()
    tree = h.explain("k").to_hash()
    tree["mutated"] = "oops"
    assert h.lookup("k") == before_value
    assert h.sources() == before_sources


def test_lookup_records_nothing_without_explain(make_tree, monkeypatch):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "a", "path": "a.yaml"},
                {"name": "b", "path": "b.yaml"},
            ]
        },
        files={
            "data/a.yaml": 'arr: [a]\nfacts_ref: "%{facts.x}"\n',
            "data/b.yaml": "arr: [b]\n",
        },
        facts={"x": "1"},
    )

    def boom(self, *a, **k):
        raise AssertionError("Explainer.push must never run for an ordinary lookup")

    monkeypatch.setattr(hyera._explain.Explainer, "push", boom)
    h = Hiera(str(root / "hiera.yaml"), scope=Scope(facts={"x": "1"}))
    assert h.lookup("arr", merge="unique") == ["a", "b"]
    assert h.lookup("facts_ref") == "1"


def test_explain_block_form_has_no_error(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    result = h.explain("nope", block=lambda n: "b")
    assert result.error is None
    assert "did not find" not in result.text()


def test_hierarchy_level_name(make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "custom-name", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    assert h.hierarchy[0].name == "custom-name"


def test_environment_layer_errors_are_reported(tmp_path, make_tree):
    root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "k: v\n"},
    )
    env_dir = tmp_path / "environments" / "target"
    (env_dir / "data").mkdir(parents=True)
    (env_dir / "hiera.yaml").write_bytes(
        b"version: 5\nhierarchy:\n  - name: c\n    path: c.yaml\n"
    )
    (env_dir / "data" / "c.yaml").write_bytes(b"envself: \"%{lookup('envself')}\"\n")
    h = Hiera(
        str(root / "hiera.yaml"),
        environmentpath=[str(tmp_path / "environments")],
        scope=Scope(environment="target"),
    )
    result = h.explain("envself")
    assert isinstance(result.error, HieraLookupError)
    assert result.text().endswith("Recursive lookup detected in [envself]\n")

    # The same recursive data in the *global* layer escapes instead.
    global_root = make_tree(
        {"hierarchy": [{"name": "c", "path": "c.yaml"}]},
        files={"data/c.yaml": "envself: \"%{lookup('envself')}\"\n"},
        root="global",
    )
    h2 = Hiera(str(global_root / "hiera.yaml"))
    raised = None
    try:
        h2.explain("envself")
    except HieraLookupError as e:
        raised = e
    assert raised is not None
