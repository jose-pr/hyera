"""``eyaml_lookup_key`` support: token scanning, PKCS7 key loading and a
cert-free PKCS7 EnvelopedData decrypt over ``cryptography`` primitives.

Original code (no phiera/Puppet header): the token grammar mirrors
hiera-eyaml's ``parser/encrypted_tokens.rb``/``parser/parser.rb`` and the
PKCS7 handling mirrors ``encryptors/pkcs7.rb``, but neither is translated
line by line -- both are re-implemented against Python's stdlib/`re`/
`cryptography` idioms, so this file matches the upstream *behaviour*
without being a structural port of it, and carries no "Ported from ..."
header.
"""

import base64
import logging
import os
import re

from .exceptions import BackendError

_LOGGER = logging.getLogger(__name__)

#: hiera-eyaml's own regexes (``encrypted_tokens.rb:109-128``), `\w` made
#: ASCII to match Ruby's default (non-Unicode) `\w` in these patterns.
_TOKEN_RE = re.compile(r"ENC\[([A-Za-z0-9_]+,)?([a-zA-Z0-9+/ =\n]+?)\]")
_STRIPPED_RE = re.compile(r"ENC\[([A-Za-z0-9_]+,)?([a-zA-Z0-9+/=]+?)\]")

_WHITESPACE_RE = re.compile(r"\s")

#: Ruby's ``Base64.decode64`` (``unpack1('m')``) silently drops any byte
#: outside the base64 alphabet instead of raising -- garbage in, garbage
#: (never an exception) out; the resulting bytes still have to pass
#: ``load_pem_private_key`` before they can become a usable key.
_B64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)

#: OIDs this decrypt path understands (``pkcs7.rb`` + AES content encryption).
_ENVELOPED_DATA_OID = "1.2.840.113549.1.7.3"
_RSA_ENCRYPTION_OID = "1.2.840.113549.1.1.1"
_AES_CBC_OIDS = {
    "2.16.840.1.101.3.4.1.2": 128,
    "2.16.840.1.101.3.4.1.22": 192,
    "2.16.840.1.101.3.4.1.42": 256,
}

_MAX_DEPTH = 32

#: hiera-eyaml decides whether a value needs decrypting at all with
#: ``/.*ENC\[.*?\]/`` -- fine under Ruby/Onigmo (linear), but under
#: Python's backtracking ``re`` the leading ``.*`` makes a run of unmatched
#: ``ENC[`` prefixes cubic (a stored value that is mostly ``"ENC[" * n``
#: with no closing ``]`` took Python over 300s past ~2500 repeats, measured
#: on the request this file exists to serve, versus Ruby's ~4ms at 10x that
#: length). This is the same *presence* test, done a line at a time in
#: linear time with no regex at all: exactly equivalent to the original
#: search on every input (verified against 200k random strings in the
#: reviewing session), since neither ever crosses a line boundary between
#: the ``ENC[`` and its ``]`` (``_TOKEN_RE``'s own body charclass excludes
#: bare newlines between distinct tokens the same way).


def _has_encrypted_token(data: str) -> bool:
    for line in data.split("\n"):
        i = line.find("ENC[")
        if i != -1 and "]" in line[i + 4 :]:
            return True
    return False


class _DerError(Exception):
    """An internal PKCS7/DER parse problem; always caught and turned into
    ``BackendError("Could not parse the PKCS7: <detail>")`` before it can
    escape this module."""


def check_cryptography() -> None:
    """Raise :class:`~hyera.BackendError` with the install hint if the
    optional ``cryptography`` package is missing. Called once per
    decrypt/key-load so the hint is always current, never cached."""
    try:
        import cryptography.hazmat.primitives.asymmetric.padding  # noqa: F401
        import cryptography.hazmat.primitives.ciphers  # noqa: F401
        import cryptography.hazmat.primitives.padding  # noqa: F401
        import cryptography.hazmat.primitives.serialization  # noqa: F401
    except ImportError as e:
        raise BackendError(
            "eyaml_lookup_key requires the optional 'cryptography' package: "
            'pip install "hyera[eyaml]"'
        ) from e


def _decode64(text: str) -> bytes:
    """Ruby ``Base64.decode64``'s leniency: stop at the first ``=`` run (a
    real base64 payload never has data after its padding), drop every
    character outside the base64 alphabet (Ruby's decoder ignores them
    rather than raising -- this also covers ``*_b64_private_key_env_var``,
    which reaches here straight from the environment with no token-regex
    filtering at all), drop a single dangling character when the remainder
    isn't a multiple of 4 (also silently ignored by Ruby), then re-pad and
    decode. Never raises: the alphabet filter above means
    :func:`base64.b64decode` never sees a character it would reject."""
    text = "".join(text.split())
    eq = text.find("=")
    if eq != -1:
        text = text[:eq]
    text = "".join(c for c in text if c in _B64_ALPHABET)
    remainder = len(text) % 4
    if remainder == 1:
        text = text[:-1]
        remainder = 0
    if remainder:
        text += "=" * (4 - remainder)
    return base64.b64decode(text)


