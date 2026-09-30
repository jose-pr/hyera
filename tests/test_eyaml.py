"""``eyaml_lookup_key`` (PKCS7): token scanning, key precedence, and the
bounds-checked BER/DER decrypt path, against both the real fixture
ciphertext and hand-built synthetic envelopes covering encodings the
fixture itself does not exercise (AES-128/192, BER constructed/indefinite
content, a PKCS8 private key)."""

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hyera import BackendError
from hyera._eyaml import _decode64, decrypt_string

FIXTURE = (
    Path(__file__).resolve().parent / "conformance" / "cases" / "backend-eyaml-pkcs7"
)
PRIVATE_KEY_PATH = str(FIXTURE / "keys" / "private_key.pkcs7.pem")
PUBLIC_KEY_PATH = FIXTURE / "keys" / "public_key.pkcs7.pem"

_AES_OIDS = {
    128: "2.16.840.1.101.3.4.1.2",
    192: "2.16.840.1.101.3.4.1.22",
    256: "2.16.840.1.101.3.4.1.42",
}


def _der_len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(content)) + content


def _oid_bytes(dotted: str) -> bytes:
    parts = [int(x) for x in dotted.split(".")]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        if p == 0:
            out += bytes([0])
            continue
        chunk = []
        while p:
            chunk.insert(0, p & 0x7F)
            p >>= 7
        for i in range(len(chunk) - 1):
            chunk[i] |= 0x80
        out += bytes(chunk)
    return out


def _oid(dotted: str) -> bytes:
    return _tlv(0x06, _oid_bytes(dotted))


@pytest.fixture(scope="module")
def public_key():
    # Derived from the fixture's own private key, not by parsing
    # public_key.pkcs7.pem's certificate: that self-signed cert has a
    # zero serial number (a real eyaml fixture property -- see the
    # parent plan's Known Facts), which newer `cryptography` releases
    # reject under `filterwarnings=error` as an RFC 5280 violation. The
    # keypair is identical either way.
    with open(PRIVATE_KEY_PATH, "rb") as fh:
        private_key = serialization.load_pem_private_key(fh.read(), password=None)
    return private_key.public_key()


def _envelope(
    plaintext: bytes,
    pubkey,
    *,
    bits: int = 256,
    constructed: bool = False,
    indefinite: bool = False,
    key_oid: str = "1.2.840.113549.1.1.1",
    content_oid=None,
    iv_len: int = 16,
) -> bytes:
    """Hand-build a minimal PKCS7 ``EnvelopedData`` ``ContentInfo`` blob
    encrypted to ``pubkey`` -- the encoder side of :func:`hyera._eyaml.
    _pkcs7_decrypt`'s decoder, used only by these tests."""
    if content_oid is None:
        content_oid = _AES_OIDS[bits]
    key = os.urandom(bits // 8)
    iv = os.urandom(iv_len)
    padder = sympad.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(key), modes.CBC(iv[:16].ljust(16, b"\0"))
    ).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    enc_key = pubkey.encrypt(key, padding.PKCS1v15())

    rid = _tlv(0x30, _tlv(0x30, b"") + _tlv(0x02, b"\x01"))
    key_alg = _tlv(0x30, _oid(key_oid) + _tlv(0x05, b""))
    ktri = _tlv(0x30, _tlv(0x02, b"\x00") + rid + key_alg + _tlv(0x04, enc_key))
    rinfos = _tlv(0x31, ktri)

    content_alg = _tlv(0x30, _oid(content_oid) + _tlv(0x04, iv))
    content_type = _oid("1.2.840.113549.1.7.1")
    if constructed:
        half = len(ciphertext) // 2
        chunks = _tlv(0x04, ciphertext[:half]) + _tlv(0x04, ciphertext[half:])
        enc_content = bytes([0xA0]) + _der_len(len(chunks)) + chunks
    else:
        enc_content = bytes([0x80]) + _der_len(len(ciphertext)) + ciphertext
    eci = _tlv(0x30, content_type + content_alg + enc_content)

    env_body = _tlv(0x02, b"\x00") + rinfos + eci
    if indefinite:
        env = bytes([0x30, 0x80]) + env_body + b"\x00\x00"
    else:
        env = _tlv(0x30, env_body)

    explicit = bytes([0xA0]) + _der_len(len(env)) + env
    return _tlv(0x30, _oid("1.2.840.113549.1.7.3") + explicit)


def _token(der: bytes) -> str:
    import base64

    return "ENC[PKCS7,{}]".format(base64.b64encode(der).decode("ascii"))


