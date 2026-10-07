"""The ``duho.Arg`` annotation of every ``Lookup`` field, and its agent-help metadata.

Module-level aliases: duho reads the same objects through
``typing.get_type_hints``, and pyright resolves a top-level
``X = duho.Arg[...]`` assignment as an implicit type alias where an inline
string forward-reference on the field would be Unknown. Imported only when
``duho`` is installed.
"""

from __future__ import annotations

import typing as _ty

import duho

# literal_value: a value option takes the next word whatever it looks like, as
# Puppet's option parser does (--knock-out-prefix --, --default -x).
_KeysArg = duho.Arg[_ty.List[str], duho.Meta(flags=["keys"], metavar="KEY")]
_MergeArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--merge"], literal_value=True)
]
_KnockOutPrefixArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--knock-out-prefix"], literal_value=True)
]
_SortMergedArraysArg = duho.Arg[bool, duho.Meta(flags=["--sort-merged-arrays"])]
_MergeHashArraysArg = duho.Arg[bool, duho.Meta(flags=["--merge-hash-arrays"])]
_ValueTypeArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--type"], literal_value=True)
]
_DefaultArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--default"], literal_value=True)
]
_ExplainArg = duho.Arg[bool, duho.Meta(flags=["--explain"])]
_ExplainOptionsArg = duho.Arg[bool, duho.Meta(flags=["--explain-options"])]
_FactsArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--facts"], literal_value=True)
]
_NodeArg = duho.Arg[_ty.Optional[str], duho.Meta(flags=["--node"], literal_value=True)]
_ScopeArg = duho.Arg[
    _ty.List[str], duho.Meta(flags=["--scope", "-s"], literal_value=True), duho.Append()
]
_HieraConfigArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--hiera_config"], literal_value=True)
]
_EnvironmentArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--environment"], literal_value=True)
]
_EnvironmentPathArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--environmentpath"], literal_value=True)
]
_ModulepathArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--modulepath"], literal_value=True)
]
_BasemodulepathArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--basemodulepath"], literal_value=True)
]
_CodedirArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--codedir"], literal_value=True)
]
_StrictArg = duho.Arg[
    _ty.Optional[str], duho.Meta(flags=["--strict"], literal_value=True)
]
_RenderAsArg = duho.Arg[
    _ty.Optional[str],
    duho.Meta(flags=["--render-as"], metavar="FORMAT", literal_value=True),
]
_DebugArg = duho.Arg[bool, duho.Meta(flags=["--debug", "-d"])]

#: Shown by the agent-help document (AGENT_HELP=1 hyera --help).
_EXIT_CODES = {
    0: "Found (or --default printed, or an explain report printed)",
    1: "No value found for the key",
    2: "Any other error: bad flag or value, unreadable facts or data, failed lookup",
    130: "Interrupted",
}
_EXAMPLES = (
    (
        "hyera --facts examples/facts.yaml --hiera_config examples/hiera.yaml users",
        "Look up a key; prints YAML.",
    ),
    (
        "hyera --facts examples/facts.yaml --hiera_config examples/hiera.yaml "
        "--merge deep --render-as json users",
        "Deep-merge every level's value and print JSON.",
    ),
    (
        "hyera --facts examples/facts.yaml --hiera_config examples/hiera.yaml "
        "--explain ntp::servers",
        "Show which hierarchy levels and files were searched.",
    ),
)
