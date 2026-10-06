"""Key fixtures and PKCS7 envelope builders shared by the eyaml test modules."""

import base64
import os
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hyera.backends._eyaml import _load_private_key, decrypt_string

FIXTURE = (
    Path(__file__).resolve().parent / "conformance" / "cases" / "backend-eyaml-pkcs7"
)


PRIVATE_KEY_PATH = str(FIXTURE / "keys" / "private_key.pkcs7.pem")


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
    # zero serial number (a real eyaml fixture property), which newer `cryptography` releases
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


def _token(der: bytes) -> str:
    import base64

    return "ENC[PKCS7,{}]".format(base64.b64encode(der).decode("ascii"))


def _decrypt(token: str, options=None):
    opts = {"pkcs7_private_key": PRIVATE_KEY_PATH}
    if options:
        opts.update(options)
    return decrypt_string(token, opts, "k", "secrets.eyaml")
