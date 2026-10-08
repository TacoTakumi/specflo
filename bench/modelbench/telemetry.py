"""Server telemetry from the llama-swap log inside a run's wall-clock window.

The log is the tee of llama-swap's whole stream: llama-swap's own lines,
llama-server output and Strata output. llama-server stamps lines only with
uptime (`0.15.506.990 I ...`), so attribution needs a wall-clock column at
the start of each line, added at the tee, as an ISO-8601 date-time followed
by one space:

    2026-10-07T21:00:09.621-03:00 0.15.506.990 I slot print_timing: ...

A stamp with no UTC offset is read as local time. When no line carries the
column, `extract` returns {"available": False, "reason": ...} and the run is
still scored.

What is counted, per line inside the window (both ends included):
- llama-server `print_timing` prompt eval and eval lines: prefill and decode
  tokens and milliseconds.
- llama-server `draft acceptance = ... (A accepted / G generated)`.
- Prompt-cache reuse per llama-server task, from its `stop processing:
  n_tokens = N` release line: prompt = N - decoded + 1, reused = prompt -
  prefilled. These lines print at the default log level.
- Strata `done: N tokens in S s (R tok/s)` lines: decode tokens, with decode
  time N / R (S also holds the prefill). Strata reports no prefill summary or
  cache reuse, so it adds to decode only.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

_STAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?) "
)
_PREFILL = re.compile(r"task (\d+) \| prompt eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
_DECODE = re.compile(r"task (\d+) \|\s+eval time =\s*([\d.]+) ms /\s*(\d+) tokens")
_DRAFT = re.compile(r"draft acceptance = [\d.]+ \(\s*(\d+) accepted /\s*(\d+) generated\)")
_RELEASE = re.compile(r"task (\d+) \| stop processing: n_tokens = (\d+)")
_STRATA_DONE = re.compile(r"^\[strata\] done: ([\d,]+) tokens in [\d.]+ s \(([\d.]+) tok/s\)")


def _as_time(value: str | datetime) -> datetime:
    t = datetime.fromisoformat(value) if isinstance(value, str) else value
    return t.astimezone()  # naive means local; aware stays comparable


def _rate(tokens: int, seconds: float) -> float | None:
    return round(tokens / seconds, 2) if seconds > 0 else None


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole > 0 else None


def unavailable(reason: str) -> dict[str, Any]:
    """The telemetry value a run records when it cannot read the log."""
    return {"available": False, "reason": reason}


def extract(log_path: str | Path, start: str | datetime, end: str | datetime) -> dict[str, Any]:
    """Sum server telemetry over the log lines stamped within [start, end]."""
    path = Path(log_path)
    if not path.is_file():
        return unavailable(f"llama-swap log not found: {path}")
    lo, hi = _as_time(start), _as_time(end)

    stamped = False
    tasks = 0
    pre_tok = dec_tok = 0
    pre_ms = dec_ms = 0.0
    prompt_tok = reused_tok = 0
    accepted = generated = 0
    pending: dict[str, dict[str, int]] = {}  # task id -> its timing tokens

    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _STAMP.match(line)
            if not m:
                continue
            stamped = True
            if not lo <= _as_time(m.group(1).replace(",", ".")) <= hi:
                continue
            body = line[m.end():]
            if m := _PREFILL.search(body):
                tasks += 1
                pre_ms += float(m.group(2))
                pre_tok += int(m.group(3))
                pending[m.group(1)] = {"prefill": int(m.group(3))}
            elif m := _DECODE.search(body):
                dec_ms += float(m.group(2))
                dec_tok += int(m.group(3))
                pending.setdefault(m.group(1), {})["decode"] = int(m.group(3))
            elif m := _DRAFT.search(body):
                accepted += int(m.group(1))
                generated += int(m.group(2))
            elif m := _RELEASE.search(body):
                seen = pending.pop(m.group(1), {})
                if "prefill" in seen and "decode" in seen:
                    prompt = max(int(m.group(2)) - seen["decode"] + 1, seen["prefill"])
                    prompt_tok += prompt
                    reused_tok += prompt - seen["prefill"]
            elif m := _STRATA_DONE.match(body):
                tasks += 1
                n, rate = int(m.group(1).replace(",", "")), float(m.group(2))
                dec_tok += n
                if rate > 0:
                    dec_ms += n / rate * 1000

    if not stamped:
        return unavailable("llama-swap log has no wall-clock column")
    pre_s, dec_s = round(pre_ms / 1000, 3), round(dec_ms / 1000, 3)
    return {
        "available": True,
        "window": {"start": lo.isoformat(), "end": hi.isoformat()},
        "tasks": tasks,
        "prefill": {"tokens": pre_tok, "seconds": pre_s, "tokens_per_second": _rate(pre_tok, pre_s)},
        "decode": {"tokens": dec_tok, "seconds": dec_s, "tokens_per_second": _rate(dec_tok, dec_s)},
        "cache": {
            "prompt_tokens": prompt_tok,
            "reused_tokens": reused_tok,
            "reuse_ratio": _ratio(reused_tok, prompt_tok),
        },
        "draft": {"accepted": accepted, "generated": generated, "acceptance": _ratio(accepted, generated)},
    }
