"""Which disagreements are declared differences, and under which id.

Every rule names the difference the README publishes and the predicate that
decides it, so a disagreement is classified by what it is and not by where it was
found. A disagreement no rule accepts is unclassified, and the run fails on it.
Rules run in order; the first that accepts wins.

A rule's ``published`` is ``None`` when its id is one of the README's
"Differences from Puppet" ids, and otherwise the exact README text of the
"Not supported" row it stands for; a test checks both against the README.

``OPEN`` holds the defects that are known and not yet fixed. They are reported
separately from declared differences and do not fail a run; each entry is removed
when its defect is fixed, and a replay fails once hyera stops diverging.
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, NamedTuple, Optional

from .outcomes import (
    Verdict,
    aio,
    canon,
    disagreement,
    json_text,
    message_differs,
    message_of,
)

#: The prefix of a verdict's rule when a known open defect explains it.
OPEN_PREFIX = "open:"


class Facts(NamedTuple):
    """What a rule may look at for one disagreement."""

    job: dict
    kind: str
    puppet: dict
    api: Optional[dict]
    cli: dict

    @property
    def ours(self) -> dict:
        return self.api if self.api is not None else self.cli

    @property
    def render(self) -> Optional[str]:
        return self.job.get("render")

    @property
    def query(self) -> dict:
        return self.job["query"]


class Rule(NamedTuple):
    """One declared difference: its id, the kinds it can explain, the test, and
    the README text it stands for when its id is not a README id."""

    id: str
    kinds: tuple
    decide: Callable[[Facts], bool]
    published: Optional[str] = None


class OpenDefect(NamedTuple):
    """A known, unfixed divergence: a short key, what it is, and the test."""

    key: str
    what: str
    kinds: tuple
    decide: Callable[[Facts], bool]


#: Puppet failures that are Ruby crashes, not designed errors.
_CRASH_TEXT = re.compile(
    r"undefined method|NoMethodError|stack level too deep|wrong number of arguments"
    r"|undefined local variable|no implicit conversion|can't convert"
    r"|private method '"
)
_NESTING_TEXT = re.compile(r"nesting of \d+ is too deep")


def _yaml_equivalent(f: Facts) -> bool:
    import yaml

    if f.render != "yaml":
        return False
    try:
        return yaml.safe_load(f.puppet["text"]) == yaml.safe_load(f.cli["text"])
    except yaml.YAMLError:
        return False


def _sensitive_yaml(f: Facts) -> bool:
    return f.render == "yaml" and "Sensitive" in f.puppet["text"]


def _knockout_prefix(f: Facts) -> Optional[str]:
    merge = f.query.get("merge")
    if isinstance(merge, dict) and "knockout_prefix" in merge:
        return merge["knockout_prefix"]
    args = f.query.get("args") or []
    for i, arg in enumerate(args):
        if arg == "--knock-out-prefix" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--knock-out-prefix="):
            return arg.split("=", 1)[1]
    return None


def _knockout_not_python_regex(f: Facts) -> bool:
    prefix = _knockout_prefix(f)
    if prefix is None:
        return False
    try:
        re.compile(prefix)
    except re.error:
        return f.ours["status"] == "error"
    return False


def _puppet_crashes(f: Facts) -> bool:
    return (
        f.puppet["status"] == "error"
        and _CRASH_TEXT.search(message_of(f.puppet)) is not None
        and f.ours["status"] in ("found", "not_found")
    )


_UNSUPPORTED_TYPE = re.compile(r"does not support the Puppet type '(\w+)")
_UNRUNNABLE_TYPES = ("Iterable", "Iterator", "Init", "Unit")


def _unsupported_type(f: Facts) -> Optional[str]:
    """The name of the type hyera refuses where Puppet answers, if that is the
    disagreement."""
    if f.puppet["status"] != "found" or f.ours["status"] != "error":
        return None
    found = _UNSUPPORTED_TYPE.search(message_of(f.ours))
    return found.group(1) if found else None


def _convert_unsupported(f: Facts) -> bool:
    if (
        f.puppet["status"] == "found"
        and f.ours["status"] == "error"
        and "does not support new() for the Puppet type" in message_of(f.ours)
    ):
        return True
    name = _unsupported_type(f)
    return name is not None and name not in _UNRUNNABLE_TYPES


def _unrunnable_type(f: Facts) -> bool:
    return _unsupported_type(f) in _UNRUNNABLE_TYPES


def _regex_construct(f: Facts) -> bool:
    return (
        f.puppet["status"] != "error"
        and f.ours["status"] == "error"
        and "in the Ruby regular expression" in message_of(f.ours)
    )


def _v3_backend(f: Facts) -> bool:
    return (
        f.puppet["status"] == "not_found"
        and f.ours["status"] == "error"
        and "Ruby Hiera 3 backends cannot run here" in message_of(f.ours)
    )


def _format_subset(f: Facts) -> bool:
    return (
        f.puppet["status"] == "found"
        and f.ours["status"] == "error"
        and "does not support the # (indenting) flag" in message_of(f.ours)
    )


def _key_shapes(f: Facts) -> bool:
    return (
        f.puppet["status"] != "error"
        and f.ours["status"] == "error"
        and "is not hashable" in message_of(f.ours)
    )


def _colon_variable(f: Facts) -> bool:
    """``%{::::x}``: the query's id carries the expression it asks about."""
    return f.kind == "VALUE" and "%{::::" in f.job.get("qid", "")


