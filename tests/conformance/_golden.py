"""Schema, digest and lint helpers shared by the recorder and the replay tests.

Dependency-free apart from PyYAML, and never imports ``hyera`` (that seam is
``_ours.py``).
"""

import hashlib
import importlib
import json
import re
from pathlib import Path

import yaml

CASES = Path(__file__).resolve().parent / "cases"
GOLDEN = "golden.json"
CASE_FILE = "case.yaml"

#: The node name passed to ``puppet lookup --node``. Never a real host.
NODE = "golden.example.com"
#: Prepended to every recorded/replayed argv unless a case or query supplies
#: its own ``--strict`` (the library defaults to ``warning``).
DEFAULT_PUPPET_ARGS = ("--strict", "warning")
#: Pins the oracle this suite was recorded against.
ORACLE = {"puppet": "8.10.0", "ruby": "4.0.7"}
#: What Puppet renders for a Sensitive value under ``--render-as json``.
SENSITIVE_JSON = "Sensitive [value redacted]"
#: The golden.json schema version this module writes. Format 2 adds each
#: result's ``exit_status``, the recording ``platform`` and the ``gems``
#: versions, and records an error's every line.
FORMAT = 2
#: Schema versions a replay still reads; a golden stays in its format until
#: it is re-recorded.
SUPPORTED_FORMATS = (1, 2)
#: Gems whose behaviour a backend ports; their recorded versions go in a golden.
GEMS = ("deep_merge", "hiera-eyaml", "hocon", "json", "psych")

#: Allowed top-level keys in a case.yaml.
CASE_FIELDS = ("description", "puppet_args", "requires", "queries")
#: Allowed keys on one query entry.
QUERY_FIELDS = (
    "key",
    "id",
    "merge",
    "default",
    "type",
    "puppet_args",
    "hash_inspect",
    "ordered",
    "error_match",
    "error_class",
    "divergence",
    "deviation",
    "note",
    "explain",
    "expression",
    "python",
)
# The subset of QUERY_FIELDS that changes what is asked of Puppet; editing any
# other field never invalidates a recording. `explain` counts only when a query
# sets it.
PUPPET_FIELDS = (
    "id",
    "key",
    "merge",
    "default",
    "type",
    "puppet_args",
    "hash_inspect",
    "explain",
    "expression",
)

#: The fixture module the apply channel puts on Puppet's module path.
FIXTURE_MODULES = Path(__file__).resolve().parent / "puppet_modules"
#: Lines around the JSON document ``hyera_fixture::emit`` writes to stdout.
EMIT_BEGIN = "@@HYERA-VALUE-BEGIN@@"
EMIT_END = "@@HYERA-VALUE-END@@"
#: Fields a query's ``python`` call spec may hold.
PYTHON_SPEC_FIELDS = ("target", "method", "params", "args", "kwargs", "block")
#: What a call spec may call: ``hiera`` methods, and ``types`` objects from
#: ``hyera.types`` (subscripted by ``params``, then called with ``args``).
HIERA_METHODS = ("lookup", "dig", "get", "getvar")
TYPE_NAMES = (
    "Any",
    "Array",
    "Boolean",
    "Collection",
    "Data",
    "Enum",
    "Float",
    "Hash",
    "Integer",
    "NotUndef",
    "Numeric",
    "Optional",
    "Pattern",
    "Regexp",
    "RichData",
    "Scalar",
    "ScalarData",
    "Sensitive",
    "String",
    "Struct",
    "Tuple",
    "Undef",
    "Variant",
)
#: The callables a call spec's ``block`` may name; ``_ours.BLOCKS`` defines them.
BLOCK_NAMES = ("echo", "const", "undef", "message")
# Values a query's ``explain`` field may take: ``data`` -> ``--explain``,
# ``options`` -> ``--explain-options``. Puppet prints the same for both flags
# together, so there is no third value.
EXPLAIN_KINDS = ("data", "options")

