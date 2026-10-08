"""`mb.py records [--check] [--matrix NAME]`: the run records under the runs dir, and the matrix check.

Without `--check`: one line per run dir (valid, invalid or incomplete, with
end reason and score).

`--check --matrix NAME`: per cell of the matrix, the count of valid records
against the count it needs, each invalid record with why, and each run dir
with no record (an interrupted run; listed, it does not fail the check).
Exit 1 when a cell is short or holds an invalid record, else 0. An invalid
record keeps the check failing until its run dir is moved out of the runs
dir, even when the cell has enough valid records.

`--check` without `--matrix`: exit 1 when any record under the runs dir is
invalid. Exit 2 for a bad matrix or arm config.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from modelbench import arms, batch, run


def _line(item: batch.RunDir) -> str:
    rec = item.record or {}
    tail = ""
    if rec:
        tail = f" {rec.get('end_reason')}, score {float(rec.get('score') or 0.0):.3f}, started {rec.get('started_at')}"
    why = f" ({'; '.join(item.problems)})" if item.problems else ""
    return f"{item.name}: {item.state}{tail}{why}"


def check_matrix(matrix: batch.Matrix, runs_dir: Path) -> tuple[list[str], int]:
    lines = [f"matrix {matrix.name}: {len(list(matrix.cells()))} cells, {matrix.runs} valid record(s) each"]
    short = invalid = 0
    for cell in batch.check_cells(matrix, runs_dir):
        status = "ok" if cell.ok else "SHORT" if cell.valid < cell.required else "INVALID"
        extra = f", {len(cell.invalid)} invalid" if cell.invalid else ""
        extra += f", {len(cell.incomplete)} without record" if cell.incomplete else ""
        lines.append(f"  {cell.entry} / {cell.harness} / {cell.level}: "
                     f"{cell.valid}/{cell.required} valid{extra}  {status}")
        for item in cell.invalid:
            lines.append(f"    invalid {_line(item)}")
        for item in cell.incomplete:
            lines.append(f"    no record: {item.name}")
        short += cell.valid < cell.required
        invalid += len(cell.invalid)
    if short or invalid:
        lines.append(f"check: FAIL - {short} cell(s) short, {invalid} invalid record(s)")
        return lines, 1
    lines.append("check: ok")
    return lines, 0


def check_all(runs_dir: Path) -> tuple[list[str], int]:
    found = batch.scan_runs(runs_dir)
    bad = [r for r in found if r.state == "invalid"]
    lines = [f"invalid {_line(r)}" for r in bad]
    lines.append(f"check: {'FAIL' if bad else 'ok'} - {len(found)} run dir(s), {len(bad)} invalid record(s)")
    return lines, 1 if bad else 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="mb.py records", description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="check the records (per matrix cell with --matrix)")
    p.add_argument("--matrix", default=None, help="matrix name in the matrix file")
    p.add_argument("--matrices", default=str(batch.DEFAULT_MATRICES), help="matrix file")
    p.add_argument("--config", default=str(arms.DEFAULT_CONFIG), help="arm config")
    p.add_argument("--runs-dir", type=Path, default=run.RUNS_DIR, help="where run records go")
    args = p.parse_args(argv)
    if args.matrix is not None and not args.check:
        p.error("--matrix needs --check")
    if not args.check:
        for item in batch.scan_runs(args.runs_dir):
            print(_line(item))
        return 0
    if args.matrix is None:
        lines, code = check_all(args.runs_dir)
    else:
        try:
            matrix = batch.load_matrix(args.matrix, arms.load_config(args.config), args.matrices)
        except (arms.ArmError, batch.MatrixError, OSError) as exc:
            print(f"mb.py records: {exc}", file=sys.stderr)
            return 2
        lines, code = check_matrix(matrix, args.runs_dir)
    for line in lines:
        print(line)
    return code
