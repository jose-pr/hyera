"""Record golden results for the conformance cases from real Puppet (dev-only).

usage: python record.py [--runner local|wsl|wsl:<distro>] [--jobs N]
                         [--check] [--list-markers] [CASE ...]

Each query in ``cases/<case>/case.yaml`` is one isolated ``puppet lookup`` run
(Puppet 8.10), or, for an ``expression`` query, one ``puppet apply`` run;
``--check`` diffs in memory and prints ``DRIFT <case>::<id>``.
"""

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from _golden import (
    CASES,
    DEFAULT_PUPPET_ARGS,
    FIXTURE_MODULES,
    FORMAT,
    GEMS,
    NODE,
    ORACLE,
    aio_inspect,
    apply_argv,
    case_dirs,
    input_digest,
    load_case,
    lookup_argv,
    normalize_message,
    normalize_paths,
    normalize_tree_paths,
    parse_emitted,
    query_id,
    read_golden,
    string_leaves,
    write_golden,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_NOT_FOUND = re.compile(r"did not find a value for the name")
_LEAK_HOST_PATTERNS = (
    re.compile(r"/mnt/"),
    re.compile(r"/home/"),
    re.compile(r"/tmp/"),
    re.compile(r"/Users/"),
    re.compile(r"[A-Za-z]:\\"),
)
_IDENTITY_FACTS = (
    "networking.fqdn",
    "networking.hostname",
    "networking.domain",
    "networking.ip",
    "networking.ip6",
)


def _iso_root(runner: str) -> str:
    return (
        "/tmp/hiera-golden"
        if runner.startswith("wsl")
        else str(Path(tempfile.gettempdir()) / "hiera-golden")
    )


def _iso_args(root: str) -> list:
    return [
        "--confdir",
        root + "/conf",
        "--codedir",
        root + "/code",
        "--vardir",
        root + "/var",
        "--logdir",
        root + "/log",
        "--rundir",
        root + "/run",
        "--environmentpath",
        "./environments",
        "--basemodulepath",
        "./modules",
    ]


def _command(runner: str, case_dir: Path, args: list, program: str = "puppet"):
    if runner == "local":
        return [program] + args, case_dir
    kind, _, distro = runner.partition(":")
    if kind != "wsl":
        raise SystemExit(
            "unknown runner {!r} (want local, wsl or wsl:<distro>)".format(runner)
        )
    prefix = ["wsl.exe"]
    if distro:
        prefix += ["-d", distro]
    # `-e <command>`, not `-- <command>`: wsl.exe's `--` relays argv through a Linux
    # shell that strips a lone or wrapping single quote (`ruby -e 'puts ARGV.inspect'
    # "'a.b'"` gives `["a.b"]` with `--`, `["'a.b'"]` with `-e`).
    return prefix + ["--cd", str(case_dir), "-e", program] + args, None


def _run(runner: str, case_dir: Path, args: list, program: str = "puppet"):
    cmd, cwd = _command(runner, case_dir, args, program)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=120)
    dec = lambda b: _ANSI.sub("", b.decode("utf-8", "replace"))  # noqa: E731
    return p.returncode, dec(p.stdout), dec(p.stderr)


def _error_text(lines: list, case_dir: Path, root: str) -> str:
    """Every line of a Puppet error: from the first ``Error:`` line to the end,
    ``Warning:`` lines and blanks dropped, each normalized."""
    start = next((i for i, l in enumerate(lines) if l.startswith("Error:")), None)
    if start is None:
        return ""
    kept = [
        normalize_message(l, case_dir, root)
        for l in lines[start:]
        if l.strip() and not l.startswith("Warning:")
    ]
    return "\n".join(kept)


def _leak_scan(obj, root: str, identities: "tuple") -> list:
    text = string_leaves(obj)
    hits = [p.pattern for p in _LEAK_HOST_PATTERNS if p.search(text)]
    for ident in identities:
        if ident and len(ident) >= 4 and ident in text:
            hits.append(ident)
    if root and root in text:
        hits.append(root)
    return hits