#: A divergence id: ``<area>/<slug>``.
_DIVERGENCE_ID_RE = re.compile(r"^[a-z0-9-]+/[a-z0-9._-]+$")
#: Host/path fragments that must never reach a shipped golden.json.
_LEAK_PATTERNS = (
    re.compile(r"/mnt/"),
    re.compile(r"/home/"),
    re.compile(r"/tmp/"),
    re.compile(r"/Users/"),
    re.compile(r"[A-Za-z]:\\"),
)


def case_dirs():
    """Every case directory under ``cases/``, sorted by name."""
    return sorted(p for p in CASES.iterdir() if p.is_dir())


def missing_requirements(case: dict) -> "list[str]":
    """The modules a case ``requires`` that cannot be imported here."""
    missing = []
    for name in case.get("requires") or []:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    return missing


def load_case(case_dir: Path) -> dict:
    return yaml.safe_load((case_dir / CASE_FILE).read_text(encoding="utf-8"))


def query_id(query: dict) -> str:
    """A query's id: its explicit ``id``, else its key (or keys, ``|``-joined).

    An expression query has no key; its id is mandatory (the schema check says so).
    """
    if query.get("id"):
        return query["id"]
    if "key" not in query:
        return str(query.get("expression", ""))
    key = query["key"]
    if isinstance(key, list):
        return "|".join(str(k) for k in key)
    return str(key)


def merge_flags(merge) -> list:
    """A query's ``merge:`` value as ``puppet lookup`` flags."""
    if merge is None:
        return []
    if isinstance(merge, str):
        return ["--merge", merge]
    flags = ["--merge", merge["strategy"]]
    if "knockout_prefix" in merge:
        flags += ["--knock-out-prefix", merge["knockout_prefix"]]
    if merge.get("sort_merged_arrays"):
        flags.append("--sort-merged-arrays")
    if merge.get("merge_hash_arrays"):
        flags.append("--merge-hash-arrays")
    return flags


def lookup_argv(case: dict, query: dict) -> list:
    """The ``puppet lookup`` argv tail for one query (after the ISO settings).

    Order: DEFAULT_PUPPET_ARGS (unless a case/query ``--strict`` replaces
    it), case ``puppet_args``, query ``puppet_args``, merge flags,
    ``--type``, ``--default``, ``--explain``/``--explain-options``, key(s).
    """
    case_args = case.get("puppet_args") or []
    query_args = query.get("puppet_args") or []
    args = []
    if "--strict" not in case_args and "--strict" not in query_args:
        args.extend(DEFAULT_PUPPET_ARGS)
    args.extend(str(a) for a in case_args)
    args.extend(str(a) for a in query_args)
    args.extend(merge_flags(query.get("merge")))
    if query.get("type"):
        args.extend(["--type", query["type"]])
    if query.get("default") is not None:
        args.extend(["--default", str(query["default"])])
    explain = query.get("explain")
    if explain == "data":
        args.append("--explain")
    elif explain == "options":
        args.append("--explain-options")
    key = query["key"]
    if isinstance(key, list):
        args.extend(str(k) for k in key)
    else:
        args.append(str(key))
    return args


def apply_argv(case: dict, query: dict) -> list:
    """The ``puppet apply`` options a case and an expression query add.

    The same order as :func:`lookup_argv` before the lookup-only flags:
    DEFAULT_PUPPET_ARGS (unless a ``--strict`` replaces it), the case's
    ``puppet_args``, then the query's.
    """
    case_args = case.get("puppet_args") or []
    query_args = query.get("puppet_args") or []
    args = []
    if "--strict" not in case_args and "--strict" not in query_args:
        args.extend(DEFAULT_PUPPET_ARGS)
    args.extend(str(a) for a in case_args)
    args.extend(str(a) for a in query_args)
    return args


