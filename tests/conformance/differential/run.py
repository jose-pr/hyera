"""Generate scenarios, run Puppet and hyera over them, and report disagreements.

usage: python run.py [--runner local|wsl|wsl:<distro>] [--area NAME ...|all]
                     [--seed N] [--count M] [--keep DIR] [--record-corpus]

One summary line per area, then every disagreement no declared difference
explains, in full. Exit status: 0 everything agrees or is a declared
difference, 1 something is unclassified, 2 the reference could not be run.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))  # tests/conformance: the recorder's modules

from differential import corpus, batch  # noqa: E402
from differential.merge import run as merge_run  # noqa: E402
from differential.reference import (
    ReferenceError,
    echo,
    reference_versions,
)  # noqa: E402
from differential.rules import OPEN  # noqa: E402
from differential.scenarios import AREAS  # noqa: E402

#: Area names that are not scenario areas.
MERGE_AREA = "merge"
DEFAULT_SEED = 1
DEFAULT_COUNT = 200


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py", description="Differential comparison of hyera with Puppet."
    )
    parser.add_argument(
        "--runner",
        default="wsl" if sys.platform == "win32" else "local",
        help="where Puppet runs: local, wsl or wsl:<distro>",
    )
    parser.add_argument(
        "--area",
        nargs="+",
        default=["all"],
        help="area names, or all ({} and {})".format(", ".join(AREAS), MERGE_AREA),
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--count", type=int, default=DEFAULT_COUNT, help="scenarios (merges) per area"
    )
    parser.add_argument("--keep", help="keep the generated trees and results here")
    parser.add_argument("--puppet-workers", type=int, default=8)
    parser.add_argument("--hyera-workers", type=int, default=1)
    parser.add_argument(
        "--record-corpus",
        action="store_true",
        help="write the replay corpus from this run (see corpus/)",
    )
    parser.add_argument("--show", type=int, default=40, help="most disagreements shown")
    return parser.parse_args(argv)


def _areas(names: List[str]) -> List[str]:
    if "all" in names:
        return list(AREAS) + [MERGE_AREA]
    known = set(AREAS) | {MERGE_AREA}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise SystemExit("unknown area(s): {}".format(", ".join(unknown)))
    return names


def _line(area: str, stats: dict) -> str:
    return (
        "{:<14} scenarios {:>5} queries {:>6} agree {:>6} declared {:>5} "
        "open {:>3} unclassified {:>4}".format(
            area,
            stats["scenarios"],
            stats["queries"],
            stats["agree"],
            sum(stats["declared"].values()),
            sum(stats["open"].values()),
            stats["unclassified"],
        )
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse(argv)
    areas = _areas(args.area)
    if args.record_corpus:
        areas = list(AREAS) + [MERGE_AREA]
    root = (
        Path(args.keep) if args.keep else Path(tempfile.mkdtemp(prefix="hyera-diff-"))
    )
    root.mkdir(parents=True, exist_ok=True)
    declared: dict = {}
    open_seen: dict = {}
    shown = 0
    unclassified_total = 0
    recordings = []
    try:
        for area in areas:
            seed, count, limit = args.seed, args.count, None
            if args.record_corpus:
                plan = corpus.MERGE_PLAN if area == MERGE_AREA else corpus.PLAN[area]
                seed, count = plan.seed, plan.count
                limit = plan.max_queries or None
            try:
                if area == MERGE_AREA:
                    stats, bad, record = merge_run.run(
                        args.runner, seed, count, root, echo
                    )
                else:
                    rows = batch.run_area(
                        args.runner,
                        area,
                        seed,
                        count,
                        root,
                        args.puppet_workers,
                        args.hyera_workers,
                        echo,
                        limit,
                    )
                    stats = batch.summarize(rows)
                    bad = [batch.describe(r) for r in rows if r.unclassified]
                    record = corpus.Recording(area, seed, count, rows)
            except ReferenceError as e:
                print("the reference could not be run: {}".format(e), file=sys.stderr)
                return 2
            print(_line(area, stats))
            for rule, n in stats["declared"].items():
                declared[rule] = declared.get(rule, 0) + n
            for key, n in stats["open"].items():
                open_seen[key] = open_seen.get(key, 0) + n
            unclassified_total += stats["unclassified"]
            for text in bad:
                if shown < args.show:
                    print(text)
                shown += 1
            recordings.append(record)
        print("declared differences seen:")
        for rule in sorted(declared):
            print("  {:<44} {}".format(rule, declared[rule]))
        what = {d.key: d.what for d in OPEN}
        print("open defects seen (not declared; fix or declare each):")
        for key in sorted(open_seen):
            print("  {:<44} {}  {}".format(key, open_seen[key], what[key]))
        for key in sorted(set(what) - set(open_seen)):
            if set(areas) >= set(AREAS):
                print("  {:<44} not seen: remove its entry if fixed".format(key))
        if args.record_corpus:
            if unclassified_total:
                print("not recording: unclassified disagreements", file=sys.stderr)
                return 1
            try:
                corpus.write(recordings, reference_versions(args.runner))
            except ReferenceError as e:
                print("the reference could not be run: {}".format(e), file=sys.stderr)
                return 2
    finally:
        if not args.keep:
            batch.discard(root)
    return 1 if unclassified_total else 0


if __name__ == "__main__":
    sys.exit(main())