_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def _query_root(root: str, case_dir: Path, query: dict) -> str:
    """A private isolation root per query.

    Puppet's SSL/dir bootstrap on a fresh confdir/vardir is not concurrency
    safe: two processes racing to create the same directory tree both fail
    with "File exists @ dir_s_mkdir". Giving every query its own subtree
    under `root` keeps `--jobs` concurrency without that race.
    """
    slug = _UNSAFE_PATH_CHARS.sub("_", query_id(query))
    return "/".join([root, case_dir.name, slug])


def _runner_path(runner: str, path: Path) -> str:
    """``path`` as the program run by ``runner`` names it: a ``/mnt/<drive>/...``
    path for a WSL runner, the path itself for a local one."""
    if not runner.startswith("wsl"):
        return str(path)
    return _mnt_path(path.resolve().as_posix())


def _mnt_path(posix: str) -> str:
    """``C:/a/b`` as WSL mounts it, ``/mnt/c/a/b``; a path with no drive
    unchanged."""
    drive, sep, rest = posix.partition(":")
    if not sep or len(drive) != 1:
        return posix
    return "/mnt/" + drive.lower() + rest


def _apply_command(runner: str, case: dict, query: dict, iso: list) -> list:
    """The ``puppet apply`` arguments for an expression query: the isolation
    settings, the case's own module directory plus the fixture module, and the
    facts terminus that reads ``./facts.yaml``."""
    modules = "./modules:" + _runner_path(runner, FIXTURE_MODULES)
    iso = [modules if a == "./modules" else a for a in iso]
    expression = "hyera_fixture::emit({})".format(query["expression"])
    return (
        ["apply", "--color", "false"]
        + iso
        + [
            "--facts_terminus",
            "hyera_file",
            "--hiera_config",
            "./hiera.yaml",
            "--node_name_value",
            NODE,
        ]
        + apply_argv(case, query)
        + ["-e", expression]
    )


def record_apply_query(
    runner: str, case_dir: Path, case: dict, query: dict, root: str, identities: "tuple"
) -> dict:
    """One expression query: ``puppet apply`` evaluates it and the fixture function
    writes the value (Puppet's own rich-data conversion) between two marker lines."""
    root = _query_root(root, case_dir, query)
    args = _apply_command(runner, case, query, _iso_args(root))
    rc, out, err = _run(runner, case_dir, args)
    result = {"channel": "apply", "exit_status": rc}
    warnings = [
        normalize_message(l, case_dir, root)
        for l in err.splitlines()
        if l.startswith("Warning:")
    ]
    if warnings:
        result["warnings"] = warnings
    emitted, value = parse_emitted(out)
    if rc == 0 and emitted:
        result["status"] = "found"
        result["value"] = value
    else:
        text = _error_text(err.splitlines(), case_dir, root)
        if _NOT_FOUND.search(text):
            result["status"] = "not_found"
        else:
            result["status"] = "error"
            result["message"] = text or "no value was emitted (exit status {})".format(
                rc
            )
    _refuse_leak(result, root, identities, case_dir, query)
    return result


def _refuse_leak(result, root, identities, case_dir, query) -> None:
    hits = _leak_scan(result, root, identities)
    if hits:
        raise SystemExit(
            "refusing to record a leaking result for {}::{}: {}".format(
                case_dir.name, query_id(query), hits
            )
        )


