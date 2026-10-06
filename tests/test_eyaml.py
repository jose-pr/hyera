"""``eyaml_lookup_key`` (PKCS7): fixture ciphertext, key precedence, wrong keys and the ``ENC[...]`` token grammar."""

import base64

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hyera import BackendError, EyamlBackend
from hyera.backends._eyaml import decrypt_string
from hyera.backends import Backend
from eyaml_support import (  # noqa: F401
    FIXTURE,
    PRIVATE_KEY_PATH,
    _decrypt,
    _envelope,
    _token,
    private_key,
    public_key,
)

PUBLIC_KEY_PATH = FIXTURE / "keys" / "public_key.pkcs7.pem"


# --- against the real fixture ciphertext --------------------------------


def test_decrypts_fixture_blobs():
    secrets = (FIXTURE / "data" / "secrets.eyaml").read_text(encoding="utf-8")
    assert _decrypt(
        "ENC[PKCS7," + secrets.split("secret: ENC[PKCS7,")[1].split("]")[0] + "]"
    ) == ("s3cr3t %{facts.os.family}")


# --- content encryption algorithms --------------------------------------


@pytest.mark.parametrize("bits", [128, 192, 256])
def test_content_encryption_algorithms(public_key, bits):
    der = _envelope(b"hello-" + str(bits).encode(), public_key, bits=bits)
    assert _decrypt(_token(der)) == "hello-{}".format(bits)


# --- BER encodings --------------------------------------------------------


def test_ber_encodings_constructed_content(public_key):
    der = _envelope(b"constructed-payload", public_key, constructed=True)
    assert _decrypt(_token(der)) == "constructed-payload"


def test_ber_encodings_indefinite_length(public_key):
    der = _envelope(
        b"indefinite-payload", public_key, indefinite=True, constructed=True
    )
    assert _decrypt(_token(der)) == "indefinite-payload"


def test_ber_encodings_optional_originator_info(public_key):
    # `originatorInfo` ([0] IMPLICIT, optional) sits between CMSVersion and
    # recipientInfos in a real EnvelopedData; eyaml itself never emits one,
    # but a spec-compliant decoder still has to skip over it.
    der = _envelope(b"originator-present", public_key, originator=True)
    assert _decrypt(_token(der)) == "originator-present"


