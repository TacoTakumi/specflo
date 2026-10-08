"""`mb.py grade --tree DIR --level LEVEL`: grade a final tree and print the result as JSON.

The JSON's `score` (passed / total held-out tests) is the run record's `score`.
Exits 1 with a message on stderr when the tree cannot be graded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from modelbench import grader, heldout


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mb.py grade", description=__doc__.splitlines()[0])
    parser.add_argument("--tree", required=True, type=Path, help="the run's final tree")
    parser.add_argument("--level", required=True, help="quick, fast or full")
    parser.add_argument("--archive", type=Path, default=grader.ARCHIVE, help="held-out archive")
    parser.add_argument(
        "--grading-dir", type=Path, default=None,
        help="fresh dir outside the repo and every workdir (default: a new temp dir)",
    )
    parser.add_argument(
        "--workdir", action="append", type=Path, default=[],
        help="another run workdir the grading dir must stay out of (repeat)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the grading dir")
    parser.add_argument("--timeout", type=float, default=grader.DEFAULT_TIMEOUT, help="seconds")
    args = parser.parse_args(argv)
    try:
        result = grader.grade(
            args.tree, args.level, archive=args.archive, grading_dir=args.grading_dir,
            workdirs=args.workdir, keep=args.keep, timeout=args.timeout,
        )
    except (grader.GraderError, heldout.HeldoutError, OSError) as exc:
        print(f"mb.py grade: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0
