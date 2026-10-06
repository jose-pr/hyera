# Ported from hiera-eyaml lib/hiera/backend/eyaml/parser/{encrypted_tokens,parser}.rb
# (https://github.com/voxpupuli/hiera-eyaml), MIT, and from Puppet 8 lib/puppet/functions/eyaml_lookup_key.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr. See NOTICE.
"""``eyaml_lookup_key`` support: token scanning and PKCS7 key loading.

The token grammar follows hiera-eyaml's
``parser/encrypted_tokens.rb`` and ``parser/parser.rb`` and the lookup
function Puppet's ``functions/eyaml_lookup_key.rb``, re-expressed with
Python's ``re``; the decrypt itself is :mod:`hyera.backends._pkcs7`.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import typing as _ty

from ..exceptions import BackendError, ConfigError
from .._lookup.function_provider import LookupContext
from . import Backend, _Names
from ._pkcs7 import _pkcs7_decrypt
from ._yaml import YAMLBackend

_LOGGER = logging.getLogger(__name__)

#: hiera-eyaml's own regexes (``encrypted_tokens.rb:109-128``), `\w` made
#: ASCII to match Ruby's default (non-Unicode) `\w` in these patterns.
_TOKEN_RE = re.compile(r"ENC\[([A-Za-z0-9_]+,)?([a-zA-Z0-9+/ =\n]+?)\]")
_STRIPPED_RE = re.compile(r"ENC\[([A-Za-z0-9_]+,)?([a-zA-Z0-9+/=]+?)\]")

_WHITESPACE_RE = re.compile(r"\s")

#: Ruby's ``Base64.decode64`` (``unpack1('m')``) silently drops any byte outside the base64 alphabet
#: instead of raising; the resulting bytes still have to pass ``load_pem_private_key``.
_B64_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
)


#: hiera-eyaml decides whether a value needs decrypting with ``/.*ENC\[.*?\]/``: linear under Ruby, but under
#: Python's backtracking ``re`` a run of unmatched ``ENC[`` prefixes is cubic (over 300s past ~2500 repeats).
#: This is the same presence test, a line at a time with no regex (equivalent on 200k random strings).


def _has_encrypted_token(data: str) -> bool:
    for line in data.split("\n"):
        i = line.find("ENC[")
        if i != -1 and "]" in line[i + 4 :]:
            return True
    return False


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


#: Ruby class names for the ``TypeError`` text a non-``String`` argument gets from ``File.exist?``/
#: ``File.read`` (``"no implicit conversion of <Class> into String"``), for the YAML scalar shapes an option can hold.
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
            # `os.path.exists`/`open` accept an int as an open file descriptor (and `open` coerces True/False to fd 1/0);
            # reading and closing a host fd the data author never named is never acceptable. Puppet's `File.exist?(3)`
            # raises this TypeError text before touching a descriptor.
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

    #: The parsed private key, loaded at most once per call and reused across every token in `data`: a list so the
    #: nested closures can read and populate it. Not cached past this call, as the key can change between calls.
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
    ``_LocationStore.load_file`` guards against for ``data_hash``), then each
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