#: Ruby class names for the ``TypeError`` text a non-``String`` argument
#: gets from ``File.exist?``/``File.read`` (``"no implicit conversion of
#: <Class> into String"``) -- only the YAML scalar shapes a hierarchy
#: option can actually hold.
_RUBY_CLASS_NAMES = {
    bool: None,  # handled specially: True/False -> TrueClass/FalseClass
    int: "Integer",
    float: "Float",
    list: "Array",
    dict: "Hash",
}


def _ruby_class_name(value) -> str:
    if isinstance(value, bool):
        return "TrueClass" if value else "FalseClass"
    return _RUBY_CLASS_NAMES.get(type(value), type(value).__name__)


def _private_key_pem(options: dict) -> bytes:
    """The PKCS7 private key bytes, by hiera-eyaml's own precedence
    (``pkcs7.rb:99-125``): ``pkcs7_private_key_env_var`` >
    ``pkcs7_b64_private_key_env_var`` > ``pkcs7_private_key`` (a path
    relative to the process cwd) > "is not defined". A warning is logged
    (never raised) when both an env-var option and the plain file option
    are set -- the env var wins silently otherwise. ``pkcs7_public_key*``
    options are never read: this decrypt path needs only the private key.
    A truthy, non-``String`` ``pkcs7_private_key`` (an ``Integer``/
    ``Boolean`` from YAML) raises before any file operation -- never opened
    as a file descriptor. An ``OSError`` opening or reading the key file
    (a directory, a permission error) is wrapped, never left raw.
    """
    env_name = options.get("pkcs7_private_key_env_var")
    if env_name:
        if options.get("pkcs7_private_key"):
            _LOGGER.warning(
                "[pkcs7] both private_key and private_key_env_var specified, "
                "using private_key_env_var"
            )
        value = os.environ.get(env_name)
        if value is None:
            raise BackendError("env {} is not set".format(env_name))
        return value.encode("utf-8")

    b64_env_name = options.get("pkcs7_b64_private_key_env_var")
    if b64_env_name:
        value = os.environ.get(b64_env_name)
        if value is None:
            raise BackendError("env {} is not set".format(b64_env_name))
        return _decode64(value)

    path = options.get("pkcs7_private_key")
    if path:
        if not isinstance(path, (str, os.PathLike)):
            # `os.path.exists`/`open` both accept an int as an already-open
            # file descriptor (and Windows/POSIX `open` on `True`/`False`
            # coerces to fd 1/0) -- reading and then closing a host fd the
            # data author never named is never acceptable. Puppet's own
            # `File.exist?(3)` raises this same TypeError text before ever
            # touching a descriptor.
            raise BackendError(
                "no implicit conversion of {} into String".format(
                    _ruby_class_name(path)
                )
            )
        if not os.path.exists(path):
            raise BackendError("file {} does not exist".format(path))
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError as e:
            raise BackendError("{} - {}".format(e.strerror, path)) from None

    raise BackendError("pkcs7_private_key is not defined")


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


#: Every OID this decrypt path ever needs to recognize
#: (``_ENVELOPED_DATA_OID``/``_RSA_ENCRYPTION_OID``/the three
#: ``_AES_CBC_OIDS``) encodes in well under 16 bytes; 32 leaves generous
#: headroom for a legitimate-but-unrecognized algorithm OID while still
#: rejecting the pathological case outright. Without this bound, building
#: ``parts`` costs one Python bigint shift-and-mask per content byte with
#: no ceiling on the resulting integer's size -- quadratic in the OID's
#: byte length (a 100 KB content took 0.68s; a hostile ~1 MB token would
#: cost roughly a minute, all before any key is ever touched).
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


