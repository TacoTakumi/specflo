"""`mb.py run --entry E --harness H --level L [--run-index N]`: one bench run to a scored, checked run record.

The run is refused (exit 2) when the entry, harness or level is missing or not
in the arm config, or the run index is not a non-negative int. Without
`--run-index`, the run takes the lowest index of this arm and level that has
not run yet. Exit 1 when preflight or the harness start refuses the run; 0 once
the record is written, whatever the run's end reason. The record path and a
summary are printed.

Level limits: `--wall-clock` and `--stall-limit` (seconds), else the level's
settings in the arm config, else the runner's provisional defaults. The auto
settings (autonomy, pass cap) are bench-wide; the defaults are the runner's.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from modelbench import arms, launch_pi, lifecycle, preflight, probe, run
from modelbench import workdir as wd


def _index(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a non-negative int, got {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative int, got {text!r}")
    return value


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected seconds, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number of seconds, got {text!r}")
    return value


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mb.py run", description=__doc__.splitlines()[0])
    p.add_argument("--entry", help="llama-swap entry id (an entry of the arm config)")
    p.add_argument("--harness", help="pi or claude-code")
    p.add_argument("--level", help="quick, fast or full")
    p.add_argument("--run-index", type=_index, default=None,
                   help="run index (default: the lowest one of this arm and level not run yet)")
    p.add_argument("--wall-clock", type=_seconds, default=None, help="wall-clock cap, seconds")
    p.add_argument("--stall-limit", type=_seconds, default=None, help="stall limit, seconds")
    p.add_argument("--autonomy", default=run.AUTONOMY, help=f"specflo auto autonomy (default {run.AUTONOMY})")
    p.add_argument("--pass-cap", type=int, default=run.PASS_CAP,
                   help=f"specflo auto pass cap (default {run.PASS_CAP})")
    p.add_argument("--headless", action="store_true", help="no herdr pane")
    p.add_argument("--config", default=str(arms.DEFAULT_CONFIG), help="arm config")
    p.add_argument("--base-url", default=preflight.DEFAULT_BASE_URL, help="llama-swap")
    p.add_argument("--state", default=str(preflight.DEFAULT_STATE), help="bench-load state file")
    p.add_argument("--runs-dir", type=Path, default=run.RUNS_DIR, help="where run records and raw logs go")
    p.add_argument("--work-root", type=Path, default=run.WORK_ROOT, help="where sealed workdirs go")
    p.add_argument("--llama-swap-log", type=Path, default=run.LLAMA_SWAP_LOG, help="llama-swap log")
    return p


def summary(path: Path, rec: dict) -> str:
    """A few lines on a finished run."""
    lc = rec["lifecycle"]
    tele = rec["telemetry"]
    lines = [
        f"record: {path}",
        f"arm: {rec['arm']['entry']} / {rec['arm']['harness']}, level {rec['level']}, run {rec['run_index']}",
        f"end: {rec['end_reason']} ({lc['end_detail']})",
        f"valid: {rec['valid']}" + (f" ({'; '.join(rec['validity']['reasons'])})" if not rec["valid"] else ""),
        f"score: {rec['score']:.3f}",
        f"wall time: {lc.get('duration')} s; passes: {rec['passes']}; placement: {lc['placement']}",
        "telemetry: " + ("available" if tele.get("available") else f"unavailable ({tele.get('reason')})"),
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        config = arms.load_config(args.config)
        index = args.run_index
        if index is None:
            index = run.next_run_index(config, entry=args.entry, harness=args.harness, level=args.level,
                                       runs_dir=args.runs_dir, work_root=args.work_root)
        arms.validate_run(config, entry=args.entry, harness=args.harness, level=args.level, run_index=index)
    except (arms.ArmError, OSError) as exc:
        print(f"mb.py run: {exc}", file=sys.stderr)
        return 2
    try:
        path, rec = run.run_one(
            entry=args.entry, harness=args.harness, level=args.level, run_index=index, config=config,
            autonomy=args.autonomy, pass_cap=args.pass_cap, wall_clock=args.wall_clock,
            stall=args.stall_limit, rig=run.Rig(args.base_url, args.state), runs_dir=args.runs_dir,
            work_root=args.work_root, llama_swap_log=args.llama_swap_log, headless=args.headless,
        )
    except arms.ArmError as exc:
        print(f"mb.py run: {exc}", file=sys.stderr)
        return 2
    except (run.RunError, preflight.PreflightError, probe.ProbeError, lifecycle.LifecycleError,
            launch_pi.LaunchError, wd.WorkdirError) as exc:
        print(f"mb.py run: {exc}", file=sys.stderr)
        return 1
    print(summary(path, rec))
    return 0
