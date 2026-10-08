"""`mb.py batch --matrix NAME [--dry-run]`: run a matrix of arms, levels and run counts in entry blocks.

The matrix comes from bench/matrices.yaml. The batch runs one block per
entry so llama-swap loads each entry once, starts only the runs each cell
still lacks (a rerun resumes an interrupted batch), starts at most one
full-level Claude Code run per night, and refuses a harness's runs on an entry
whose reasoning probe differs from the other entry's. See modelbench.batch for
the rules.

`--dry-run` lists the blocks, probe steps and runs and sends nothing to
llama-swap. Exit 0 when every run allowed tonight ran (runs left by the night
rule wait for a rerun), 1 when a run or probe stopped the batch or a probe
refused a comparison, 2 for a bad matrix or arm config, 130 on an interrupt.

Each run is `mb.py run` with the bench-wide settings: the level limits of the
arm config (else the runner's provisional ones), autonomy and pass cap.
"""

from __future__ import annotations

import argparse
import functools
import sys
from pathlib import Path

from modelbench import arms, batch, preflight, probe, run


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mb.py batch", description=__doc__.splitlines()[0])
    p.add_argument("--matrix", required=True, help="matrix name in the matrix file (e.g. pilot, first-round)")
    p.add_argument("--dry-run", action="store_true", help="list the blocks, probes and runs; start nothing")
    p.add_argument("--matrices", default=str(batch.DEFAULT_MATRICES), help="matrix file")
    p.add_argument("--headless", action="store_true", help="no herdr pane")
    p.add_argument("--config", default=str(arms.DEFAULT_CONFIG), help="arm config")
    p.add_argument("--base-url", default=preflight.DEFAULT_BASE_URL, help="llama-swap")
    p.add_argument("--state", default=str(preflight.DEFAULT_STATE), help="bench-load state file")
    p.add_argument("--runs-dir", type=Path, default=run.RUNS_DIR, help="where run records and probes go")
    p.add_argument("--work-root", type=Path, default=run.WORK_ROOT, help="where sealed workdirs go")
    p.add_argument("--llama-swap-log", type=Path, default=run.LLAMA_SWAP_LOG, help="llama-swap log")
    p.add_argument("--probe-timeout", type=float, default=probe.DEFAULT_TIMEOUT,
                   help="seconds per model load and per probe")
    return p


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        config = arms.load_config(args.config)
        matrix = batch.load_matrix(args.matrix, config, args.matrices)
        plan = batch.make_plan(matrix, config, runs_dir=args.runs_dir, work_root=args.work_root)
    except (arms.ArmError, batch.MatrixError, OSError) as exc:
        print(f"mb.py batch: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        for line in batch.describe_plan(plan, runs_dir=args.runs_dir):
            print(line)
        return 0
    rig = run.Rig(args.base_url, args.state)
    run_fn = functools.partial(
        run.run_one, config=config, autonomy=run.AUTONOMY, pass_cap=run.PASS_CAP, rig=rig,
        runs_dir=args.runs_dir, work_root=args.work_root, llama_swap_log=args.llama_swap_log,
        headless=args.headless,
    )
    probe_fn = batch.rig_probe(rig, config, args.runs_dir / batch.PROBES_DIR_NAME, timeout=args.probe_timeout)
    out = functools.partial(print, flush=True)
    try:
        outcome = batch.run_batch(plan, config=config, run_fn=run_fn, probe_fn=probe_fn,
                                  runs_dir=args.runs_dir, work_root=args.work_root, out=out)
    except KeyboardInterrupt:
        print("mb.py batch: interrupted; rerun the batch to start the runs still missing", file=sys.stderr)
        return 130
    for line in batch.summary(outcome, plan, args.runs_dir):
        out(line)
    return outcome.exit_code