def _pkcs7_decrypt(der: bytes, key_pem: bytes) -> bytes:
    """Decrypt a PKCS7 ``EnvelopedData`` blob with only the recipient's
    private key (no certificate) -- eyaml's own encrypt-to-one-recipient
    shape. Every structural problem raises
    ``BackendError("Could not parse the PKCS7: <detail>")``; a key or
    cipher problem raises ``BackendError("Could not decrypt the PKCS7:
    <detail>")`` or ``BackendError("bad decrypt")`` for anything that is,
    or could be, a wrong-key/garbled-ciphertext symptom (OpenSSL's own
    text for exactly that case) -- never a distinguishable error for a
    length mismatch versus a padding failure versus an implicit-rejection
    "succeeded with garbage".

    On every exit path, ``key_pem``/``private_key``/``key``/``padded`` are
    overwritten before this function returns or its exception leaves the
    frame: a ``raise ... from None`` inside an ``except`` block still sets
    ``__context__`` to the exception being handled (``from None`` only
    suppresses it in *printed* tracebacks), and that inner exception's own
    traceback frame is this same frame -- so without this, the private key
    PEM and, on an unpadding failure, the real plaintext of every AES
    block before the tampered one stay reachable from the exception a
    caller sees, through ``__context__.__traceback__``.
    """
    from cryptography.hazmat.primitives import padding as sympad
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    private_key = None
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
                idx += 1
                if idx >= len(env_children):
                    raise _DerError("malformed EnvelopedData")
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
            raise BackendError("Could not parse the PKCS7: {}".format(e)) from None

        try:
            private_key = serialization.load_pem_private_key(key_pem, password=None)
        except Exception as e:
            raise BackendError("Could not read the private key: {}".format(e)) from None

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
        # Scrub every frame local that ever held the private key PEM or
        # decrypted bytes, on every exit path (return or raise) -- see the
        # docstring above for why `from None` alone does not do this.
        key_pem = b""
        private_key = None
        key = None
        padded = b""


def _chomp(text: str) -> str:
    """Ruby ``String#chomp`` (no argument): drop one trailing ``\\r\\n``,
    ``\\n`` or ``\\r``, nothing else."""
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n") or text.endswith("\r"):
        return text[:-1]
    return text


def decrypt_string(data: str, options: dict, key, path) -> str:
    """Decrypt every ``ENC[...]`` token in ``data`` (``eyaml_lookup_key.
    rb:81-98``); a string with no such token is returned unchanged.

    Each token is stripped of whitespace and re-matched before decoding, so
    a value folded across lines (YAML already turns most of that into
    plain spaces) still parses the same way hiera-eyaml's own scanner
    would. The default scheme is ``PKCS7``, compared case-insensitively;
    any other name raises hiera-eyaml's own ``LoadError`` text, unwrapped
    (Puppet's own ``rescue StandardError`` misses ``LoadError`` too) --
    with a hint naming this project's own PKCS7-only support. Every other
    failure is wrapped as ``BackendError("hiera-eyaml backend error
    decrypting <token> when looking up <key> in <path>. Error was
    <message>", path=path)`` and chomped once at the end (never per
    token), matching hiera-eyaml's own single ``.chomp`` on the whole
    joined result.
    """
    if not _has_encrypted_token(data):
        return data

    def decrypt_one(scheme: str, body: str) -> str:
        """Everything that *is* wrapped as a decrypt-error on failure.

        ``key_pem``/``plaintext`` are scrubbed in ``finally`` on every exit
        path, for the same reason ``_pkcs7_decrypt`` scrubs its own copies
        (see that function's docstring): this frame is on the traceback of
        whatever it raises, and a chained ``__context__`` keeps that
        traceback -- and this frame's locals -- reachable even past a
        ``from None``.
        """
        key_pem = b""
        plaintext = b""
        try:
            key_pem = _private_key_pem(options)
            der = _decode64(body)
            plaintext = _pkcs7_decrypt(der, key_pem)
            try:
                return plaintext.decode("utf-8")
            except UnicodeDecodeError:
                raise BackendError("invalid byte sequence in UTF-8") from None
        finally:
            key_pem = b""
            plaintext = b""

    def replace(match: "re.Match") -> str:
        token = match.group(0)
        stripped = _WHITESPACE_RE.sub("", token)
        clean = _STRIPPED_RE.fullmatch(stripped)
        if clean is None:
            raise BackendError(
                "hiera-eyaml backend error decrypting {} when looking up {} "
                "in {}. Error was Could not parse the PKCS7: malformed "
                "token".format(token, key, path),
                path=str(path),
            )
        raw_scheme = clean.group(1)
        scheme = (raw_scheme[:-1] if raw_scheme else "PKCS7").lower()
        body = clean.group(2)
        if scheme != "pkcs7":
            # Puppet's own `rescue StandardError` never catches Ruby's
            # LoadError either -- this is never wrapped in the "backend
            # error decrypting ..." text.
            raise BackendError(
                "cannot load such file -- hiera/backend/eyaml/encryptors/"
                "{}: only the PKCS7 eyaml encryptor is supported".format(scheme),
                path=str(path),
            )
        try:
            return decrypt_one(scheme, body)
        except BackendError as e:
            raise BackendError(
                "hiera-eyaml backend error decrypting {} when looking up {} "
                "in {}. Error was {}".format(token, key, path, e),
                path=str(path),
            ) from None

    result = _TOKEN_RE.sub(replace, data)
    return _chomp(result)
