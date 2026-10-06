# Ported from hiera-eyaml lib/hiera/backend/eyaml/encryptors/pkcs7.rb
# (https://github.com/voxpupuli/hiera-eyaml), MIT. Modified by jose-pr.
# See NOTICE.
"""A cert-free PKCS7 EnvelopedData decrypt over ``cryptography`` primitives.

Follows hiera-eyaml's ``encryptors/pkcs7.rb``, with a bounds-checked BER/DER
reader in place of OpenSSL's PKCS7 parser.
"""

from __future__ import annotations

from ..exceptions import BackendError

#: OIDs this decrypt path understands (``pkcs7.rb`` + AES content encryption).
_ENVELOPED_DATA_OID = "1.2.840.113549.1.7.3"
_RSA_ENCRYPTION_OID = "1.2.840.113549.1.1.1"
_AES_CBC_OIDS = {
    "2.16.840.1.101.3.4.1.2": 128,
    "2.16.840.1.101.3.4.1.22": 192,
    "2.16.840.1.101.3.4.1.42": 256,
}

_MAX_DEPTH = 32


class _DerError(Exception):
    """An internal PKCS7/DER parse problem; always caught and turned into
    ``BackendError("Could not parse the PKCS7: <detail>")`` before it can
    escape this module."""


# --- a small, bounds-checked BER/DER reader --------------------------------


def _read_length(buf: bytes, i: int):
    """Returns ``(length_or_None, next_index, indefinite)``. ``length`` is
    ``None`` only when ``indefinite`` is true."""
    if i >= len(buf):
        raise _DerError("truncated length")
    first = buf[i]
    i += 1
    if not (first & 0x80):
        return first, i, False
    n = first & 0x7F
    if n == 0:
        return None, i, True
    if n > 8:
        raise _DerError("length field too large")
    if i + n > len(buf):
        raise _DerError("truncated length")
    length = int.from_bytes(buf[i : i + n], "big")
    i += n
    return length, i, False


def _read_tlv(buf: bytes, i: int, depth: int):
    """Reads one TLV (definite or indefinite length) starting at ``i``.

    Returns ``(tag, content_bytes, constructed, next_index)``. For an
    indefinite-length constructed value, ``content_bytes`` is the raw bytes
    between the header and the terminating ``00 00`` (End-of-Contents) --
    itself a valid sequence of child TLVs, exactly like a definite-length
    constructed value's content, so :func:`_children` handles both
    uniformly. Every length is checked against ``len(buf)`` before slicing;
    nesting past :data:`_MAX_DEPTH` raises rather than recursing further.
    """
    if depth > _MAX_DEPTH:
        raise _DerError("nesting too deep")
    if i >= len(buf):
        raise _DerError("truncated tag")
    tag = buf[i]
    i += 1
    constructed = bool(tag & 0x20)
    length, i, indefinite = _read_length(buf, i)
    if not indefinite:
        if length < 0 or i + length > len(buf):
            raise _DerError("truncated content")
        return tag, buf[i : i + length], constructed, i + length
    if not constructed:
        raise _DerError("indefinite length on a primitive value")
    start = i
    while True:
        if i + 2 > len(buf):
            raise _DerError("truncated indefinite-length content")
        if buf[i] == 0 and buf[i + 1] == 0:
            return tag, buf[start:i], constructed, i + 2
        _t, _v, _c, i = _read_tlv(buf, i, depth + 1)


def _children(buf: bytes, depth: int):
    """Every sibling TLV in ``buf``, as ``(tag, content, constructed)``."""
    i, out = 0, []
    n = len(buf)
    while i < n:
        tag, value, constructed, i = _read_tlv(buf, i, depth)
        out.append((tag, value, constructed))
    return out


#: Every OID this decrypt path recognizes (``_ENVELOPED_DATA_OID``,
#: ``_RSA_ENCRYPTION_OID``, ``_AES_CBC_OIDS``) fits in 16 bytes; 32 leaves headroom.
#: Unbounded, building ``parts`` is quadratic in the OID's length (100 KB took 0.68s).
_MAX_OID_BYTES = 32


