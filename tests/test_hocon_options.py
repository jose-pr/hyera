"""The ``hocon_includes`` option on an entry, in ``defaults`` and in the hierarchy
config.
"""

import pytest

from hyera import BackendError, ConfigError, Hiera
from hyera.backends import HOCONBackend
from hocon_support import (  # noqa: F401
    pyhocon_tripwire,
)

# -- default: a hierarchy-level `hocon_includes: false` reaches the
# backend the same way `datadir` already does --------------------------------


def test_hocon_includes_false_via_hierarchy_conf(
    tmp_path, monkeypatch, pyhocon_tripwire
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")
    conf = {"hocon_includes": False}
    with pytest.raises(BackendError, match="line 1"):
        HOCONBackend(conf).loads('include file("inc.conf")\nplain = p\n')
    assert pyhocon_tripwire == []


# -- the stricter include mode is selected from hiera.yaml through options --


def _hocon_tree(make_tree, tmp_path, monkeypatch, entry_options=None, defaults=None):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "inc.conf").write_bytes(b"fromfile = included\n")
    entry = {"name": "h", "data_hash": "hocon_data", "path": "c.conf"}
    if entry_options is not None:
        entry["options"] = entry_options
    config = {"hierarchy": [entry]}
    if defaults is not None:
        config["defaults"] = defaults
    root = make_tree(
        config,
        files={"data/c.conf": 'include file("inc.conf")\nk = v\n'},
    )
    return Hiera(str(root / "hiera.yaml"))


def test_hocon_includes_option_unset_reads_the_included_file(
    make_tree, tmp_path, monkeypatch
):
    h = _hocon_tree(make_tree, tmp_path, monkeypatch)
    assert h.lookup("fromfile") == "included"


def test_hocon_includes_false_option_on_the_entry_refuses_file_includes(
    make_tree, tmp_path, monkeypatch
):
    h = _hocon_tree(make_tree, tmp_path, monkeypatch, {"hocon_includes": False})
    with pytest.raises(BackendError, match="line 1"):
        h.lookup("k")


def test_hocon_includes_true_option_keeps_the_default(make_tree, tmp_path, monkeypatch):
    h = _hocon_tree(make_tree, tmp_path, monkeypatch, {"hocon_includes": True})
    assert h.lookup("fromfile") == "included"


def test_hocon_includes_false_option_from_defaults_refuses_file_includes(
    make_tree, tmp_path, monkeypatch
):
    h = _hocon_tree(
        make_tree,
        tmp_path,
        monkeypatch,
        defaults={"options": {"hocon_includes": False}},
    )
    with pytest.raises(BackendError, match="line 1"):
        h.lookup("k")


def test_hocon_includes_option_must_be_a_boolean(make_tree, tmp_path, monkeypatch):
    h = _hocon_tree(make_tree, tmp_path, monkeypatch, {"hocon_includes": "no"})
    with pytest.raises(ConfigError, match="hocon_includes.*Boolean"):
        h.lookup("k")


def test_other_hocon_data_options_are_still_refused(make_tree, tmp_path, monkeypatch):
    h = _hocon_tree(
        make_tree, tmp_path, monkeypatch, {"hocon_includes": False, "other": 1}
    )
    with pytest.raises(ConfigError, match="one of 'path'"):
        h.lookup("k")


# -- hocon_env: a substitution the document does not define and the process
# environment ---------------------------------------------------------------

_VAR = "HYERA_TEST_HOCON_ENV"


def test_hocon_env_default_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv(_VAR, "from-env")
    text = "req = ${%s}\nopt = ${?%s}\n" % (_VAR, _VAR)
    assert HOCONBackend().loads(text) == {"req": "from-env", "opt": "from-env"}


def test_hocon_env_false_by_constructor_leaves_the_optional_form_absent(monkeypatch):
    monkeypatch.setenv(_VAR, "from-env")
    text = "a = 1\nopt = ${?%s}\n" % _VAR
    assert HOCONBackend(hocon_env=False).loads(text) == {"a": 1}


def test_hocon_env_false_by_constructor_fails_the_required_form(monkeypatch):
    monkeypatch.setenv(_VAR, "from-env")
    with pytest.raises(BackendError, match="Cannot resolve"):
        HOCONBackend(hocon_env=False).loads("req = ${%s}\n" % _VAR)


def test_hocon_env_false_still_resolves_a_name_the_document_defines(monkeypatch):
    monkeypatch.setenv("home", "from-env")
    text = "home = mine\nuse = ${home}\n"
    expected = {"home": "mine", "use": "mine"}
    assert HOCONBackend(hocon_env=False).loads(text) == expected


def test_hocon_env_false_stops_a_self_reference_reading_the_environment(monkeypatch):
    monkeypatch.setenv("path", "from-env")
    assert HOCONBackend(hocon_env=False).loads("path = ${?path}\n") == {}


def test_hocon_env_is_restored_after_a_load(monkeypatch):
    monkeypatch.setenv(_VAR, "from-env")
    HOCONBackend(hocon_env=False).loads("a = 1\n")
    assert HOCONBackend().loads("r = ${%s}\n" % _VAR) == {"r": "from-env"}


def _env_tree(make_tree, entry_options=None, defaults=None):
    entry = {"name": "h", "data_hash": "hocon_data", "path": "c.conf"}
    if entry_options is not None:
        entry["options"] = entry_options
    config = {"hierarchy": [entry]}
    if defaults is not None:
        config["defaults"] = defaults
    root = make_tree(config, files={"data/c.conf": "k = v\nopt = ${?%s}\n" % _VAR})
    return Hiera(str(root / "hiera.yaml"))


def test_hocon_env_false_option_on_the_entry(make_tree, monkeypatch):
    monkeypatch.setenv(_VAR, "from-env")
    assert _env_tree(make_tree).lookup("opt") == "from-env"
    h = _env_tree(make_tree, {"hocon_env": False})
    assert h.lookup("opt", None, None, "absent") == "absent"


def test_hocon_env_option_must_be_a_boolean(make_tree):
    h = _env_tree(make_tree, {"hocon_env": "no"})
    with pytest.raises(ConfigError, match="'hocon_env' must be a Boolean"):
        h.lookup("k")


def test_hocon_env_and_includes_options_combine(make_tree, monkeypatch):
    monkeypatch.setenv(_VAR, "from-env")
    h = _env_tree(make_tree, {"hocon_env": False, "hocon_includes": False})
    assert h.lookup("k") == "v"
    assert h.lookup("opt", None, None, "absent") == "absent"
