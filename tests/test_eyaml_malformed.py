"""``eyaml_lookup_key`` on malformed PKCS7 blobs.

Only ``BackendError`` may surface, and its message holds no plaintext."""

import base64
import os

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric import padding

from hyera import BackendError
from hyera.backends._pkcs7 import _pkcs7_decrypt
from eyaml_support import (  # noqa: F401
    _AES_OIDS,
    _decrypt,
    _der_len,
    _envelope,
    _oid,
    _tlv,
    _token,
    private_key,
    public_key,
)


def _wrap_ci(env_body: bytes) -> bytes:
    """Wrap raw ``EnvelopedData`` content in the ``ContentInfo`` shell
    :func:`_pkcs7_decrypt` expects, without a real RSA/AES round trip."""
    env = _tlv(0x30, env_body)
    explicit = bytes([0xA0]) + _der_len(len(env)) + env
    return _tlv(0x30, _oid("1.2.840.113549.1.7.3") + explicit)


def _valid_ktri(pubkey) -> bytes:
    """A structurally valid ``KeyTransRecipientInfo`` (real RSA-encrypted key).

    For tests that must get past the recipient-info checks; the AES key is never
    used."""
    key = os.urandom(32)
    enc_key = pubkey.encrypt(key, padding.PKCS1v15())
    rid = _tlv(0x30, _tlv(0x30, b"") + _tlv(0x02, b"\x01"))
    key_alg = _tlv(0x30, _oid("1.2.840.113549.1.1.1") + _tlv(0x05, b""))
    return _tlv(0x30, _tlv(0x02, b"\x00") + rid + key_alg + _tlv(0x04, enc_key))


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
    """Seeded byte flips/truncations of a real envelope raise only ``BackendError``.

    A mutation that lands somewhere inert may decrypt cleanly; never
    ``IndexError``/``RecursionError``/a hang. Each iteration is a real RSA
    decryption, so the default run is short: set ``HYERA_FUZZ_ITERATIONS`` (e.g.
    5000) for a long run. The seed is fixed, so a longer run extends the sequence."""
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


# structural DER edge cases through _pkcs7_decrypt: each builds just enough of a
# ContentInfo/EnvelopedData shell (`_wrap_ci`/`_valid_ktri`, or raw bytes) to reach
# one structural check, so the BackendError message names the check that fired.


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
    # Only nested indefinite-length constructed values recurse through `_read_tlv`
    # (definite-length children are walked by `_children` at fixed depth), so pushing
    # `depth` past `_MAX_DEPTH` needs real nesting.
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