def test_pkcs8_private_key(public_key, monkeypatch):
    # Re-wrap the fixture's own key as PKCS8 -- `load_pem_private_key`
    # accepts both without our code caring which.
    with open(PRIVATE_KEY_PATH, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    pkcs8_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    monkeypatch.setenv("HYERA_TEST_PKCS8", pkcs8_pem.decode("ascii"))
    der = _envelope(b"pkcs8-ok", public_key)
    result = decrypt_string(
        _token(der),
        {"pkcs7_private_key_env_var": "HYERA_TEST_PKCS8"},
        "k",
        "secrets.eyaml",
    )
    assert result == "pkcs8-ok"


# --- key precedence and env vars ------------------------------------------


def test_env_var_beats_file_with_warning(public_key, caplog, monkeypatch):
    with open(PRIVATE_KEY_PATH, "r", encoding="utf-8") as fh:
        pem_text = fh.read()
    monkeypatch.setenv("HYERA_TEST_KEY", pem_text)
    der = _envelope(b"env-wins", public_key)
    import logging

    with caplog.at_level(logging.WARNING, logger="hyera"):
        result = decrypt_string(
            _token(der),
            {
                "pkcs7_private_key": "keys/missing-should-be-ignored.pem",
                "pkcs7_private_key_env_var": "HYERA_TEST_KEY",
            },
            "k",
            "secrets.eyaml",
        )
    assert result == "env-wins"
    assert any(
        "both private_key and private_key_env_var specified" in r.getMessage()
        for r in caplog.records
    )


def test_b64_env_var_beats_file(public_key, monkeypatch):
    with open(PRIVATE_KEY_PATH, "rb") as fh:
        raw = fh.read()
    import base64

    monkeypatch.setenv("HYERA_TEST_B64_KEY", base64.b64encode(raw).decode("ascii"))
    der = _envelope(b"b64-wins", public_key)
    result = decrypt_string(
        _token(der),
        {
            "pkcs7_private_key": "keys/missing-should-be-ignored.pem",
            "pkcs7_b64_private_key_env_var": "HYERA_TEST_B64_KEY",
        },
        "k",
        "secrets.eyaml",
    )
    assert result == "b64-wins"


def test_env_var_unset(monkeypatch):
    monkeypatch.delenv("HYERA_TEST_UNSET_KEY", raising=False)
    with pytest.raises(BackendError, match="env HYERA_TEST_UNSET_KEY is not set"):
        _decrypt(
            "ENC[PKCS7,aGVsbG8=]", {"pkcs7_private_key_env_var": "HYERA_TEST_UNSET_KEY"}
        )


def test_b64_env_var_unset(monkeypatch):
    monkeypatch.delenv("HYERA_TEST_UNSET_B64_KEY", raising=False)
    with pytest.raises(BackendError, match="env HYERA_TEST_UNSET_B64_KEY is not set"):
        _decrypt(
            "ENC[PKCS7,aGVsbG8=]",
            {"pkcs7_b64_private_key_env_var": "HYERA_TEST_UNSET_B64_KEY"},
        )


def test_public_key_never_read(public_key, monkeypatch):
    # `_private_key_pem` only ever reads `pkcs7_private_key*` options; a
    # `pkcs7_public_key` option pointing at a file that would raise if
    # opened must be silently ignored.
    der = _envelope(b"no-public-key-read", public_key)
    real_open = open

    def guarded_open(path, *a, **k):
        if "public" in str(path):
            raise AssertionError("pkcs7_public_key must never be read")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", guarded_open)
    result = _decrypt(_token(der), {"pkcs7_public_key": str(PUBLIC_KEY_PATH)})
    assert result == "no-public-key-read"


# --- wrong key -------------------------------------------------------------


def test_wrong_key_is_bad_decrypt(public_key, tmp_path):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    other_path = tmp_path / "wrong_key.pem"
    other_path.write_bytes(other_pem)
    der = _envelope(b"wrong-key", public_key)
    with pytest.raises(BackendError, match="Error was bad decrypt"):
        _decrypt(_token(der), {"pkcs7_private_key": str(other_path)})


# --- Puppet parity: whole-value error text, key-before-ciphertext order,
# and the private key loaded at most once per decrypt_string call ----------


def test_decrypt_error_embeds_whole_value_not_just_the_token():
    # hiera-eyaml's eyaml_lookup_key.rb wraps one rescue around parsing and decrypting
    # the whole stored value and interpolates the entire value into the message, not
    # just the failing token (hiera-eyaml 5.0.1, backend-eyaml-pkcs7 case
    # corrupt_prefixed).
    data = "prefix ENC[PKCS7,aGVsbG8=] suffix"
    with pytest.raises(BackendError) as exc:
        _decrypt(data)
    message = str(exc.value)
    assert "decrypting {} when looking up".format(data) in message
    assert "decrypting ENC[PKCS7,aGVsbG8=] when looking up" not in message


def test_bad_key_is_reported_before_the_ciphertext_is_ever_parsed(tmp_path):
    # Pkcs7.decrypt (pkcs7.rb) parses the private key before it touches the ciphertext:
    # a malformed key with a malformed ciphertext reports the key problem, never "Could
    # not parse the PKCS7" (hiera-eyaml 5.0.1, "Error was Neither PUB key nor PRIV
    # key"; case backend-eyaml-pkcs7-bad-key).
    bad_key_path = tmp_path / "garbage.pem"
    bad_key_path.write_text("this is not a pem key at all\n", encoding="utf-8")
    with pytest.raises(
        BackendError, match="Error was Neither PUB key nor PRIV key$"
    ) as exc:
        _decrypt("ENC[PKCS7,aGVsbG8=]", {"pkcs7_private_key": str(bad_key_path)})
    assert "Could not parse the PKCS7" not in str(exc.value)


def test_private_key_parsed_once_per_decrypt_string_call(public_key, monkeypatch):
    # hiera-eyaml's Pkcs7.decrypt re-parses the key for every ENC[...] token (~50x the
    # `cryptography` cost per parse); this reuses one parse across the tokens of a
    # value, which share the configured key within one call.
    import hyera.backends._eyaml as eyaml_module

    calls = []
    real_load = eyaml_module._load_private_key

    def counting_load(key_pem):
        calls.append(key_pem)
        return real_load(key_pem)

    monkeypatch.setattr(eyaml_module, "_load_private_key", counting_load)

    tokens = [_token(_envelope(v, public_key)) for v in (b"a", b"b", b"c")]
    data = " ".join(tokens)
    result = _decrypt(data)
    assert result == "a b c"
    assert len(calls) == 1


def test_private_key_not_cached_across_decrypt_string_calls(public_key, monkeypatch):
    # The reuse above is per call, never a cache across calls: the configured key can
    # change between lookups (a pkcs7_private_key_env_var), so each call parses again.
    import hyera.backends._eyaml as eyaml_module

    calls = []
    real_load = eyaml_module._load_private_key

    def counting_load(key_pem):
        calls.append(key_pem)
        return real_load(key_pem)

    monkeypatch.setattr(eyaml_module, "_load_private_key", counting_load)

    der = _envelope(b"once-per-call", public_key)
    assert _decrypt(_token(der)) == "once-per-call"
    assert _decrypt(_token(der)) == "once-per-call"
    assert len(calls) == 2


# --- chomp -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [("a\r\n", "a"), ("a\n", "a"), ("a\r", "a"), ("a", "a")],
    ids=["crlf", "lf", "cr", "none"],
)
def test_chomp(public_key, raw, expected):
    der = _envelope(raw.encode(), public_key)
    assert _decrypt(_token(der)) == expected


