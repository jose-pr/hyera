# Derived from phiera/backends.py (https://github.com/Nike-Inc/phiera),
# Apache-2.0. Modified by jose-pr. See NOTICE.
"""Data backends: load a hiera data file (YAML, JSON, sops-encrypted YAML)."""

import json
import shutil
import subprocess

import yaml

from .exceptions import BackendError
from .util import LookupDict

__all__ = [
    "Backend",
    "YAMLBackend",
    "SopsYAMLBackend",
    "JSONBackend",
    "HOCONBackend",
    "BackendError",
    "has_hocon",
]

#: How long (seconds) to wait for the ``sops`` subprocess before giving up.
#: Kept finite so an unattended lookup never hangs forever on a wedged sops.
SOPS_TIMEOUT = 30


class Backend(object):
    """Backends load data from files. Subclasses override ``.load``.

    A backend registers itself under one or more ``NAMES`` (the Hiera 5
    ``data_hash`` value used in a hierarchy level, e.g. ``yaml_data``).
    """

    #: data_hash name(s) this backend answers to.
    NAMES: "tuple[str, ...]" = ()

    def __init__(self, conf: dict = None):
        self.conf = conf or {}
        # Accept either ``datadir`` or Hiera-5's ``data_dir`` spelling; the
        # loader normalizes to one of these. Missing/None -> "" (relative).
        datadir = self.conf.get("datadir")
        if datadir is None:
            datadir = self.conf.get("data_dir")
        self.datadir: str = datadir or ""

    def read_file(self, path) -> bytes:
        return path.read_bytes()

    def load(self, data: bytes):
        raise NotImplementedError("Subclasses must implement .load")


class YAMLBackend(Backend):
    NAMES = ("yaml_data", "yaml")

    def load(self, data):
        return self.load_ordered(data)

    @staticmethod
    def load_ordered(stream, Loader=yaml.SafeLoader, object_pairs_hook=LookupDict):
        """Parse YAML, materializing mappings as :class:`LookupDict`.

        Uses ``SafeLoader`` by default: hiera data is untrusted config, and
        the full ``Loader`` can construct arbitrary Python objects.
        """

        class OrderedLoader(Loader):
            pass

        def construct_mapping(loader, node):
            loader.flatten_mapping(node)
            return object_pairs_hook(loader.construct_pairs(node))

        OrderedLoader.add_constructor(
            yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
        )
        try:
            return yaml.load(stream, OrderedLoader)
        except yaml.YAMLError as e:
            raise BackendError("Failed to parse YAML: {}".format(e)) from e


class SopsYAMLBackend(YAMLBackend):
    """YAML decrypted on the fly via the ``sops`` CLI.

    Hardened for unattended use: the subprocess has a finite timeout, its
    stderr is captured and surfaced, and a missing ``sops`` binary raises a
    clear :class:`BackendError` instead of an opaque ``FileNotFoundError``.
    """

    NAMES = ("yaml.enc", "sops")

    def read_file(self, path) -> bytes:
        if shutil.which("sops") is None:
            raise BackendError(
                "sops executable not found on PATH; cannot decrypt {}".format(path)
            )
        try:
            proc = subprocess.run(
                ["sops", "--input-type=yaml", "--output-type=yaml", "-d", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=SOPS_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise BackendError(
                "sops timed out after {}s decrypting {}".format(SOPS_TIMEOUT, path)
            ) from e
        except OSError as e:
            raise BackendError("Failed to run sops on {}: {}".format(path, e)) from e

        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()
            raise BackendError(
                "sops failed (exit {}) decrypting {}: {}".format(
                    proc.returncode, path, detail or "<no stderr>"
                )
            )
        return proc.stdout


class JSONBackend(Backend):
    NAMES = ("json_data", "json")

    def load(self, data):
        try:
            return json.loads(data, object_pairs_hook=LookupDict)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise BackendError("Failed to parse JSON: {}".format(e)) from e


class HOCONBackend(Backend):
    """HOCON (``.conf``) data via the optional ``pyhocon`` package."""

    NAMES = ("hocon_data", "hocon")

    def load(self, data):
        try:
            from pyhocon import ConfigFactory
        except ImportError as e:  # pragma: no cover - optional dependency
            raise BackendError(
                "hocon_data backend requires the 'pyhocon' package "
                "(pip install hieralib[hocon])"
            ) from e
        if isinstance(data, bytes):
            data = data.decode("utf-8")
        try:
            parsed = ConfigFactory.parse_string(data)
        except Exception as e:
            raise BackendError("Failed to parse HOCON: {}".format(e)) from e
        return _as_lookupdict(parsed)


def _as_lookupdict(obj):
    """Recursively convert a parsed mapping into :class:`LookupDict`."""
    if isinstance(obj, dict):
        return LookupDict((k, _as_lookupdict(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_as_lookupdict(v) for v in obj]
    return obj


def has_hocon() -> bool:
    """True if the optional ``pyhocon`` dependency is importable."""
    try:
        import pyhocon  # noqa: F401

        return True
    except ImportError:
        return False