def record_query(
    runner: str, case_dir: Path, case: dict, query: dict, root: str, identities: "tuple"
) -> dict:
    if "expression" in query:
        return record_apply_query(runner, case_dir, case, query, root, identities)
    root = _query_root(root, case_dir, query)
    iso = _iso_args(root)
    tail = lookup_argv(case, query)
    base = (
        ["lookup"]
        + iso
        + [
            "--hiera_config",
            "./hiera.yaml",
            "--facts",
            "./facts.yaml",
            "--node",
            NODE,
        ]
    )
    rc, out, err = _run(runner, case_dir, base + ["--render-as", "json"] + tail)
    result = {"argv": tail, "exit_status": rc}
    warnings = [
        normalize_message(l, case_dir, root)
        for l in err.splitlines()
        if l.startswith("Warning:")
    ]
    if warnings:
        result["warnings"] = warnings
    if query.get("explain"):
        # Not the --explain fallback below: --explain is already in `tail`, and a
        # swallowed LookupError is Puppet's last text line; rc is 0 whenever the report
        # printed (application/lookup.rb:305-335).
        errors = [l for l in err.splitlines() if l.startswith("Error:")]
        if rc != 0 or errors or not out.strip():
            result["status"] = "error"
            result["message"] = _error_text(
                err.splitlines(), case_dir, root
            ) or normalize_message(out.strip() or err.strip(), case_dir, root)
        else:
            _, s_out, s_err = _run(runner, case_dir, base + ["--render-as", "s"] + tail)
            result["status"] = "explained"
            result["tree"] = normalize_tree_paths(json.loads(out), case_dir, root)
            result["text"] = [
                normalize_paths(l, case_dir, root) for l in s_out.splitlines()
            ]
        hits = _leak_scan(result, root, identities)
        if hits:
            raise SystemExit(
                "refusing to record a leaking result for {}::{}: {}".format(
                    case_dir.name, query_id(query), hits
                )
            )
        return result
    if rc == 0 and out.strip():
        result["status"] = "found"
        result["value"] = json.loads(out)
    else:
        errors = [l for l in err.splitlines() if l.startswith("Error:")]
        if errors:
            result["status"] = "error"
            result["message"] = _error_text(err.splitlines(), case_dir, root)
        else:
            # Exit 1 with no output is ambiguous: a miss and a swallowed LookupError
            # look alike and only the last line of --explain tells them apart. A benign
            # stderr Warning: accompanies every call, so read stdout's last line first.
            _, out2, err2 = _run(runner, case_dir, base + ["--explain"] + tail)
            out2_lines = [l for l in out2.splitlines() if l.strip()]
            err2_lines = [l for l in err2.splitlines() if l.strip()]
            last = (
                out2_lines[-1] if out2_lines else (err2_lines[-1] if err2_lines else "")
            )
            if _NOT_FOUND.search(last):
                result["status"] = "not_found"
            else:
                result["status"] = "error"
                result["message"] = _error_text(
                    err2_lines, case_dir, root
                ) or normalize_message(last, case_dir, root)

    if query.get("hash_inspect") and result.get("status") == "found":
        normalized = aio_inspect(result["value"])
        if normalized != result["value"]:
            result["raw_value"] = result["value"]
            result["value"] = normalized

    hits = _leak_scan(result, root, identities)
    if hits:
        raise SystemExit(
            "refusing to record a leaking result for {}::{}: {}".format(
                case_dir.name, query_id(query), hits
            )
        )
    return result


