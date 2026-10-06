"""Include directives inside files that were themselves included."""

import pytest

from hyera import BackendError
from hyera.backends import HOCONBackend
from hocon_support import (  # noqa: F401
    BS,
)

# -- the include rules hold in a file reached through include file() ------


@pytest.fixture
def secret_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secret.txt").write_bytes(b"root = leaked\n")
    return tmp_path


def _load_through_file_include(directory, inner_text):
    (directory / "outer.conf").write_bytes(inner_text.encode("utf-8"))
    return HOCONBackend().loads('include file("outer.conf")\n')


def test_plain_quoted_include_in_an_included_file_contributes_nothing(secret_dir):
    result = _load_through_file_include(secret_dir, 'include "secret.txt"\nouter = 1\n')
    assert result == {"outer": 1}


def test_plain_quoted_include_two_files_deep_contributes_nothing(secret_dir):
    (secret_dir / "mid.conf").write_bytes(b'include "secret.txt"\nmid = 2\n')
    result = _load_through_file_include(
        secret_dir, 'include file("mid.conf")\nouter = 1\n'
    )
    assert result == {"mid": 2, "outer": 1}


def test_required_include_in_an_included_file_raises(secret_dir):
    with pytest.raises(BackendError, match="not supported"):
        _load_through_file_include(
            secret_dir, 'include required(file("secret.txt"))\nouter = 4\n'
        )


def test_caseless_include_in_an_included_file_raises(secret_dir):
    with pytest.raises(BackendError, match="not supported"):
        _load_through_file_include(
            secret_dir, 'INCLUDE FILE("secret.txt")\nouter = 2\n'
        )


def test_value_position_include_in_an_included_file_is_literal_text(secret_dir):
    result = _load_through_file_include(
        secret_dir, 'v = include file("secret.txt")\nouter = 3\n'
    )
    assert result == {"v": "include file(secret.txt)", "outer": 3}


def test_unicode_escape_in_an_included_file_is_decoded(secret_dir):
    result = _load_through_file_include(
        secret_dir, 'e = "' + BS + 'u00e9"\nouter = 5\n'
    )
    assert result == {"e": "é", "outer": 5}


def test_parse_error_chain_holds_no_document_text():
    secret = "planted-secret-value-7f3a"
    with pytest.raises(BackendError) as info:
        HOCONBackend().loads('k = "{}"\n}}\n'.format(secret))
    seen = []
    exc = info.value
    while exc is not None and exc not in seen:
        seen.append(exc)
        assert secret not in repr(exc) + str(exc.args)
        assert secret not in repr(vars(exc))
        exc = exc.__cause__ or exc.__context__
    assert len(seen) == 1