def _oid(value: bytes) -> str:
    if not value:
        raise _DerError("empty OBJECT IDENTIFIER")
    if len(value) > _MAX_OID_BYTES:
        raise _DerError("OBJECT IDENTIFIER too long")
    parts = [value[0] // 40, value[0] % 40]
    n = 0
    for c in value[1:]:
        n = (n << 7) | (c & 0x7F)
        if not (c & 0x80):
            parts.append(n)
            n = 0
    if n:
        raise _DerError("truncated OBJECT IDENTIFIER")
    return ".".join(str(p) for p in parts)


def _pkcs7_decrypt(der: bytes, private_key) -> bytes:
    """Decrypt a PKCS7 ``EnvelopedData`` blob with only the recipient's
    already-loaded private key (no certificate) -- eyaml's own
    encrypt-to-one-recipient shape. Every structural problem raises
    ``BackendError("Could not parse the PKCS7: <detail>")``; a key or
    cipher problem raises ``BackendError("Could not decrypt the PKCS7:
    <detail>")`` or ``BackendError("bad decrypt")`` for anything that is,
    or could be, a wrong-key/garbled-ciphertext symptom (OpenSSL's own
    text for exactly that case) -- never a distinguishable error for a
    length mismatch versus a padding failure versus an implicit-rejection
    "succeeded with garbage".

    ``private_key`` is owned by the caller (:func:`decrypt_string` reuses
    it across every token in one value) and is never cleared here -- only
    this call's own ``key``/``padded`` locals are. On every exit path
    those are overwritten before this function returns or its exception
    leaves the frame: a ``raise ... from None`` inside an ``except`` block
    still sets ``__context__`` to the exception being handled (``from
    None`` only suppresses it in *printed* tracebacks), and that inner
    exception's own traceback frame is this same frame -- so without this,
    the real plaintext of every AES block before a tampered one (on an
    unpadding failure) would stay reachable from the exception a caller
    sees, through ``__context__.__traceback__``.
    """
    from cryptography.hazmat.primitives import padding as sympad
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = None
    padded = b""
    try:
        try:
            _ci_tag, ci_content, _ci_constructed, _end = _read_tlv(der, 0, 0)
            ci_children = _children(ci_content, 1)
            if len(ci_children) < 2:
                raise _DerError("malformed ContentInfo")
            if _oid(ci_children[0][1]) != _ENVELOPED_DATA_OID:
                raise _DerError("not EnvelopedData")

            # ci_children[1] is the [0] EXPLICIT wrapper around EnvelopedData.
            _explicit_tag, explicit_value, explicit_constructed = ci_children[1]
            if not explicit_constructed:
                raise _DerError("malformed ContentInfo content")
            _env_tag, env_content, _env_constructed, _ = _read_tlv(explicit_value, 0, 1)
            env_children = _children(env_content, 2)
            if len(env_children) < 3:
                raise _DerError("malformed EnvelopedData")

            idx = 1  # env_children[0] is CMSVersion.
            rinfos_tag, rinfos_value, _ = env_children[idx]
            if rinfos_tag == 0xA0:  # optional [0] originatorInfo
                # No bounds check: the `len(env_children) < 3` guard above guarantees 3
                # elements and `idx` only reaches 2 in this branch.
                idx += 1
                rinfos_tag, rinfos_value, _ = env_children[idx]
            if rinfos_tag != 0x31:  # SET OF RecipientInfo
                raise _DerError("missing recipientInfos")
            rinfo_list = _children(rinfos_value, 3)
            if not rinfo_list:
                raise _DerError("no recipient in recipientInfos")

            # eyaml always encrypts to exactly one recipient: use the first.
            _ktri_tag, ktri_value, ktri_constructed = rinfo_list[0]
            if not ktri_constructed:
                raise _DerError("malformed RecipientInfo")
            ktri_children = _children(ktri_value, 4)
            if len(ktri_children) < 4:
                raise _DerError("malformed KeyTransRecipientInfo")
            _keyalg_tag, keyalg_value, _ = ktri_children[2]
            keyalg_children = _children(keyalg_value, 5)
            if (
                not keyalg_children
                or _oid(keyalg_children[0][1]) != _RSA_ENCRYPTION_OID
            ):
                raise _DerError("unsupported key-encryption algorithm")
            _enc_key_tag, enc_key, _ = ktri_children[3]

            idx += 1
            if idx >= len(env_children):
                raise _DerError("malformed EnvelopedData")
            _eci_tag, eci_value, eci_constructed = env_children[idx]
            if not eci_constructed:
                raise _DerError("malformed EncryptedContentInfo")
            eci_children = _children(eci_value, 3)
            if len(eci_children) < 3:
                raise _DerError("malformed EncryptedContentInfo")
            _content_alg_tag, content_alg_value, _ = eci_children[1]
            alg_children = _children(content_alg_value, 4)
            if not alg_children:
                raise _DerError("malformed contentEncryptionAlgorithm")
            content_oid = _oid(alg_children[0][1])
            bits = _AES_CBC_OIDS.get(content_oid)
            if bits is None:
                raise BackendError(
                    "Could not decrypt the PKCS7: unsupported algorithm {}".format(
                        content_oid
                    )
                )
            if len(alg_children) < 2 or len(alg_children[1][1]) != 16:
                raise _DerError("malformed AES-CBC IV")
            iv = alg_children[1][1]

            (
                _enc_content_tag,
                enc_content_value,
                enc_content_constructed,
            ) = eci_children[2]
            if enc_content_constructed:
                content = b"".join(v for _t, v, _c in _children(enc_content_value, 4))
            else:
                content = enc_content_value
        except _DerError as e:
            raise BackendError("Could not parse the PKCS7: {}".format(e)) from None
        except (IndexError, ValueError) as e:
            # Defense in depth, unreachable as written: every subscript above follows a
            # length check raising `_DerError`, and the readers bounds-check `i`. A
            # future unguarded index becomes this `BackendError`, not an `IndexError`.
            raise BackendError("Could not parse the PKCS7: {}".format(e)) from None

        try:
            key = private_key.decrypt(enc_key, padding.PKCS1v15())
        except Exception:
            raise BackendError("bad decrypt") from None
        if len(key) * 8 != bits:
            # RSA PKCS#1 v1.5 decrypt uses implicit rejection: a wrong key
            # does not raise, it returns a garbage value of unpredictable
            # length.
            raise BackendError("bad decrypt")

        try:
            decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            padded = decryptor.update(content) + decryptor.finalize()
            unpadder = sympad.PKCS7(algorithms.AES.block_size).unpadder()
            plaintext = unpadder.update(padded) + unpadder.finalize()
        except Exception:
            raise BackendError("bad decrypt") from None
        return plaintext
    finally:
        # Scrub this call's locals that ever held decrypted bytes, on every exit path
        # (see the docstring above for why `from None` alone does not). `private_key` is
        # the caller's, reused across tokens, and never touched.
        key = None
        padded = b""
