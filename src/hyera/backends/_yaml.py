# Ported from Puppet 8 lib/puppet/functions/yaml_data.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""YAML (``yaml_data``) backend: Puppet's own Psych-compatible parsing."""

import logging
import typing as _ty

import yaml

from ..exceptions import BackendError
from . import Backend, _Names
from ._psych import safe_load, symkeys_to_string

_LOGGER = logging.getLogger(__name__)

__all__ = ["YAMLBackend"]


class YAMLBackend(Backend):
    """YAML (``.yaml``/``.yml``) data via Puppet's own Psych-compatible
    rules (:mod:`hyera.backends._psych`): numbers/booleans/dates/symbols
    parse Ruby's way, and a non-Hash top-level document warns (or raises
    under ``strict="error"``) and reads as empty."""

    NAMES: _ty.ClassVar[_Names] = {"function": ("yaml_data",), "format": ("yaml",)}
    EXTENSIONS: _ty.ClassVar[_ty.Tuple[str, ...]] = (".yaml", ".yml")

    def loads(self, text: str) -> _ty.Any:
        """Parse YAML the way Puppet's ``yaml_data`` does (Ruby Psych
        semantics via :mod:`hyera.backends._psych`), not PyYAML's own
        Python-flavored resolver.

        :param text: the YAML text to parse.
        :returns: the parsed value.
        """
        # Psych's rules (types, BOM, one-document, symbol keys/values),
        # ported in ``_psych``: numbers/booleans/dates/symbols per
        # Ruby's ScalarScanner, not PyYAML's own Python-flavored resolver.
        return safe_load(text)

    def dumps(self, obj: _ty.Any, **kw: _ty.Any) -> str:
        """Render ``obj`` as YAML (block style, sorted keys off, Unicode
        left unescaped).

        :param obj: the value to render.
        :param kw: forwarded to :func:`yaml.safe_dump`.
        :returns: the rendered YAML text.
        """
        kw.setdefault("sort_keys", False)
        kw.setdefault("allow_unicode", True)
        kw.setdefault("default_flow_style", False)
        return yaml.safe_dump(obj, **kw)

    def _as_data_hash(self, parsed, path):
        """Port of ``yaml_data.rb:27-35``: a Hash passes through (with any
        ``RubySymbol`` key turned into its plain-string name); ``nil``/
        ``false`` always warn-and-empty; any other non-Hash value errors
        under ``strict == "error"``, else warns-and-empties."""
        if isinstance(parsed, dict):
            return symkeys_to_string(parsed)
        message = "{}: file does not contain a valid yaml hash".format(path)
        if parsed is None or parsed is False:
            _LOGGER.warning(message)
            return {}
        if self.strict == "error":
            raise BackendError(message)
        _LOGGER.warning(message)
        return {}
