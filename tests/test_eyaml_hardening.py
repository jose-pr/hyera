"""``eyaml_lookup_key`` hardening against adversarial input, key paths and secrets reachable from the context."""

import base64
import os
import time

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from hyera import BackendError
from hyera.backends._eyaml import _decode64, _has_encrypted_token, decrypt_string
from hyera.backends._pkcs7 import _pkcs7_decrypt
from eyaml_support import (  # noqa: F401
    PRIVATE_KEY_PATH,
    _decrypt,
    _envelope,
    _tlv,
    _token,
    private_key,
    public_key,
)

# hardening against adversarial input: `.*ENC\[.*?\]` is cubic under backtracking
# `re` on a value of unterminated `ENC[` prefixes; `_has_encrypted_token` is linear.


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
    # Flipping a bit in the last ciphertext byte scrambles the final AES block, which
    # usually leaves invalid PKCS7 padding; `_envelope` draws a fresh key and IV per
    # call, so about 1 in 256 flips still looks like a valid pad and surfaces as a
    # UTF-8 error instead. Search for a flip that breaks the padding for this run's
    # key and IV rather than a fixed `^= 1`.
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
    # Scrub this frame's PEM copy first: when `_decrypt` raises, `_walk_for_secrets`
    # inspects every traceback frame, and `key_pem` would still hold the private key.
    key_pem = b""
    with pytest.raises(BackendError, match="bad decrypt") as exc:
        _decrypt(_token(bytes(der)))
    hits = _walk_for_secrets(exc.value, (b"PRIVATE KEY",))
    assert hits == []