def parse_emitted(stdout: str):
    """The value ``hyera_fixture::emit`` wrote: ``(True, data)``, or ``(False,
    None)`` when no complete marker-delimited JSON document is on ``stdout``."""
    lines = stdout.splitlines()
    try:
        begin = lines.index(EMIT_BEGIN)
        end = lines.index(EMIT_END, begin + 1)
    except ValueError:
        return False, None
    try:
        return True, json.loads("\n".join(lines[begin + 1 : end]))
    except ValueError:
        return False, None


def python_spec_problems(spec) -> "list[str]":
    """Why a query's ``python`` call spec is not well-formed (empty when it is)."""
    if not isinstance(spec, dict):
        return ["python must be a mapping"]
    problems = []
    unknown = set(spec) - set(PYTHON_SPEC_FIELDS)
    if unknown:
        problems.append("python: unknown fields {}".format(sorted(unknown)))
    target = spec.get("target")
    if target == "hiera":
        if spec.get("method") not in HIERA_METHODS:
            problems.append(
                "python: hiera method must be one of {}".format(HIERA_METHODS)
            )
        if "params" in spec:
            problems.append("python: params belongs to a types target")
    elif target == "types":
        if spec.get("method") not in TYPE_NAMES:
            problems.append("python: types method must be one of {}".format(TYPE_NAMES))
        if "block" in spec:
            problems.append("python: a types target takes no block")
        if "params" in spec and not isinstance(spec["params"], list):
            problems.append("python: params must be a list")
    else:
        problems.append("python: target must be hiera or types")
    if "args" in spec and not isinstance(spec["args"], list):
        problems.append("python: args must be a list")
    if "kwargs" in spec and not isinstance(spec["kwargs"], dict):
        problems.append("python: kwargs must be a mapping")
    if "block" in spec and spec["block"] not in BLOCK_NAMES:
        problems.append("python: block must be one of {}".format(BLOCK_NAMES))
    return problems


def _expression_digest_files() -> "list[Path]":
    return sorted(
        (p for p in FIXTURE_MODULES.rglob("*") if p.is_file()),
        key=lambda p: p.relative_to(FIXTURE_MODULES).as_posix(),
    )


def input_digest(case_dir: Path) -> str:
    """sha256 over everything Puppet saw when a case was recorded.

    That is DEFAULT_PUPPET_ARGS, NODE, the case's own ``puppet_args``, the
    PUPPET_FIELDS of every query, and every file in the case dir except
    ``golden.json``/``case.yaml``; a case with an expression query adds the
    fixture module's files. Line endings are normalized to LF first
    (except in a ``.raw.`` file, whose bytes are exact on purpose), so a
    CRLF checkout on Windows hashes the same as the LF checkout a golden
    was recorded from.
    """
    case = load_case(case_dir)
    h = hashlib.sha256()
    asked = {
        "default_puppet_args": list(DEFAULT_PUPPET_ARGS),
        "node": NODE,
        "puppet_args": case.get("puppet_args", []),
        "queries": [
            {k: q[k] for k in PUPPET_FIELDS if k in q} for q in case["queries"]
        ],
    }
    h.update(json.dumps(asked, sort_keys=True).encode("utf-8") + b"\0")
    # Sort by the relative POSIX path string, not Path objects: WindowsPath orders
    # case-insensitively, so mixed-case names (location-glob-order2) would hash
    # differently on Windows than on Linux/macOS.
    files = [p for p in case_dir.rglob("*") if p.is_file()]
    for path in sorted(files, key=lambda p: p.relative_to(case_dir).as_posix()):
        rel = path.relative_to(case_dir).as_posix()
        if rel in (GOLDEN, CASE_FILE):
            continue
        data = path.read_bytes()
        if ".raw." not in path.name:
            data = data.replace(b"\r\n", b"\n")
        h.update(rel.encode("utf-8") + b"\0")
        h.update(data + b"\0")
    if any("expression" in q for q in case["queries"]):
        for path in _expression_digest_files():
            rel = "fixture/" + path.relative_to(FIXTURE_MODULES).as_posix()
            h.update(rel.encode("utf-8") + b"\0")
            h.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()


