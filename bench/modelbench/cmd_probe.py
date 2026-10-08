"""`mb.py probe [--harness H]... [--entry E]...`: the reasoning setting each engine gets through each harness.

For each entry (loaded once, after preflight) and each harness, the harness
runs one short prompt through a capturing proxy; the probe prints what the
request carried, what the engine renders with (reasoning on or off, effort)
and what the reply shows. Then, per harness, it compares the two engines.

Exits 0 when every harness gets the same setting on both engines, 1 when one
differs (the batch refuses that comparison), 2 when a probe could not run.
The captured requests and each probe's JSON are kept under --out.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import tempfile
from pathlib import Path

from modelbench import arms, preflight, probe


def _on(value: bool | None) -> str:
    return "unknown" if value is None else "on" if value else "off"


def describe(result: probe.ProbeResult) -> str:
    """Two lines for one probe: what was sent, what the engine got and what the reply shows."""
    got = result.received
    effort = f", effort {got.effort}" if got.thinking else ""
    ignored = f"; ignores {', '.join(got.ignored)}" if got.ignored else ""
    budget = f", thinking budget {got.budget}" if got.budget is not None else ""
    chars = "" if result.reasoning_chars is None else f" ({result.reasoning_chars} chars)"
    return (
        f"{result.harness} / {result.entry} ({result.engine}, {result.api}): "
        f"sent {json.dumps(result.sent, sort_keys=True)}\n"
        f"  engine renders: reasoning {_on(got.thinking)}{effort}{budget} [{got.source}{ignored}]; "
        f"reply: reasoning {_on(result.observed)}{chars}; {result.requests} model request(s)"
    )


def verdicts(results: list[probe.ProbeResult]) -> list[tuple[str, str | None]]:
    """(harness, refusal or None) for every pair of probes of one harness on different engines."""
    out = []
    by_harness: dict[str, list[probe.ProbeResult]] = {}
    for r in results:
        by_harness.setdefault(r.harness, []).append(r)
    for harness, group in by_harness.items():
        for a, b in itertools.combinations(group, 2):
            if a.engine != b.engine:
                out.append((harness, probe.refusal(a, b)))
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mb.py probe", description=__doc__.splitlines()[0])
    parser.add_argument("--harness", action="append", choices=arms.HARNESSES,
                        help="harness to probe (repeat; default: every harness of the arm config)")
    parser.add_argument("--entry", action="append",
                        help="llama-swap entry to probe (repeat; default: every entry of the arm config)")
    parser.add_argument("--config", default=str(arms.DEFAULT_CONFIG), help="arm config")
    parser.add_argument("--base-url", default=preflight.DEFAULT_BASE_URL, help="llama-swap")
    parser.add_argument("--state", default=str(preflight.DEFAULT_STATE), help="bench-load state file")
    parser.add_argument("--out", type=Path, default=None,
                        help="dir for captured requests and probe JSON (default: a new temp dir)")
    parser.add_argument("--prompt", default=probe.PROMPT)
    parser.add_argument("--timeout", type=float, default=probe.DEFAULT_TIMEOUT,
                        help="seconds per model load and per harness run")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args(argv)

    try:
        config = arms.load_config(args.config)
    except (arms.ArmError, OSError) as exc:
        print(f"mb.py probe: {exc}", file=sys.stderr)
        return 2
    harnesses = args.harness or list(config.harnesses)
    entries = args.entry or list(config.entries)
    for entry in entries:
        if entry not in config.entries:
            print(f"mb.py probe: entry {entry!r} is not an entry of the arm config", file=sys.stderr)
            return 2
    out = args.out or Path(tempfile.mkdtemp(prefix="mb-probe-"))
    out.mkdir(parents=True, exist_ok=True)

    results: list[probe.ProbeResult] = []
    # One entry at a time: both use the whole rig, so each load unloads the other.
    for entry in entries:
        run = arms.Run(entry=entry, harness=harnesses[0], level=next(iter(config.levels)), run_index=0)
        try:
            running = preflight.preflight(run, base_url=args.base_url, state_path=args.state)
            if entry not in running:
                preflight.record_bench_load(args.state, entry)
            probe.load_entry(args.base_url, entry, timeout=args.timeout)
            for harness in harnesses:
                result = probe.probe(
                    harness, entry, base_url=args.base_url, run_dir=out / f"{harness}--{entry}",
                    prompt=args.prompt, timeout=args.timeout, config=config,
                )
                results.append(result)
                if not args.json:
                    print(describe(result), flush=True)
        except (preflight.PreflightError, probe.ProbeError) as exc:
            print(f"mb.py probe: {exc}", file=sys.stderr)
            return 2

    found = verdicts(results)
    if args.json:
        print(json.dumps({
            "probes": [r.to_dict() for r in results],
            "comparisons": [{"harness": h, "refusal": msg} for h, msg in found],
            "out": str(out),
        }, indent=2))
    else:
        for harness, message in found:
            if message:
                print(f"{harness}: DIFFERENT - {message}")
            else:
                same = next(r for r in results if r.harness == harness).received
                effort = f", effort {same.effort}" if same.thinking else ""
                print(f"{harness}: same on both engines (reasoning {_on(same.thinking)}{effort})")
        print(f"captured requests: {out}")
    return 1 if any(message for _, message in found) else 0
