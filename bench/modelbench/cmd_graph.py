"""`mb.py graph`: pass-rate and behaviour-metric graphs with disclosures.

Exits 1, naming the fields, when the arms of a comparison differ in more than
one variable or in a setting; nothing is written then.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from modelbench import arms, graphs


def _arm(value: str) -> tuple[str, str]:
    entry, sep, harness = value.rpartition(":")
    if not sep or not entry or not harness:
        raise argparse.ArgumentTypeError(f"expected ENTRY:HARNESS, got {value!r}")
    return entry, harness


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mb.py graph", description=__doc__)
    parser.add_argument("--records", required=True, type=Path, help="directory of run records")
    parser.add_argument("--out", required=True, type=Path, help="directory for the images")
    parser.add_argument(
        "--arm", action="append", type=_arm, metavar="ENTRY:HARNESS",
        help="graph only these arms (repeat); default every arm in the records",
    )
    parser.add_argument(
        "--config", type=Path, default=arms.DEFAULT_CONFIG,
        help="arm config that names each entry's engine",
    )
    args = parser.parse_args(argv)
    try:
        config = arms.load_config(args.config)
        engines = {e: str(spec["engine"]) for e, spec in config.entries.items()}
        records = graphs.load_records(args.records)
        written = graphs.generate(records, args.out, engines, args.arm)
    except (graphs.GraphError, arms.ArmError) as exc:
        print(f"mb.py graph: {exc}", file=sys.stderr)
        return 1
    for image, _ in written:
        print(image)
    return 0
