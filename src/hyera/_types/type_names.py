# Ported from Puppet 8 lib/puppet/pops/types/type_parser.rb
# (https://github.com/puppetlabs/puppet), Apache-2.0. Modified by jose-pr.
# See NOTICE.
"""The type names a Puppet type expression accepts.

Which names take no parameters, which are unsupported, and the constructor
each bare name stands for, as ``type_parser.rb`` lists them.
"""

from __future__ import annotations

from .compound_types import COLLECTION, Array, Hash, Struct, Tuple, Variant, _PNamedType
from .types import (
    ANY,
    BOOLEAN,
    NAMED_ONLY_TYPES,
    NUMERIC,
    REGEXP,
    SCALAR,
    SCALAR_DATA,
    UNDEF,
    Enum,
    Float,
    Integer,
    NotUndef,
    Optional,
    Pattern,
    SensitiveType,
    String,
)

#: Names never accepted with parameters, whether or not they are otherwise
#: parameterizable elsewhere (``type_parser.rb`` ``when 'any', 'data', ...``).
_NEVER_PARAMETERIZED = frozenset(
    [
        "any",
        "data",
        "catalogentry",
        "scalar",
        "undef",
        "numeric",
        "default",
        "semverrange",
        "scalardata",
    ]
)

#: The third tier: never modeled at all, bare or
#: parameterized.
_UNSUPPORTED_NAMES = frozenset(["iterable", "iterator", "init", "unit"])

#: Bare (zero-argument) type names -> a constructor taking no arguments.
_BARE_TYPES = {
    "any": lambda: ANY,
    "undef": lambda: UNDEF,
    "notundef": lambda: NotUndef(),
    "optional": lambda: Optional(),
    "enum": lambda: Enum([]),
    "pattern": lambda: Pattern([]),
    "scalar": lambda: SCALAR,
    "scalardata": lambda: SCALAR_DATA,
    "string": lambda: String.DEFAULT,
    "integer": lambda: Integer.DEFAULT,
    "float": lambda: Float.DEFAULT,
    "numeric": lambda: NUMERIC,
    "boolean": lambda: BOOLEAN,
    "array": lambda: Array(),
    "hash": lambda: Hash(),
    "collection": lambda: COLLECTION,
    "tuple": lambda: Tuple([]),
    "struct": lambda: Struct([]),
    "variant": lambda: Variant([]),
    "sensitive": lambda: SensitiveType(),
    "regexp": lambda: REGEXP,
}
#: Names whose Puppet-cased spelling ``str.capitalize()`` gets wrong.
_PRETTY_NAMES = {
    "uri": "URI",
    "semver": "SemVer",
    "semverrange": "SemVerRange",
    "typeset": "TypeSet",
    "catalogentry": "CatalogEntry",
    "notundef": "NotUndef",
    "scalardata": "ScalarData",
}
for _n in NAMED_ONLY_TYPES:
    _pretty_n = _PRETTY_NAMES.get(_n, _n.capitalize())
    _BARE_TYPES.setdefault(_n, (lambda n: (lambda: _PNamedType(n)))(_pretty_n))