def _compile_trusted(f: Facts) -> bool:
    argv = f.job["argv"]
    return ("--compile" in argv or "--trusted" in argv) and f.ours["status"] == "error"


def _missing_config(f: Facts) -> bool:
    return (
        "--hiera_config" in f.job["argv"]
        and f.ours["status"] == "error"
        and "Unable to read the Lookup Configuration" in message_of(f.ours)
    )


def _equal_keys(f: Facts) -> bool:
    return (
        f.puppet["status"] != "error"
        and f.ours["status"] == "error"
        and "indistinguishable to hyera" in message_of(f.ours)
    )


def _environment_argument(f: Facts) -> Optional[str]:
    args = f.query.get("args") or []
    for i, arg in enumerate(args):
        if arg == "--environment" and i + 1 < len(args):
            return args[i + 1]
    return None


def _environment_slash(f: Facts) -> bool:
    name = _environment_argument(f)
    return bool(name) and name.endswith("/") and f.ours["status"] == "error"


def _non_utf8(f: Facts) -> bool:
    return (
        f.puppet["status"] == "found"
        and f.ours["status"] == "error"
        and "codec can't decode" in message_of(f.ours)
    )


def _hocon_parser(f: Facts) -> bool:
    return bool({".conf", ".hocon"} & set(f.job.get("exts", ())))


def _nesting_bound(f: Facts) -> bool:
    return (
        f.puppet["status"] == "error"
        and _NESTING_TEXT.search(message_of(f.puppet)) is not None
        and f.ours["status"] != "error"
    )


def _json_float_spelling(f: Facts) -> bool:
    """Same values in the same order, spelled differently."""
    if f.render in ("yaml", "s") or f.kind != "RENDER":
        return False
    try:
        ours = canon(json.loads(json_text(f.cli)), True)
        theirs = canon(json.loads(json_text(f.puppet)), True)
    except ValueError:
        return False
    return ours == theirs and aio(json_text(f.puppet)) != aio(json_text(f.cli))


def _empty_environment(f: Facts) -> bool:
    args = f.query.get("args") or []
    return any(
        a == "--environment" and i + 1 < len(args) and args[i + 1] == ""
        for i, a in enumerate(args)
    )


def _lookup_options_key(f: Facts) -> bool:
    return (
        f.query["key"] == "lookup_options"
        and f.puppet["status"] == "not_found"
        and f.ours["status"] == "error"
    )