def normalize_message(line: str, case_dir: Path, root: "str | None" = None) -> str:
    """Strip host- and run-specific parts of a Puppet message.

    Maps any path ending in ``cases/<case-name>`` to ``<case>``, an
    isolation root ``root`` (when given) to ``<iso>``, strips a leading
    ``Error: ``/``Warning: `` and an embedded ``Could not run: ``, and
    trailing whitespace.
    """
    line = line.strip()
    line = re.sub(r"\S*[\\/]cases[\\/]" + re.escape(case_dir.name), "<case>", line)
    if root:
        line = line.replace(root, "<iso>")
    line = re.sub(r"^(Error|Warning): (Could not run: )?", "", line)
    return line.strip()


def normalize_paths(text: str, case_dir: Path, root: "str | None" = None) -> str:
    """Quote-preserving path normalization for explain text/tree strings.

    An explain line such as ``Path "/abs/…/cases/<case>/data/a.yaml"`` would
    have its opening quote swallowed by :func:`normalize_message`'s ``\\S*``
    lead-in, so this uses a charclass that stops at a quote/paren/space
    instead. Unlike :func:`normalize_message` this never strips an
    ``Error:``/``Warning:`` prefix and never applies :func:`aio_inspect` --
    explain's own hash dump uses `` => `` verbatim, and normalizing that
    away would corrupt the text Puppet actually printed.
    """
    text = re.sub(
        r'[^\s"\'(]*[\\/]cases[\\/]' + re.escape(case_dir.name), "<case>", text
    )
    if root:
        text = text.replace(root, "<iso>")
    return text


def normalize_tree_paths(obj, case_dir: Path, root: "str | None" = None):
    """:func:`normalize_paths` applied recursively to every string leaf of a
    JSON-like structure (explain's ``tree``), including dict keys."""
    if isinstance(obj, str):
        return normalize_paths(obj, case_dir, root)
    if isinstance(obj, list):
        return [normalize_tree_paths(v, case_dir, root) for v in obj]
    if isinstance(obj, dict):
        return {
            (
                normalize_paths(k, case_dir, root) if isinstance(k, str) else k
            ): normalize_tree_paths(v, case_dir, root)
            for k, v in obj.items()
        }
    return obj


def aio_inspect(obj):
    """Ruby >=3.4 Hash#inspect (``"k" => "v"``) to the Ruby 3.2 AIO form
    (``"k"=>"v"``) that shipped when this suite was recorded.

    Only string leaves are touched, recursively, so this is a no-op for
    obj without an embedded Ruby inspect string.
    """
    if isinstance(obj, str):
        return obj.replace(" => ", "=>")
    if isinstance(obj, list):
        return [aio_inspect(v) for v in obj]
    if isinstance(obj, dict):
        return {k: aio_inspect(v) for k, v in obj.items()}
    return obj


def write_golden(case_dir: Path, golden: dict) -> None:
    text = json.dumps(golden, indent=2, ensure_ascii=False) + "\n"
    (case_dir / GOLDEN).write_bytes(text.encode("utf-8"))


def read_golden(case_dir: Path) -> dict:
    return json.loads((case_dir / GOLDEN).read_text(encoding="utf-8"))


def _leak_hits(text: str) -> list:
    return [p.pattern for p in _LEAK_PATTERNS if p.search(text)]


