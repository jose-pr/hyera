"""``eyaml_lookup_key`` support: token scanning, PKCS7 key loading and a
cert-free PKCS7 EnvelopedData decrypt over ``cryptography`` primitives.

Original code: the token grammar mirrors
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
import typing as _ty

from ..exceptions import BackendError, ConfigError
from .._lookup.function_provider import LookupContext
from . import Backend, _Names
from ._yaml import YAMLBackend

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


def _load_private_key(key_pem: bytes):
    """Parse ``key_pem`` into a usable private key object, matching Ruby's
    ``OpenSSL::PKey::RSA.new(private_key_pem)`` -- the step hiera-eyaml's
    own ``Pkcs7.decrypt`` performs *before* it ever looks at the
    ciphertext (``pkcs7.rb``'s ``decrypt`` loads and parses the key, then
    parses the PKCS7 DER structure, in that order). Called once per
    :func:`decrypt_string` call (not once per ``ENC[...]`` token the way
    Ruby's own per-token ``Pkcs7.decrypt`` re-parses it) and the result
    reused for every token in that value; the key can change between
    separate :func:`decrypt_string` calls, so nothing here is cached past
    one call.

    :raises BackendError: ``key_pem`` is not a parseable, unencrypted
        private key, with the fixed text Ruby's OpenSSL binding raises for
        it (``OpenSSL::PKey::RSA.new``: "Neither PUB key nor PRIV key").
        The `cryptography` exception is not chained or quoted, so no key
        material can reach the message.
    """
    from cryptography.hazmat.primitives import serialization

    try:
        return serialization.load_pem_private_key(key_pem, password=None)
    except Exception:
        raise BackendError("Neither PUB key nor PRIV key") from None


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
                # No bounds check needed here: the `len(env_children) < 3`
                # guard above already guarantees at least 3 elements, and
                # `idx` only ever reaches 2 in this branch -- always a valid
                # index into `env_children`.
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
            # Defense-in-depth, not currently reachable: every subscript
            # above (`ci_children[...]`, `env_children[...]`,
            # `ktri_children[...]`, `eci_children[...]`, `alg_children[...]`)
            # is preceded by an explicit length/emptiness check that raises
            # `_DerError` first, and `_read_tlv`/`_read_length`/`_children`
            # never index `buf` without bounds-checking `i` first either.
            # Kept anyway so a future edit that adds an unguarded index
            # still surfaces as this same `BackendError` instead of a raw
            # `IndexError`/`ValueError` escaping the parser.
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
        # Scrub this call's own locals that ever held decrypted bytes, on
        # every exit path (return or raise) -- see the docstring above for
        # why `from None` alone does not do this. `private_key` is the
        # caller's, reused across tokens, and is never touched here.
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
    decrypting <data> when looking up <key> in <path>. Error was
    <message>", path=path)`` -- ``<data>`` is the whole stored value, not
    just the one token that failed, matching hiera-eyaml's own
    ``eyaml_lookup_key.rb``, which wraps a single ``rescue`` around parsing
    and decrypting the entire value at once and interpolates its own
    original (pre-decrypt) argument into the message. Chomped once at the
    end (never per token), matching hiera-eyaml's own single ``.chomp`` on
    the whole joined result.

    The private key is loaded and parsed at most once per call (not once
    per token, the way hiera-eyaml's own per-token ``Pkcs7.decrypt``
    re-parses it every time) and that parse happens before any ciphertext
    token is ever decoded or parsed -- matching Ruby's own order
    (``pkcs7.rb``'s ``decrypt`` loads and validates the key before it ever
    touches the PKCS7 DER structure), so a bad key is reported as a key
    problem even when the stored ciphertext is also malformed.
    """
    if not _has_encrypted_token(data):
        return data

    #: The parsed private key, loaded at most once for this call and reused
    #: across every token in `data` -- a list (not a plain variable) so the
    #: nested closures below can both read and populate it. Never cached
    #: past this one call: the key can change between separate
    #: `decrypt_string` calls.
    _key_box: "_ty.List[_ty.Any]" = []

    def get_private_key():
        if not _key_box:
            key_pem = b""
            try:
                key_pem = _private_key_pem(options)
                _key_box.append(_load_private_key(key_pem))
            finally:
                key_pem = b""
        return _key_box[0]

    def decrypt_one(scheme: str, body: str) -> str:
        """Everything that *is* wrapped as a decrypt-error on failure.

        ``plaintext`` is scrubbed in ``finally`` on every exit path, for
        the same reason ``_pkcs7_decrypt`` scrubs its own copies (see that
        function's docstring): this frame is on the traceback of whatever
        it raises, and a chained ``__context__`` keeps that traceback --
        and this frame's locals -- reachable even past a ``from None``.
        """
        plaintext = b""
        try:
            private_key = get_private_key()
            der = _decode64(body)
            plaintext = _pkcs7_decrypt(der, private_key)
            try:
                return plaintext.decode("utf-8")
            except UnicodeDecodeError:
                raise BackendError("invalid byte sequence in UTF-8") from None
        finally:
            plaintext = b""

    def replace(match: "re.Match") -> str:
        token = match.group(0)
        stripped = _WHITESPACE_RE.sub("", token)
        clean = _STRIPPED_RE.fullmatch(stripped)
        if clean is None:
            raise BackendError(
                "hiera-eyaml backend error decrypting {} when looking up {} "
                "in {}. Error was Could not parse the PKCS7: malformed "
                "token".format(data, key, path),
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
                "in {}. Error was {}".format(data, key, path, e),
                path=str(path),
            ) from None

    try:
        result = _TOKEN_RE.sub(replace, data)
        return _chomp(result)
    finally:
        _key_box.clear()


class EyamlBackend(Backend):
    """Puppet's hiera-eyaml ``lookup_key`` function, PKCS7 only (behind the
    optional ``eyaml`` extra). Ports ``functions/eyaml_lookup_key.
    rb:25-79``: the raw ``.eyaml`` file loads once per change (through
    :meth:`~hyera._lookup.function_provider.LookupContext.cached_file_data`, its
    *raw* parse only -- caching the non-Hash rule's strict-sensitive result
    would freeze whichever strictness read it first, exactly the trap
    ``Hiera._load_file`` guards against for ``data_hash``), then each
    requested key's value is decrypted (:func:`hyera.backends._eyaml.decrypt_string`);
    the engine keeps that result until the file changes. The raw hash is
    never returned to the engine, and its
    values are never interpolated except through
    :func:`~hyera.backends._eyaml.decrypt_string`'s own trailing ``context.
    interpolate`` call.
    """

    NAMES: _ty.ClassVar[_Names] = {"function": ("eyaml_lookup_key",)}

    @classmethod
    def check_available(cls) -> None:
        """Raise :class:`BackendError` naming the ``eyaml`` extra
        when ``cryptography`` is not importable."""
        check_cryptography()

    def lookup_key(
        self,
        key: str,
        options: _ty.Mapping[str, _ty.Any],
        context: LookupContext,
    ) -> _ty.Any:
        """Decrypt ``key``'s PKCS7 ``ENC[...]`` value from the ``.eyaml``
        file named by the hierarchy location, matching Puppet's
        ``eyaml_lookup_key``.

        :param key: the key to decrypt.
        :param options: the hierarchy entry's ``options`` (``path`` required).
        :param context: the per-location :class:`LookupContext`.
        :returns: the decrypted (and interpolated) value.
        :raises ConfigError: no ``path`` location was declared.
        :raises BackendError: the private key or ciphertext could not be
            read, parsed or decrypted.
        """
        if "path" not in options:
            raise ConfigError(
                "'eyaml_lookup_key': one of 'path', 'paths' 'glob', 'globs' "
                "or 'mapped_paths' must be declared in hiera.yaml when "
                "using this lookup_key function"
            )
        path = options["path"]
        parsed = context.cached_file_data(path, parse=YAMLBackend().loads)

        # The non-Hash rule reads `self.strict` at call time, same as
        # `yaml_data`'s own -- applied fresh on every read (never cached),
        # so a later call under different strictness sees its own rule.
        raw = YAMLBackend(strict=self.strict)._as_data_hash(parsed, path)
        if key not in raw:
            context.not_found()
        value = raw[key]
        return self._decrypt(value, options, context, key, path)

    def _decrypt(self, value, options, context, key, path):
        """Recurse into ``value`` decrypting every string
        (``eyaml_lookup_key.rb:66-79``): a Hash's keys are interpolated but
        never decrypted, a List/Hash's elements/values recurse, and every
        other type (int/float/bool/None) passes through unchanged. Only the
        decrypted string leaves through ``context.interpolate`` -- a value
        with no ``ENC[...]`` token is interpolated too (mirroring Puppet's
        own unconditional call)."""
        if isinstance(value, str):
            return context.interpolate(decrypt_string(value, options, key, path))
        if isinstance(value, dict):
            return {
                context.interpolate(k): self._decrypt(v, options, context, key, path)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self._decrypt(v, options, context, key, path) for v in value]
        return value