#: Declared differences, in the order they are tried.
RULES: List[Rule] = [
    Rule("render-yaml-sensitive-redacted", ("RENDER",), _sensitive_yaml),
    Rule("render-yaml-equivalent-not-identical", ("RENDER",), _yaml_equivalent),
    Rule(
        "json-float-spelling",
        ("RENDER",),
        _json_float_spelling,
        "`--render-as json` float text",
    ),
    Rule("knockout-prefix-not-python-regex", ("STATUS",), _knockout_not_python_regex),
    Rule("convert-to-unsupported-type", ("STATUS",), _convert_unsupported),
    Rule("ruby-regex-constructs", ("STATUS",), _regex_construct),
    Rule(
        "unrunnable-types",
        ("STATUS",),
        _unrunnable_type,
        "The types `Iterable`, `Iterator`, `Init` and `Unit` in a type expression",
    ),
    Rule("v3-ruby-backend-unavailable", ("STATUS",), _v3_backend),
    Rule("string-format-subset", ("STATUS",), _format_subset),
    Rule(
        "interpolation-key-shapes",
        ("STATUS", "VALUE"),
        lambda f: _key_shapes(f) or _colon_variable(f),
    ),
    Rule("environment-conf-compile-trusted-unsupported", ("STATUS",), _compile_trusted),
    Rule("missing-config-raises", ("STATUS",), _missing_config),
    Rule("python-equal-hash-keys", ("STATUS",), _equal_keys),
    Rule("environment-trailing-slash", ("STATUS",), _environment_slash),
    Rule("non-utf8-data", ("STATUS",), _non_utf8),
    Rule("nesting-bound", ("STATUS",), _nesting_bound),
    Rule("puppet-crashes-hyera-answers", ("STATUS",), _puppet_crashes),
    Rule(
        "hocon-pyhocon-parser",
        ("STATUS", "VALUE", "RENDER"),
        _hocon_parser,
    ),
    Rule("global-config-error-at-construction", ("STATUS",), _lookup_options_key),
    Rule(
        "empty-environment",
        ("STATUS", "CHANNEL"),
        _empty_environment,
        "An empty `--environment`",
    ),
]

#: Known, unfixed divergences.
OPEN: List[OpenDefect] = []


def classify(facts: Facts) -> Optional[str]:
    """The id of the first rule (or ``open:<key>``) that explains this
    disagreement, or ``None``."""
    for rule in RULES:
        if facts.kind in rule.kinds and rule.decide(facts):
            return rule.id
    for defect in OPEN:
        if facts.kind in defect.kinds and defect.decide(facts):
            return OPEN_PREFIX + defect.key
    return None


def judge(job: dict, puppet: dict, hyera: dict) -> Verdict:
    """Compare Puppet's outcome with hyera's and classify the result.

    :param job: the job (its ``query``, ``render`` and ``exts``).
    :param puppet: Puppet's outcome.
    :param hyera: ``{"api": ..., "cli": ...}`` from :func:`outcomes.hyera_outcomes`.
    :returns: ``(kind, rule)``: ``("AGREE", None)`` when they agree,
        ``("AGREE", id)`` when they agree on the status and a declared difference
        explains the text, ``(kind, id)`` for a classified disagreement,
        ``(kind, "open:<key>")`` for a known open defect and ``(kind, None)`` for
        an unclassified one.
    """
    kind = disagreement(job, puppet, hyera)
    if kind is None:
        if puppet["status"] == "error" and message_differs(puppet, hyera):
            return ("AGREE", "error-message-text")
        if job.get("render") == "s" and puppet["status"] == "found":
            if puppet["text"] != hyera["cli"]["text"]:
                return ("AGREE", "aio-hash-rendering")
        return ("AGREE", None)
    facts = Facts(job, kind, puppet, hyera["api"], hyera["cli"])
    return (kind, classify(facts))
