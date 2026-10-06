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
    # The built-in yaml/json/hocon/sops data_hash backends all reject a
    # uri location outright (ConfigError, before _files_for ever sees it),
    # but a third-party data_hash backend's own data_hash() is free to
    # accept one -- _files_for's own loc.is_uri check still has to exclude
    # it from sources() either way, same as test_sources_exclude_uri_
    # locations above does for a lookup_key uri (which never reaches this
    # check at all, being filtered out earlier by its own kind).
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
    # A data_hash entry with no location key at all (calls its function
    # once, with no location, same as a location-less lookup_key/data_dig
    # entry) has provider.locations is None -- _files_for must skip it
    # outright rather than trying to iterate None as if it were a list of
    # resolved locations.
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
    # _files_for resolves a level's own location (exist=True, a fresh
    # probe) and loads it immediately after, one level at a time -- unlike
    # the main lookup pipeline, nothing else of this same call runs in
    # between for a single-location level, so the only way the file can
    # still vanish between those two probes is something outside Hiera's
    # own code entirely (another process, here simulated by making the
    # exist-check's own probe delete the file as a side effect, the
    # instant after it observes "still there"). load_file's fresh probe
    # then correctly finds it gone despite the cached entry from an
    # earlier, successful sources() call, and this level contributes
    # nothing to the result instead of a stale or crashing entry.
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
