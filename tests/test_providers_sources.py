"""``Hiera.sources`` over function-provider levels."""

from hyera import Hiera
from providers_support import (  # noqa: F401
    _isolated_registry,
    backends,
    calls,
    script,
)


def test_sources_exclude_uri_locations(make_tree, backends, script):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "u", "lookup_key": "test_lookup_key", "uri": "mailto:a@b"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    script["lookup_key"] = lambda key, options, context: "v"
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert all("mailto" not in str(s) for s in sources)
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_exclude_a_data_hash_uri_location(make_tree, backends, script):
    # The built-in data_hash backends reject a uri location, but a third-party
    # data_hash() may accept one; _files_for's `loc.is_uri` check must still exclude it
    # from sources(). A lookup_key uri is filtered out earlier, by kind.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "u", "data_hash": "test_data_hash", "uri": "mailto:a@b"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    script["data_hash"] = lambda path, options: {}
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert all("mailto" not in str(s) for s in sources)
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_exclude_a_nonexistent_location(make_tree):
    root = make_tree(
        {
            "hierarchy": [
                {"name": "missing", "path": "missing.yaml"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert all("missing.yaml" not in str(s) for s in sources)
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_skips_a_location_less_data_hash_level(make_tree):
    # A data_hash entry with no location key has `provider.locations is None`;
    # _files_for must skip it rather than iterate None.
    root = make_tree(
        {
            "hierarchy": [
                {"name": "nolock", "data_hash": "yaml_data"},
                {"name": "common", "path": "common.yaml"},
            ]
        },
        files={"data/common.yaml": "k: v\n"},
    )
    h = Hiera(str(root / "hiera.yaml"))
    sources = h.sources()
    assert any(str(s).endswith("common.yaml") for s in sources)


def test_sources_skips_a_file_that_vanishes_between_its_own_exist_check_and_load(
    make_tree, monkeypatch
):
    # _files_for loads a single-location level right after resolving it, so only the
    # exist-check's probe deleting the file (simulating another process) can remove it
    # between the probes; load_file then finds it gone and the level is empty.
    from hyera._lookup import locations

    root = make_tree(
        {"hierarchy": [{"name": "s", "path": "a.yaml"}]},
        files={"data/a.yaml": "k: v\n"},
    )
    path = root / "data" / "a.yaml"

    h = Hiera(str(root / "hiera.yaml"))
    first = h.sources()
    assert any(str(s).endswith("a.yaml") for s in first)

    real_probe = locations._probe
    armed = []

    def vanishing_probe(p):
        result = real_probe(p)
        if armed and str(p) == str(path) and result.kind == "file":
            path.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(locations, "_probe", vanishing_probe)
    armed.append(True)

    second = h.sources()
    assert second == ()