def record_case(
    runner: str,
    case_dir: Path,
    jobs: int,
    versions: dict,
    root: str,
    identities: "tuple",
) -> dict:
    case = load_case(case_dir)
    queries = case["queries"]
    ids = [query_id(q) for q in queries]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(
            "{}: duplicate query ids {}".format(case_dir.name, sorted(dupes))
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(
            pool.map(
                lambda q: record_query(runner, case_dir, case, q, root, identities),
                queries,
            )
        )
    return {
        "format": FORMAT,
        "puppet_version": versions["puppet"],
        "ruby_version": versions["ruby"],
        "platform": versions["platform"],
        "gems": versions["gems"],
        "node": NODE,
        "inputs_sha256": input_digest(case_dir),
        "results": dict(zip(ids, results)),
    }


def _versions(runner: str, root: str):
    """One-time, sequential: puppet --version, and a facts probe that also
    creates the isolation directories before the query pool starts.

    Returns ``(versions, identities)``: `versions` has `puppet`/`ruby`;
    `identities` is the tuple of this recording host's fqdn/hostname/
    domain/ip/ip6 -- never written to a golden, kept only to refuse a
    golden that accidentally contains them (server_facts carries these too).
    """
    _, out, _ = _run(runner, CASES, ["--version"])
    puppet_version = out.strip()
    probe_dir = CASES  # any cwd; the facts probe writes only under `root`.
    _, facts_out, _ = _run(
        runner,
        probe_dir,
        ["facts", "show"]
        + _iso_args(root)
        + ["--render-as", "json"]
        + list(_IDENTITY_FACTS)
        + ["ruby.version", "ruby.platform"],
    )
    ruby_version = "unknown"
    platform = "unknown"
    identities = ()
    try:
        facts = json.loads(facts_out)
        ruby_version = facts.get("ruby.version", ruby_version)
        platform = facts.get("ruby.platform", platform)
        identities = tuple(str(facts[f]) for f in _IDENTITY_FACTS if facts.get(f))
    except ValueError:
        pass
    return {
        "puppet": puppet_version,
        "ruby": ruby_version,
        "platform": platform,
        "gems": _gem_versions(runner),
    }, identities


def _gem_versions(runner: str) -> dict:
    """The installed version of each gem whose behaviour a backend ports."""
    _, out, _ = _run(runner, CASES, ["list", "--local"] + list(GEMS), program="gem")
    found = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+) \((?:default: )?([^,)]+)", line)
        if m and m.group(1) in GEMS:
            found[m.group(1)] = m.group(2)
    return dict(sorted(found.items()))


def list_markers() -> int:
    divergence_counts = {}
    deviations = []
    for case_dir in case_dirs():
        case = load_case(case_dir)
        for q in case["queries"]:
            d = q.get("divergence")
            if d:
                ids = d if isinstance(d, list) else [d]
                for entry in ids:
                    key = entry["id"] if isinstance(entry, dict) else entry
                    divergence_counts[key] = divergence_counts.get(key, 0) + 1
            dev = q.get("deviation")
            if dev:
                deviations.append(
                    (case_dir.name, query_id(q), dev["id"], dev["reason"])
                )
    for did in sorted(divergence_counts):
        print("{}\t{}".format(did, divergence_counts[did]))
    for case_name, qid, dev_id, reason in deviations:
        print("deviation\t{}::{}\t{}\t{}".format(case_name, qid, dev_id, reason))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runner", default="local")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list-markers", action="store_true")
    ap.add_argument("cases", nargs="*")
    opts = ap.parse_args(argv)

    if opts.list_markers:
        return list_markers()

    root = _iso_root(opts.runner)
    versions, identities = _versions(opts.runner, root)
    if not versions["puppet"] or ORACLE["puppet"] not in versions["puppet"]:
        print(
            "warning: oracle reports puppet {!r}, this suite expects {}".format(
                versions["puppet"], ORACLE["puppet"]
            ),
            file=sys.stderr,
        )

    names = opts.cases or sorted(p.name for p in case_dirs())
    drift = 0
    for name in names:
        case_dir = CASES / name
        golden = record_case(
            opts.runner, case_dir, opts.jobs, versions, root, identities
        )
        if opts.check:
            try:
                old = read_golden(case_dir)
            except FileNotFoundError:
                print("DRIFT {}: no golden.json recorded yet".format(name))
                drift += 1
                continue
            for qid, res in golden["results"].items():
                if old["results"].get(qid) != res:
                    drift += 1
                    print(
                        "DRIFT {}::{}\n  old {}\n  new {}".format(
                            name, qid, old["results"].get(qid), res
                        )
                    )
        else:
            write_golden(case_dir, golden)
            counts = {}
            for res in golden["results"].values():
                counts[res["status"]] = counts.get(res["status"], 0) + 1
            print(
                "recorded {} with puppet {}: {}".format(
                    name, versions["puppet"], counts
                )
            )
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