def string_leaves(obj) -> str:
    """Every string in a JSON-like value (keys included), one per line, so a
    leak scan reads the text itself and not its JSON escaping (an escaped
    newline after a colon would read as a drive prefix)."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(
            string_leaves(k) + "\n" + string_leaves(v) for k, v in obj.items()
        )
    if isinstance(obj, (list, tuple)):
        return "\n".join(string_leaves(v) for v in obj)
    return ""


def _pathlib_next_includes_hidden_by_default() -> bool:
    """Whether the installed pathlib_next's ``Path.glob`` defaults to matching dotfiles.

    The declared range ``pathlib_next>=0.9.0,<0.10`` spans ``include_hidden = False``
    (through 0.9.2) and ``True`` (from 0.9.11), so a glob-over-a-dotfile golden's
    divergence marker is gated on this probe.
    """
    import inspect

    from pathlib_next import Path as _PNPath

    default = inspect.signature(_PNPath.glob).parameters["include_hidden"].default
    return bool(default)


# Named runtime facts a divergence dict's ``when`` key may reference, alongside or
# instead of the platform guard ``on``. Each is a zero-argument callable returning
# a bool, evaluated per test run and never cached.
RUNTIME_PREDICATES = {
    "pathlib_next-includes-hidden": _pathlib_next_includes_hidden_by_default,
}


def _is_marker_valid(value) -> bool:
    if isinstance(value, str):
        return bool(_DIVERGENCE_ID_RE.match(value))
    if isinstance(value, list):
        return bool(value) and all(_is_marker_valid(v) for v in value)
    if isinstance(value, dict):
        # A bare `on:`/`when:` key is read as the boolean True by PyYAML's YAML 1.1
        # resolver; catching it makes a guard written `on: [...]` fail loudly instead of
        # never applying.
        if set(value) - {"id", "on", "when"}:
            return False
        if not _DIVERGENCE_ID_RE.match(value.get("id", "")):
            return False
        on = value.get("on")
        if not (
            on is None
            or (
                isinstance(on, list)
                and all(o in ("win32", "linux", "darwin") for o in on)
            )
        ):
            return False
        when = value.get("when")
        return when is None or when in RUNTIME_PREDICATES
    return False


#: A hash key quoted the way Puppet's own formatter quotes it; Ruby's
#: ``Hash#inspect`` uses double quotes.
_PUPPET_FORMATTED_HASH = re.compile(r"'[^']*' => ")


def hash_inspect_problems(qid, result) -> "list[str]":
    """Why a query may not set ``hash_inspect``: it rewrites Ruby's
    ``Hash#inspect`` into the Ruby 3.2 form, so the recorded value must be
    such output, never text Puppet's own formatter produced."""
    raw = result.get("raw_value")
    if result.get("status") != "found" or not isinstance(raw, str):
        return []
    if _PUPPET_FORMATTED_HASH.search(raw):
        return [
            "query {}: hash_inspect on a value Puppet's formatter produced "
            "({!r}), not Ruby inspect output".format(qid, raw)
        ]
    return []


def _format2_problems(golden: dict) -> "list[str]":
    problems = []
    if not isinstance(golden.get("platform"), str):
        problems.append("format 2 golden has no platform")
    gems = golden.get("gems")
    if not (isinstance(gems, dict) and set(gems) <= set(GEMS)):
        problems.append("format 2 golden has a malformed gems table")
    for qid, res in golden.get("results", {}).items():
        if not isinstance(res.get("exit_status"), int):
            problems.append("query {}: format 2 result has no exit_status".format(qid))
    return problems


#: Fields that only a key query (``puppet lookup``) can use.
_KEY_ONLY_FIELDS = ("merge", "default", "type", "explain", "hash_inspect")