def _decrypt(token: str, options=None):
    opts = {"pkcs7_private_key": PRIVATE_KEY_PATH}
    if options:
        opts.update(options)
    return decrypt_string(token, opts, "k", "secrets.eyaml")


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


# --- malformed blobs: never anything but BackendError ----------------------


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d[:5], "Could not parse the PKCS7"),
        (lambda d: d[:3] + bytes([d[3] ^ 0xFF]) + d[4:], "Could not parse the PKCS7"),
        (
            lambda d: d[:10] + bytes([0x84, 0xFF, 0xFF, 0xFF, 0xFF]) + d[15:],
            "Could not parse the PKCS7",
        ),
        (
            lambda d: d + b"\xff" * 4,
            None,
        ),  # trailing junk after a valid blob: still decrypts
    ],
    ids=[
        "truncated",
        "corrupted-length-byte",
        "length-past-buffer",
        "trailing-junk-still-decrypts",
    ],
)
def test_malformed_blobs(public_key, mutate, match):
    der = _envelope(b"malformed-probe", public_key)
    mutated = mutate(der)
    token = "ENC[PKCS7,{}]".format(_b64(mutated))
    if match is None:
        assert _decrypt(token) == "malformed-probe"
    else:
        with pytest.raises(BackendError, match=match):
            _decrypt(token)


def test_malformed_rsa_recipient_wrong_algorithm(public_key):
    # A key-encryption algorithm OID that is not rsaEncryption (eyaml/our
    # decrypt path supports only PKCS#1 v1.5 key transport).
    der = _envelope(b"oaep", public_key, key_oid="1.2.840.113549.1.1.7")
    with pytest.raises(BackendError, match="Could not parse the PKCS7"):
        _decrypt(_token(der))


def test_malformed_unknown_content_algorithm(public_key):
    der = _envelope(b"unknown-alg", public_key, content_oid="1.2.3.4.5")
    with pytest.raises(BackendError, match="Could not decrypt the PKCS7"):
        _decrypt(_token(der))


def test_malformed_never_raises_index_or_recursion_error(public_key):
    """Mutation fuzzing (not property-based): every seeded random byte
    flip/truncation of a real envelope must surface only as ``BackendError``
    (or decrypt cleanly, if the mutation happened to land somewhere inert) --
    never ``IndexError``/``RecursionError``/a hang.

    Each iteration is a real RSA decryption, so the default run is short;
    set ``HYERA_FUZZ_ITERATIONS`` (e.g. 5000) for a long run after changing
    the PKCS7 decoder. The seed is fixed, so a longer run extends the same
    sequence."""
    der = _envelope(b"bounds-probe", public_key)
    import os
    import random

    rng = random.Random(1234)
    for _ in range(int(os.environ.get("HYERA_FUZZ_ITERATIONS", "300"))):
        mutated = bytearray(der)
        kind = rng.random()
        if kind < 0.5:
            for _flip in range(rng.randint(1, 5)):
                i = rng.randrange(len(mutated))
                mutated[i] = rng.randrange(256)
        else:
            cut = rng.randrange(1, len(mutated))
            mutated = mutated[:cut]
        token = "ENC[PKCS7,{}]".format(_b64(bytes(mutated)))
        try:
            _decrypt(token)
        except BackendError:
            pass


# --- plaintext never leaks in errors ---------------------------------------


def test_errors_never_contain_plaintext_invalid_utf8(public_key):
    der = _envelope(b"\xff\xfe\xfd", public_key)
    with pytest.raises(BackendError) as exc:
        _decrypt(_token(der))
    message = str(exc.value)
    assert "invalid byte sequence in UTF-8" in message
    assert "\xff" not in message and "\\xff" not in message


def test_errors_never_contain_plaintext_after_good_token(public_key):
    good_der = _envelope(b"GOOD-SECRET-VALUE", public_key)
    good_token = _token(good_der)
    bad_token = "ENC[PKCS7,aGVsbG8=]"
    data = "prefix {} mid {} suffix".format(good_token, bad_token)
    with pytest.raises(BackendError) as exc:
        _decrypt(data)
    assert "GOOD-SECRET-VALUE" not in str(exc.value)


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


def test_decrypted_value_cached_per_key(public_key, tmp_path, monkeypatch):
    """Caching is ``EyamlBackend``'s job (per key, in the ``LookupContext``),
    not ``decrypt_string``'s own -- exercise it through a real ``Hiera``
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
    from hyera import _eyaml as eyaml_mod

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
