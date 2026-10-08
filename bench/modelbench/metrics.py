"""Behaviour metrics computed from one normalised session log.

`compute(log)` returns a JSON-serialisable dict for the run record's `metrics`:

    turns                    number of turns (see below)
    output_tokens_per_turn   {"mean", "median", "max"} of output tokens over the
                             turns; each is None when there are no turns
    tool_calls               {tool name: count}, sorted by name, over every call
    tool_errors              number of tool calls whose result has the error flag
    wall_time_s              seconds from the first event to the last (0.0 if empty)

A turn is one model request that does the run's work: a request of class
`main` or `subagent`. Requests of class `background` (titles, summaries,
permission classifier), `compaction` and `other` are harness overhead or
unplaceable, so they are not turns and their output tokens are not counted.
Tool calls and tool errors count every call, whatever request issued it.

Wall time uses every event of every class: it starts at the first event in
`normlog.events()` order and ends at the latest request end or tool-call time.
"""

from __future__ import annotations

import statistics
from typing import Any

from modelbench import normlog

TURN_CLASSES = ("main", "subagent")


def compute(log: dict) -> dict[str, Any]:
    """Return the behaviour metrics of a valid normalised log."""
    outputs = [
        r["output_tokens"] for r in log["requests"] if r["request_class"] in TURN_CLASSES
    ]
    tool_calls: dict[str, int] = {}
    for call in log["tool_calls"]:
        tool_calls[call["name"]] = tool_calls.get(call["name"], 0) + 1

    first = next(normlog.events(log), None)
    if first is None:
        wall = 0.0
    else:
        kind, item = first
        start = item["start"] if kind == "request" else item["time"]
        end = max(
            [r["end"] for r in log["requests"]] + [c["time"] for c in log["tool_calls"]]
        )
        wall = float(end - start)

    return {
        "turns": len(outputs),
        "output_tokens_per_turn": {
            "mean": statistics.fmean(outputs) if outputs else None,
            "median": statistics.median(outputs) if outputs else None,
            "max": max(outputs) if outputs else None,
        },
        "tool_calls": dict(sorted(tool_calls.items())),
        "tool_errors": sum(1 for c in log["tool_calls"] if c["is_error"]),
        "wall_time_s": wall,
    }
