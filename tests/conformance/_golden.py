"""Schema, digest and lint helpers shared by the recorder and the replay tests.

Kept dependency-free apart from PyYAML so the replay runner imports it
without needing Puppet, and the recorder imports it without needing hyera.
Never import ``hyera`` from this module -- that seam is ``_ours.py`` only.
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
)
#: The subset of QUERY_FIELDS that changes what is asked of Puppet. Editing
#: anything else (divergence, deviation, ordered, error_match, error_class,
#: description, note) never invalidates a recording. `explain` is
#: included only when a query actually sets it, so every pre-existing
#: query's digest is unchanged by its addition.
PUPPET_FIELDS = (
    "id",
    "key",
    "merge",
    "default",
    "type",
    "puppet_args",
    "hash_inspect",
    "explain",
)
#: Values a query's ``explain`` field may take: ``data`` -> ``--explain``,
#: ``options`` -> ``--explain-options``. ``--explain --explain-options``
#: prints exactly what ``--explain`` alone prints (measured against the
#: oracle), so there is no third value.
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
    """A query's id: its explicit ``id``, else its key (or keys, ``|``-joined)."""
    if query.get("id"):
        return query["id"]
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


def input_digest(case_dir: Path) -> str:
    """sha256 over everything Puppet saw when a case was recorded.

    That is DEFAULT_PUPPET_ARGS, NODE, the case's own ``puppet_args``, the
    PUPPET_FIELDS of every query, and every file in the case dir except
    ``golden.json``/``case.yaml``. Line endings are normalized to LF first
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
    # Sort by the relative POSIX path *string*, never by comparing Path
    # objects directly: WindowsPath orders case-insensitively (lowercases
    # before comparing) while PosixPath is case-sensitive, so a case
    # naming its files with a mix of cases (location-glob-order2's
    # B.yaml/_x.yaml/a.yaml, deliberately -- it tests Puppet's own
    # byte-order glob sort) hashed its files in a different order on
    # Windows than on Linux/macOS, so a golden recorded on Windows read
    # "stale" in Linux/macOS CI even though not one byte had changed.
    # Found 2026-09-29 via a CI-only failure that never reproduced on the
    # Windows recording box.
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
    """Whether the installed pathlib_next's ``Path.glob`` defaults to
    matching dotfiles.

    ``critic-engineering/glob-semantics-drift-with-pathlib-next-patch``:
    the declared range ``pathlib_next>=0.9.0,<0.10`` covers two
    behaviors -- ``include_hidden: bool = False`` through 0.9.2 (and the
    floor, 0.9.0), ``= True`` from 0.9.11. A glob-over-a-dotfile golden's
    outcome therefore depends on which patch is actually resolved, not
    just on our own code, so its divergence marker is gated on this
    runtime probe rather than applying unconditionally.
    """
    import inspect

    from pathlib_next import Path as _PNPath

    default = inspect.signature(_PNPath.glob).parameters["include_hidden"].default
    return bool(default)


#: Named runtime facts a divergence dict's ``when`` key may reference,
#: alongside (or instead of) the platform guard ``on``. Every predicate is
#: a zero-argument callable returning a bool, evaluated fresh per test run
#: (never cached: a re-install between runs must be picked up).
RUNTIME_PREDICATES = {
    "pathlib_next-includes-hidden": _pathlib_next_includes_hidden_by_default,
}


def _is_marker_valid(value) -> bool:
    if isinstance(value, str):
        return bool(_DIVERGENCE_ID_RE.match(value))
    if isinstance(value, list):
        return bool(value) and all(_is_marker_valid(v) for v in value)
    if isinstance(value, dict):
        # A bare `on:` (or `when:`) key is read back as the boolean True
        # by PyYAML's YAML-1.1 resolver (the same "Norway problem" as
        # unquoted off/on/yes elsewhere in a case.yaml) -- catch it here
        # so a guard silently authored as `on: [...]` instead of
        # `"on": [...]` fails loudly instead of never applying.
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
        if "key" not in q:
            problems.append("query {}: missing key".format(i))
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
