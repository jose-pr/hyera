"""The ``duho.Arg`` annotation of every ``Lookup`` field, and its agent-help metadata.

Module-level aliases: duho reads the same objects through
``typing.get_type_hints``, and pyright resolves a top-level
``X = duho.Arg[...]`` assignment as an implicit type alias where an inline
string forward-reference on the field would be Unknown. Imported only when
``duho`` is installed.
"""

import typing as _ty

import duho

_KeysArg = duho.Arg[_ty.List[str], duho.NS(flags=["keys"], metavar="KEY")]
_MergeArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--merge"])]
_KnockOutPrefixArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--knock-out-prefix"])]
_SortMergedArraysArg = duho.Arg[bool, duho.NS(flags=["--sort-merged-arrays"])]
_MergeHashArraysArg = duho.Arg[bool, duho.NS(flags=["--merge-hash-arrays"])]
_ValueTypeArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--type"])]
_DefaultArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--default"])]
_ExplainArg = duho.Arg[bool, duho.NS(flags=["--explain"])]
_ExplainOptionsArg = duho.Arg[bool, duho.NS(flags=["--explain-options"])]
_FactsArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--facts"])]
_NodeArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--node"])]
_ScopeArg = duho.Arg[_ty.List[str], duho.NS(flags=["--scope", "-s"]), duho.Append()]
_HieraConfigArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--hiera_config"])]
_EnvironmentArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--environment"])]
_EnvironmentPathArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--environmentpath"])]
_ModulepathArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--modulepath"])]
_BasemodulepathArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--basemodulepath"])]
_CodedirArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--codedir"])]
_StrictArg = duho.Arg[_ty.Optional[str], duho.NS(flags=["--strict"])]
_RenderAsArg = duho.Arg[
    _ty.Optional[str], duho.NS(flags=["--render-as"], metavar="FORMAT")
]
_DebugArg = duho.Arg[bool, duho.NS(flags=["--debug", "-d"])]

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