# --- ENC[...] token grammar --------------------------------------------


def test_enc_across_newline_not_decrypted():
    # A newline immediately breaks hiera-eyaml's own token body charclass
    # (`[a-zA-Z0-9+/ =\n]` allows `\n` -- but *not* inside a scheme id or
    # between `ENC[` and the body); a plain non-ENC string is untouched.
    assert decrypt_string("no token here\nsecond line", {}, "k", "p") == (
        "no token here\nsecond line"
    )


def test_enc_token_with_whitespace_only_body_is_malformed():
    # `_TOKEN_RE`'s body allows a bare space/newline, so a whitespace-only body matches;
    # stripping it leaves nothing for `_STRIPPED_RE` (one real base64 character), which
    # fails.
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: malformed token"
    ):
        decrypt_string("ENC[ ]", {}, "k", "p")


def test_decrypted_value_cached_per_key(public_key, tmp_path, monkeypatch):
    """The engine keeps a key's decrypted result while the file is
    unchanged, not ``decrypt_string`` -- exercise it through a real ``Hiera``
    lookup and count actual decrypt calls via a wrapped ``decrypt_string``.
    """
    der = _envelope(b"cached-once", public_key)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.eyaml").write_text(
        "k: {}\n".format(_token(der)), encoding="utf-8"
    )
    key_path = PRIVATE_KEY_PATH.replace("\\", "/")
    hiera_yaml = (
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data}\n"
        "hierarchy:\n"
        "  - {name: s, lookup_key: eyaml_lookup_key, path: secrets.eyaml, "
        "options: {pkcs7_private_key: " + key_path + "}}\n"
    )
    (tmp_path / "hiera.yaml").write_text(hiera_yaml, encoding="utf-8")

    calls = []
    from hyera.backends import _eyaml as eyaml_mod

    real = eyaml_mod.decrypt_string

    def counting(data, options, key, path):
        calls.append(key)
        return real(data, options, key, path)

    monkeypatch.setattr(eyaml_mod, "decrypt_string", counting)

    from hyera import Hiera

    h = Hiera(str(tmp_path / "hiera.yaml"))
    assert h.lookup("k") == "cached-once"
    assert h.lookup("k") == "cached-once"
    assert calls == ["k"]


@pytest.mark.parametrize("revalidate", [True, False])
def test_changed_eyaml_file_is_reread(tmp_path, revalidate):
    from hyera import Hiera

    (tmp_path / "data").mkdir()
    data = tmp_path / "data" / "a.eyaml"
    data.write_text("password: old-secret\nother: 1\n", encoding="utf-8")
    (tmp_path / "hiera.yaml").write_text(
        "version: 5\n"
        "defaults: {datadir: data, data_hash: yaml_data}\n"
        "hierarchy:\n"
        "  - {name: s, lookup_key: eyaml_lookup_key, path: a.eyaml, "
        "options: {pkcs7_private_key: " + PRIVATE_KEY_PATH.replace("\\", "/") + "}}\n",
        encoding="utf-8",
    )
    h = Hiera(str(tmp_path / "hiera.yaml"), revalidate=revalidate)
    assert h.lookup("password") == "old-secret"

    data.write_text("password: rotated-new-secret\nadded: yes\n", encoding="utf-8")
    if revalidate:
        assert h.lookup("password") == "rotated-new-secret"
        assert h.lookup("added") is True
    else:
        assert h.lookup("password") == "old-secret"
        h.clear_cache()
        assert h.lookup("password") == "rotated-new-secret"


def test_eyaml_backend_registered_under_eyaml_lookup_key():
    # EyamlBackend is a public class (in hyera.__all__) even though nothing
    # constructs it directly -- the engine only ever reaches it through the
    # registry, by the function name a hiera.yaml entry declares.
    assert EyamlBackend.NAMES["function"] == ("eyaml_lookup_key",)
    assert Backend.find("eyaml_lookup_key") is EyamlBackend