def _expression_problems(index, query: dict) -> "list[str]":
    """The key-versus-expression rules for one query (``index`` names it before its
    id is known)."""
    label = query.get("id") or index
    has_key, has_expr = "key" in query, "expression" in query
    if has_key == has_expr:
        return ["query {}: exactly one of key and expression".format(label)]
    if not has_expr:
        if "python" in query:
            return ["query {}: python belongs to an expression query".format(label)]
        return []
    problems = []
    if not isinstance(query["expression"], str) or not query["expression"].strip():
        problems.append("query {}: expression must be a non-empty string".format(label))
    if not query.get("id"):
        problems.append("query {}: an expression query needs an id".format(label))
    for field in _KEY_ONLY_FIELDS:
        if field in query:
            problems.append(
                "query {}: {} not allowed with expression".format(label, field)
            )
    if "--modulepath" in (query.get("puppet_args") or []):
        problems.append(
            "query {}: --modulepath not allowed with expression".format(label)
        )
    if "python" not in query:
        problems.append("query {}: expression needs a python call spec".format(label))
    else:
        problems.extend(
            "query {}: {}".format(label, p)
            for p in python_spec_problems(query["python"])
        )
    return problems


def _apply_result_problems(qid, res) -> "list[str]":
    """What an expression query's recorded result must hold."""
    problems = []
    if res.get("channel") != "apply":
        problems.append("query {}: expression result needs channel apply".format(qid))
    status = res.get("status")
    if status not in ("found", "not_found", "error"):
        problems.append("query {}: apply result status {!r}".format(qid, status))
    if status == "found" and "value" not in res:
        problems.append("query {}: found result has no value".format(qid))
    if status == "error" and not isinstance(res.get("message"), str):
        problems.append("query {}: error result must have a message".format(qid))
    return problems


