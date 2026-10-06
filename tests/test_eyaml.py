"""``eyaml_lookup_key`` (PKCS7): token scanning, key precedence, and the
bounds-checked BER/DER decrypt path, against both the real fixture
ciphertext and hand-built synthetic envelopes covering encodings the
fixture itself does not exercise (AES-128/192, BER constructed/indefinite
content, a PKCS8 private key)."""

import base64
import os
import time
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hyera import BackendError, EyamlBackend
from hyera.backends._eyaml import (
    _decode64,
    _has_encrypted_token,
    _load_private_key,
    _pkcs7_decrypt,
    decrypt_string,
)
from hyera.backends import Backend

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


@pytest.fixture(scope="module")
def private_key():
    # `_pkcs7_decrypt` takes an already-loaded key object (`decrypt_string`
    # parses it once per call and reuses it across every token), not raw
    # PEM bytes -- the tests that exercise it directly need the same shape.
    with open(PRIVATE_KEY_PATH, "rb") as fh:
        return _load_private_key(fh.read())


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
    originator: bool = False,
) -> bytes:
    """Hand-build a minimal PKCS7 ``EnvelopedData`` ``ContentInfo`` blob
    encrypted to ``pubkey`` -- the encoder side of :func:`hyera.backends._eyaml.
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

    originator_info = _tlv(0xA0, b"") if originator else b""
    env_body = _tlv(0x02, b"\x00") + originator_info + rinfos + eci
    if indefinite:
        env = bytes([0x30, 0x80]) + env_body + b"\x00\x00"
    else:
        env = _tlv(0x30, env_body)

    explicit = bytes([0xA0]) + _der_len(len(env)) + env
    return _tlv(0x30, _oid("1.2.840.113549.1.7.3") + explicit)


def _wrap_ci(env_body: bytes) -> bytes:
    """Wrap raw ``EnvelopedData`` SEQUENCE content (``env_body``) in the
    ``ContentInfo``/``[0] EXPLICIT`` shell :func:`_pkcs7_decrypt` expects,
    without needing a full :func:`_envelope` (real RSA/AES) round trip --
    for structural-malformation tests that raise before ever touching a
    key or cipher."""
    env = _tlv(0x30, env_body)
    explicit = bytes([0xA0]) + _der_len(len(env)) + env
    return _tlv(0x30, _oid("1.2.840.113549.1.7.3") + explicit)


def _valid_ktri(pubkey) -> bytes:
    """A structurally valid ``KeyTransRecipientInfo`` (real RSA-encrypted
    key, real ``rsaEncryption`` algorithm OID) for tests that need parsing
    to get past the recipient-info checks before hitting a later
    malformation -- the AES key itself is never used since these tests
    always raise before decrypting any content."""
    key = os.urandom(32)
    enc_key = pubkey.encrypt(key, padding.PKCS1v15())
    rid = _tlv(0x30, _tlv(0x30, b"") + _tlv(0x02, b"\x01"))
    key_alg = _tlv(0x30, _oid("1.2.840.113549.1.1.1") + _tlv(0x05, b""))
    return _tlv(0x30, _tlv(0x02, b"\x00") + rid + key_alg + _tlv(0x04, enc_key))


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
    # hiera-eyaml's own eyaml_lookup_key.rb wraps a single rescue around
    # parsing *and* decrypting the entire stored value, and interpolates
    # its own pre-decrypt argument (the whole value) into the message --
    # never just the one token that failed, even when surrounded by other
    # text. Confirmed against the WSL hiera-eyaml 5.0.1 oracle (the
    # backend-eyaml-pkcs7 conformance case's own corrupt_prefixed query).
    data = "prefix ENC[PKCS7,aGVsbG8=] suffix"
    with pytest.raises(BackendError) as exc:
        _decrypt(data)
    message = str(exc.value)
    assert "decrypting {} when looking up".format(data) in message
    assert "decrypting ENC[PKCS7,aGVsbG8=] when looking up" not in message


def test_bad_key_is_reported_before_the_ciphertext_is_ever_parsed(tmp_path):
    # Ruby's own Pkcs7.decrypt loads and parses the private key before it
    # ever touches the ciphertext (pkcs7.rb's `decrypt`): a malformed key
    # and a malformed ciphertext together must report the key problem,
    # never "Could not parse the PKCS7" -- confirmed against the WSL
    # hiera-eyaml 5.0.1 oracle, whose text ends "Error was Neither PUB key
    # nor PRIV key" (Ruby's OpenSSL binding; see the
    # backend-eyaml-pkcs7-bad-key conformance case).
    bad_key_path = tmp_path / "garbage.pem"
    bad_key_path.write_text("this is not a pem key at all\n", encoding="utf-8")
    with pytest.raises(
        BackendError, match="Error was Neither PUB key nor PRIV key$"
    ) as exc:
        _decrypt("ENC[PKCS7,aGVsbG8=]", {"pkcs7_private_key": str(bad_key_path)})
    assert "Could not parse the PKCS7" not in str(exc.value)


def test_private_key_parsed_once_per_decrypt_string_call(public_key, monkeypatch):
    # hiera-eyaml's own Pkcs7.decrypt re-parses the key fresh for every
    # single ENC[...] token (~50x the Python `cryptography` cost per parse,
    # measured separately); this reuses one parse across every token in the
    # same value instead, with no behavior change (a value's tokens all
    # share the same configured key within one call).
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
    # The per-call reuse above must never become a cross-call cache: the
    # configured key can change between separate lookups (e.g. a
    # pkcs7_private_key_env_var whose value changes), so a second,
    # independent decrypt_string call parses it again.
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


# --- structural DER edge cases, hit directly through _pkcs7_decrypt --------
#
# Each of these builds just enough of a ContentInfo/EnvelopedData shell (via
# `_wrap_ci`/`_valid_ktri`, or raw bytes for the reader-level cases) to reach
# one specific structural check in the decoder and no further, so the
# resulting `BackendError` message pins down exactly which check fired.


def test_tag_with_no_length_byte(private_key):
    # A single tag byte with nothing after it: `_read_length` has no byte
    # left to read at all.
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: truncated length"
    ):
        _pkcs7_decrypt(bytes([0x30]), private_key)


def test_long_form_length_missing_bytes(private_key):
    # 0x82 says two more length octets follow; only one is actually present.
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: truncated length"
    ):
        _pkcs7_decrypt(bytes([0x30, 0x82, 0x01]), private_key)


def test_empty_der_is_truncated_tag(private_key):
    with pytest.raises(BackendError, match="Could not parse the PKCS7: truncated tag"):
        _pkcs7_decrypt(b"", private_key)


def test_indefinite_length_on_primitive_tag_rejected(private_key):
    # 0x04 (OCTET STRING) is primitive; an indefinite-length primitive is
    # illegal in BER.
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: indefinite length on a primitive value",
    ):
        _pkcs7_decrypt(bytes([0x04, 0x80]), private_key)


def test_indefinite_length_content_runs_out_before_terminator(private_key):
    # A constructed, indefinite-length SEQUENCE containing one NULL child
    # and then nothing -- no `00 00` End-of-Contents ever arrives.
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: truncated indefinite-length content",
    ):
        _pkcs7_decrypt(bytes([0x30, 0x80, 0x05, 0x00]), private_key)


def test_deeply_nested_indefinite_length_rejected(private_key):
    # Only nested indefinite-length constructed values recurse through
    # `_read_tlv` itself (a definite-length SEQUENCE's children are walked
    # by `_children` at a fixed, hand-picked depth instead), so this needs
    # genuine nesting, not just many sibling elements, to push `depth` past
    # `_MAX_DEPTH`.
    node = _tlv(0x05, b"")  # innermost: a definite-length NULL
    for _ in range(40):
        node = bytes([0x30, 0x80]) + node + b"\x00\x00"
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: nesting too deep"
    ):
        _pkcs7_decrypt(node, private_key)


def test_empty_object_identifier_rejected(private_key):
    der = _tlv(0x30, _tlv(0x06, b"") + _tlv(0xA0, b""))
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: empty OBJECT IDENTIFIER"
    ):
        _pkcs7_decrypt(der, private_key)


def test_content_info_with_single_child_rejected(private_key):
    der = _tlv(0x30, _oid("1.2.840.113549.1.7.3"))
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: malformed ContentInfo"
    ):
        _pkcs7_decrypt(der, private_key)


def test_enveloped_data_with_too_few_children_rejected(private_key):
    # Just CMSVersion: recipientInfos and encryptedContentInfo are missing.
    der = _wrap_ci(_tlv(0x02, b"\x00"))
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: malformed EnvelopedData"
    ):
        _pkcs7_decrypt(der, private_key)


def test_empty_recipient_infos_rejected(private_key):
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0x31, b"") + _tlv(0x30, b""))
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: no recipient in recipientInfos",
    ):
        _pkcs7_decrypt(der, private_key)


def test_primitive_recipient_info_rejected(private_key):
    der = _wrap_ci(
        _tlv(0x02, b"\x00")
        + _tlv(0x31, _tlv(0x02, b"\x01"))  # a primitive INTEGER, not constructed
        + _tlv(0x30, b"")
    )
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: malformed RecipientInfo"
    ):
        _pkcs7_decrypt(der, private_key)


def test_key_trans_recipient_info_with_too_few_children_rejected(private_key):
    ktri = _tlv(0x30, _tlv(0x02, b"\x00") + _tlv(0x02, b"\x01"))
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0x31, ktri) + _tlv(0x30, b""))
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: malformed KeyTransRecipientInfo",
    ):
        _pkcs7_decrypt(der, private_key)


def test_originator_info_present_but_no_encrypted_content_info(public_key, private_key):
    # originatorInfo present, then only recipientInfos -- nothing left over
    # for encryptedContentInfo.
    ktri = _valid_ktri(public_key)
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0xA0, b"") + _tlv(0x31, ktri))
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: malformed EnvelopedData"
    ):
        _pkcs7_decrypt(der, private_key)


def test_primitive_encrypted_content_info_rejected(public_key, private_key):
    ktri = _valid_ktri(public_key)
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0x31, ktri) + _tlv(0x04, b""))
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: malformed EncryptedContentInfo",
    ):
        _pkcs7_decrypt(der, private_key)


def test_encrypted_content_info_with_too_few_children_rejected(public_key, private_key):
    ktri = _valid_ktri(public_key)
    eci = _tlv(0x30, _oid("1.2.840.113549.1.7.1"))  # contentType only
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0x31, ktri) + eci)
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: malformed EncryptedContentInfo",
    ):
        _pkcs7_decrypt(der, private_key)


def test_empty_content_encryption_algorithm_rejected(public_key, private_key):
    ktri = _valid_ktri(public_key)
    eci = _tlv(
        0x30,
        _oid("1.2.840.113549.1.7.1") + _tlv(0x30, b"") + _tlv(0x04, b""),
    )
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0x31, ktri) + eci)
    with pytest.raises(
        BackendError,
        match="Could not parse the PKCS7: malformed contentEncryptionAlgorithm",
    ):
        _pkcs7_decrypt(der, private_key)


def test_wrong_iv_length_rejected(public_key, private_key):
    ktri = _valid_ktri(public_key)
    content_alg = _tlv(0x30, _oid(_AES_OIDS[256]) + _tlv(0x04, b"\x01\x02\x03"))
    eci = _tlv(
        0x30,
        _oid("1.2.840.113549.1.7.1") + content_alg + _tlv(0x04, b""),
    )
    der = _wrap_ci(_tlv(0x02, b"\x00") + _tlv(0x31, ktri) + eci)
    with pytest.raises(
        BackendError, match="Could not parse the PKCS7: malformed AES-CBC IV"
    ):
        _pkcs7_decrypt(der, private_key)


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


def test_enc_token_with_whitespace_only_body_is_malformed():
    # `_TOKEN_RE`'s body charclass allows a bare space/newline, so a body of
    # only whitespace matches it -- but stripping that whitespace before
    # re-matching against `_STRIPPED_RE` (which requires at least one real
    # base64 character) leaves nothing, and that re-match fails.
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


# --- hardening against adversarial input -------------------------------------
# `.*ENC\[.*?\]` was cubic under Python's backtracking `re` for a value
# that is mostly unterminated `ENC[` prefixes; `_has_encrypted_token` is a
# linear line-at-a-time replacement.


def test_no_token_present_returns_unchanged_fast():
    huge = "ENC[" * 250000
    t0 = time.perf_counter()
    assert decrypt_string(huge, {}, "k", "p") == huge
    assert time.perf_counter() - t0 < 1.0


def test_has_encrypted_token_matches_original_regex_on_random_strings():
    import random
    import re

    original_presence_check = re.compile(r".*ENC\[.*?\]")
    rng = random.Random(1)
    alphabet = ["E", "N", "C", "[", "]", "\n", "x", "ENC[", "]"]
    for _ in range(20000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
        assert bool(original_presence_check.search(s)) == _has_encrypted_token(s), repr(
            s
        )


# --- The OID reader built an unbounded Python int per sub-identifier,
# quadratic in the OID's byte length; `_oid` now caps content length.


def test_oid_length_is_bounded(private_key):
    huge_oid = b"\x2a" + b"\xff" * 60000 + b"\x01"
    der = _tlv(0x30, _oid_bytes_raw(huge_oid) + _tlv(0xA0, b""))
    t0 = time.perf_counter()
    with pytest.raises(BackendError, match="Could not parse the PKCS7"):
        _pkcs7_decrypt(der, private_key)
    assert time.perf_counter() - t0 < 1.0


def _oid_bytes_raw(content: bytes) -> bytes:
    return _tlv(0x06, content)


# --- A non-string `pkcs7_private_key` must never be opened as a file
# descriptor (`os.path.exists`/`open` both accept an int fd).


def test_non_string_key_path_int_never_opens_fd(public_key):
    der = _envelope(b"S", public_key)
    tok = _token(der)
    f = open(PRIVATE_KEY_PATH, "rb")
    fd = f.fileno()
    try:
        with pytest.raises(
            BackendError, match="no implicit conversion of Integer into String"
        ):
            decrypt_string(tok, {"pkcs7_private_key": fd}, "k", "p")
        os.fstat(fd)  # still open: not closed by being misread as an fd
    finally:
        f.close()


def test_non_string_key_path_bool_rejected(public_key):
    der = _envelope(b"S", public_key)
    tok = _token(der)
    with pytest.raises(
        BackendError, match="no implicit conversion of TrueClass into String"
    ):
        decrypt_string(tok, {"pkcs7_private_key": True}, "k", "p")


# --- An OSError opening/reading the key file (a directory, a
# permission error) must be wrapped, never left raw.


def test_key_path_directory_is_wrapped(tmp_path, public_key):
    der = _envelope(b"directory-key", public_key)
    tok = _token(der)
    with pytest.raises(BackendError):
        decrypt_string(tok, {"pkcs7_private_key": str(tmp_path)}, "k", "p")


# --- `_decode64` must tolerate non-alphabet characters the way Ruby's
# `Base64.decode64` does, instead of letting `binascii.Error` escape raw.


def test_decode64_strips_non_alphabet_characters():
    assert _decode64("QU-JD") == base64.b64decode("QUJD")
    # Never raises, however unlike the "real" base64 alphabet this is:
    _decode64("not base64 at all!!")


def test_decode64_drops_single_dangling_character():
    # After alphabet-filtering, a length of 4n+1 has no valid re-padding
    # (only 4n, 4n+2 or 4n+3 do); Ruby's decoder silently drops the last
    # character rather than raising, and so does this one.
    assert _decode64("QUJDA") == b"ABC"


@pytest.mark.parametrize("garbage", ["QU-JD", "not base64 at all!!"])
def test_b64_env_var_garbage_is_wrapped_not_raw_binascii_error(
    monkeypatch, public_key, garbage
):
    der = _envelope(b"S", public_key)
    tok = _token(der)
    monkeypatch.setenv("HYERA_TEST_B64_GARBAGE", garbage)
    with pytest.raises(BackendError):
        decrypt_string(
            tok, {"pkcs7_b64_private_key_env_var": "HYERA_TEST_B64_GARBAGE"}, "k", "p"
        )


# --- No decrypted plaintext or private-key PEM reachable via
# `exc.__context__`'s traceback frame locals, even though `raise ... from
# None` alone does not clear `__context__`.


def _walk_for_secrets(exc, markers):
    seen = set()
    stack = [exc]
    hits = []
    while stack:
        obj = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, BaseException):
            for attr in ("__cause__", "__context__"):
                v = getattr(obj, attr)
                if v is not None:
                    stack.append(v)
            tb = obj.__traceback__
            while tb is not None:
                frame = tb.tb_frame
                for name, value in frame.f_locals.items():
                    if isinstance(value, (bytes, str)):
                        blob = (
                            value
                            if isinstance(value, bytes)
                            else value.encode("utf-8", "replace")
                        )
                        for marker in markers:
                            if marker in blob:
                                hits.append((frame.f_code.co_name, name))
                    if isinstance(value, BaseException):
                        stack.append(value)
                tb = tb.tb_next
    return hits


def test_no_secrets_reachable_via_context_after_partial_success(public_key):
    good_der = _envelope(b"GOOD-SECRET-VALUE", public_key)
    good_token = _token(good_der)
    bad_token = "ENC[PKCS7,aGVsbG8=]"
    data = "prefix {} mid {} end".format(good_token, bad_token)
    with pytest.raises(BackendError) as exc:
        _decrypt(data)
    hits = _walk_for_secrets(exc.value, (b"PRIVATE KEY", b"GOOD-SECRET-VALUE"))
    assert hits == []


def _write_wrong_key_pem(path):
    # A helper frame, not the test's own: it returns (and its locals, the
    # actual PEM bytes, go away) before `_decrypt` is ever called, so only
    # `hyera` code's own frames are on the raised exception's traceback.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(other_pem)


def test_no_secrets_reachable_via_context_after_wrong_key(public_key, tmp_path):
    other_path = tmp_path / "wrong_key.pem"
    _write_wrong_key_pem(other_path)
    der = _envelope(b"wrong-key-secret", public_key)
    with pytest.raises(BackendError) as exc:
        _decrypt(_token(der), {"pkcs7_private_key": str(other_path)})
    hits = _walk_for_secrets(exc.value, (b"PRIVATE KEY",))
    assert hits == []


def test_no_secrets_reachable_via_context_after_bad_padding(public_key, private_key):
    base_der = bytearray(_envelope(b"x" * 64, public_key))
    with open(PRIVATE_KEY_PATH, "rb") as fh:
        key_pem = fh.read()
    # A single-bit flip in the last ciphertext byte scrambles the whole
    # final AES block once it goes through CBC decryption (ordinary block
    # cipher diffusion), which almost always leaves invalid PKCS7 padding
    # behind -- but `_envelope` picks a fresh random AES key and IV on
    # every call, so which flip value actually does that is not fixed:
    # about 1 in 256 draws the garbled block coincidentally still looks
    # like a valid one-byte pad, `_pkcs7_decrypt` returns garbage instead
    # of raising, and the failure only then surfaces higher up as a UTF-8
    # decode error -- which this test does not expect, so it flakes.
    # Search deterministically for a flip that reproduces the padding
    # failure against *this* run's random key/IV, instead of trusting a
    # single fixed guess (`^= 1`) to land on one.
    for flip in range(1, 256):
        der = bytearray(base_der)
        der[-1] ^= flip
        try:
            _pkcs7_decrypt(bytes(der), private_key)
        except BackendError as e:
            if "bad decrypt" in str(e):
                break
    else:
        pytest.fail("no single-byte corruption reproduced a padding failure")
    # Scrub this frame's own PEM copy before the assertion under test: once
    # `_decrypt` raises, `_walk_for_secrets` inspects every frame on the
    # exception's traceback, including this one, and `key_pem` would
    # otherwise still be a live local holding "...PRIVATE KEY...".
    key_pem = b""
    with pytest.raises(BackendError, match="bad decrypt") as exc:
        _decrypt(_token(bytes(der)))
    hits = _walk_for_secrets(exc.value, (b"PRIVATE KEY",))
    assert hits == []


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
