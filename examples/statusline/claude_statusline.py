#!/usr/bin/env python3
"""A Claude Code statusline with the specflo segment.

Claude Code runs this on every statusline redraw and passes the session as
JSON on stdin. It prints one line:

    <project> | <specflo> | <model> <effort> | <ctx>/<size> (<pct>%) | cache <pct>% | Q5h <pct>% <reset> | Q7d <pct>% <reset>

Every segment reads its data defensively: a segment whose data is absent is
left out. For example, rate_limits and current_usage are absent before the
first API response, and current_usage is null again right after /compact.

The specflo segment comes from specflo_segment.py in the same directory as
this file (links are followed). Without it, the line prints without the
segment.

Install: copy both files to ~/.claude/, make this one executable, and add to
~/.claude/settings.json:

    "statusLine": {"type": "command", "command": "~/.claude/claude_statusline.py"}
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
try:
    import specflo_segment
except Exception:
    specflo_segment = None

RESET = "\033[0m"
CYAN = "36"
GREEN = "32"
YELLOW = "33"
RED = "31"
DIM = "2"


def color(s, c):
    return "\033[{}m{}{}".format(c, s, RESET) if c else s


def get(d, path, default=None):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def fmt_tokens(n):
    n = int(n)
    if n >= 1_000_000:
        v = n / 1_000_000
        return ("{:.0f}M" if v == int(v) else "{:.1f}M").format(v)
    return "{}k".format(int(round(n / 1000)))


def quota_color(pct):
    return RED if pct >= 80 else (YELLOW if pct >= 50 else GREEN)


def fmt_reset(epoch):
    try:
        rem = int(float(epoch) - datetime.now(timezone.utc).timestamp())
    except Exception:
        return ""
    if rem <= 0:
        return ""
    d, h, m = rem // 86400, (rem % 86400) // 3600, (rem % 3600) // 60
    if d:
        return "{}d{}h".format(d, h)
    return "{}h{:02d}m".format(h, m) if h else "{}m".format(m)


def quota(label, window):
    if not isinstance(window, dict) or window.get("used_percentage") is None:
        return None
    p = int(round(window["used_percentage"]))
    seg = color("{} {}%".format(label, p), quota_color(p))
    reset = fmt_reset(window.get("resets_at"))
    if reset:
        seg += " " + color(reset, DIM)
    return seg


def statusline(d):
    segs = []

    # 1. Project directory (basename), which anchors the line.
    proj = get(d, "workspace.project_dir") or get(d, "workspace.current_dir") or get(d, "cwd")
    if proj:
        segs.append(color(os.path.basename(proj.rstrip("/")) or proj, CYAN))

    # 2. specflo: the active project and its phase or task.
    here = get(d, "workspace.current_dir") or get(d, "cwd") or get(d, "workspace.project_dir")
    if here and specflo_segment is not None:
        sf = specflo_segment.segment(here, color=True)
        if sf:
            segs.append(sf)

    # 3. Model name (without a trailing "(... context)") and effort.
    model = re.sub(r"\s*\([^)]*context\)\s*$", "", get(d, "model.display_name") or "").strip()
    if model:
        effort = get(d, "effort.level")
        segs.append(model + (" " + effort if effort else ""))

    cu = get(d, "context_window.current_usage")
    size = get(d, "context_window.context_window_size")

    # 4. Context window usage.
    if isinstance(cu, dict) and size:
        total = ((cu.get("input_tokens") or 0) + (cu.get("cache_creation_input_tokens") or 0)
                 + (cu.get("cache_read_input_tokens") or 0))
        if total > 0:
            pct = int(round(total * 100 / size))
            c = RED if pct >= 90 else (YELLOW if pct >= 70 else None)
            segs.append(color("{}/{} ({}%)".format(fmt_tokens(total), fmt_tokens(size), pct), c))

    # 5. Cache-read share of the input: cache hits over all input tokens.
    if isinstance(cu, dict):
        inp = cu.get("input_tokens") or 0
        cc = cu.get("cache_creation_input_tokens") or 0
        cr = cu.get("cache_read_input_tokens") or 0
        denom = inp + cc + cr
        if denom > 0:
            cp = int(round(cr * 100 / denom))
            c = GREEN if cp >= 70 else (YELLOW if cp >= 40 else None)
            segs.append(color("cache {}%".format(cp), c))

    # 6 and 7. The 5-hour and 7-day quota, each with the time to its reset.
    for label, key in (("Q5h", "rate_limits.five_hour"), ("Q7d", "rate_limits.seven_day")):
        seg = quota(label, get(d, key))
        if seg:
            segs.append(seg)

    return " | ".join(segs)


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(d, dict):
        return
    line = statusline(d)
    if line:
        print(line)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