def lint_case(case_dir: Path) -> "list[str]":
    """Every reason `case_dir` is not a valid, current, safe-to-ship case.

    An empty list means the case passes `test_case_is_current`.
    """
    problems = []
    try:
        case = load_case(case_dir)
    except Exception as e:  # noqa: BLE001 - report, don't crash the lint
        return ["case.yaml did not parse: {!r}".format(e)]

    unknown_case_fields = set(case) - set(CASE_FIELDS)
    if unknown_case_fields:
        problems.append("unknown case fields: {}".format(sorted(unknown_case_fields)))

    queries = case.get("queries") or []
    if not queries:
        problems.append("no queries")

    ids = []
    for i, q in enumerate(queries):
        unknown = set(q) - set(QUERY_FIELDS)
        if unknown:
            problems.append("query {}: unknown fields {}".format(i, sorted(unknown)))
        problems.extend(_expression_problems(i, q))
        ids.append(query_id(q))
        if q.get("default") is not None and not isinstance(q["default"], str):
            problems.append("query {}: default must be a string".format(query_id(q)))
        for pa in q.get("puppet_args") or []:
            if not isinstance(pa, str):
                problems.append(
                    "query {}: puppet_args entries must be strings".format(query_id(q))
                )
        if "ordered" in q and not isinstance(q["ordered"], bool):
            problems.append("query {}: ordered must be a boolean".format(query_id(q)))
        if q.get("ordered") is False and not q.get("note"):
            problems.append(
                "query {}: ordered: false needs a note giving the reason".format(
                    query_id(q)
                )
            )
        if "divergence" in q and not _is_marker_valid(q["divergence"]):
            problems.append("query {}: malformed divergence marker".format(query_id(q)))
        if "deviation" in q:
            dev = q["deviation"]
            if not (
                isinstance(dev, dict)
                and isinstance(dev.get("id"), str)
                and "reason" in dev
                and isinstance(dev.get("expect"), dict)
                and "status" in dev["expect"]
            ):
                problems.append("query {}: malformed deviation".format(query_id(q)))
        if "explain" in q:
            if q["explain"] not in EXPLAIN_KINDS:
                problems.append(
                    "query {}: explain must be one of {}".format(
                        query_id(q), EXPLAIN_KINDS
                    )
                )
            if q.get("hash_inspect"):
                problems.append(
                    "query {}: explain queries never set hash_inspect".format(
                        query_id(q)
                    )
                )
            if "deviation" in q:
                problems.append(
                    "query {}: explain queries never set deviation".format(query_id(q))
                )
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        problems.append("duplicate query ids: {}".format(dupes))
    for pa in case.get("puppet_args") or []:
        if not isinstance(pa, str):
            problems.append("case puppet_args entries must be strings")

    facts_path = case_dir / "facts.yaml"
    if not facts_path.exists():
        problems.append("missing facts.yaml")
    else:
        facts = yaml.safe_load(facts_path.read_text(encoding="utf-8"))
        if not isinstance(facts, dict) or not facts:
            problems.append(
                "facts.yaml must be a non-empty mapping (Puppet rejects {})"
            )

    golden_path = case_dir / GOLDEN
    if not golden_path.exists():
        problems.append("no golden.json recorded; run record.py")
        return problems

    golden = read_golden(case_dir)
    if golden.get("format") not in SUPPORTED_FORMATS:
        problems.append("unsupported golden format {!r}".format(golden.get("format")))
    if golden.get("format") == 2:
        problems.extend(_format2_problems(golden))
    if (
        golden.get("puppet_version") != ORACLE["puppet"]
        or golden.get("ruby_version") != ORACLE["ruby"]
    ):
        problems.append(
            "golden recorded against puppet {}/ruby {}, expected {}".format(
                golden.get("puppet_version"), golden.get("ruby_version"), ORACLE
            )
        )
    if golden.get("inputs_sha256") != input_digest(case_dir):
        problems.append("stale golden.json (inputs changed); re-run record.py")

    results = golden.get("results", {})
    missing = sorted(set(ids) - set(results))
    extra = sorted(set(results) - set(ids))
    if missing:
        problems.append("golden.json missing results for {}".format(missing))
    if extra:
        problems.append("golden.json has stray results for {}".format(extra))

    hits = _leak_hits(string_leaves(golden))
    if hits:
        problems.append("golden.json leaks host/path patterns: {}".format(hits))

    for q in queries:
        qid = query_id(q)
        res = results.get(qid, {})
        status = res.get("status")
        if "expression" in q:
            problems.extend(_apply_result_problems(qid, res))
        elif res.get("channel") == "apply":
            problems.append("query {}: a key query never has channel apply".format(qid))
        if q.get("explain"):
            if status == "explained":
                if not isinstance(res.get("tree"), dict):
                    problems.append(
                        "query {}: explained result must have a dict tree".format(qid)
                    )
                text = res.get("text")
                if not (
                    isinstance(text, list) and all(isinstance(t, str) for t in text)
                ):
                    problems.append(
                        "query {}: explained result must have a list-of-str text".format(
                            qid
                        )
                    )
            elif status == "error":
                if not isinstance(res.get("message"), str):
                    problems.append(
                        "query {}: error result must have a message".format(qid)
                    )
            elif status is not None:
                problems.append(
                    "query {}: explain result status must be explained or error, got {!r}".format(
                        qid, status
                    )
                )
        elif status == "explained":
            problems.append(
                "query {}: a non-explain query never has status explained".format(qid)
            )

    for q in queries:
        if q.get("hash_inspect"):
            problems.extend(
                hash_inspect_problems(query_id(q), results.get(query_id(q), {}))
            )

    for q in queries:
        em = q.get("error_match")
        if not em:
            continue
        res = results.get(query_id(q), {})
        message = res.get("message", "")
        if res.get("status") != "error" or not re.search(em, message):
            problems.append(
                "query {}: error_match {!r} does not match recorded message {!r}".format(
                    query_id(q), em, message
                )
            )

    seen_casefold = {}
    for path in case_dir.rglob("*"):
        if path.is_symlink():
            problems.append("symlink not allowed: {}".format(path))
        key = str(path.relative_to(case_dir)).lower()
        if key in seen_casefold:
            problems.append(
                "case-fold collision: {} / {}".format(seen_casefold[key], path)
            )
        seen_casefold[key] = path

    return problems
