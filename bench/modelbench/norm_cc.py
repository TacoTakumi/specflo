"""Normalise a Claude Code session transcript into the normlog format.

normalise(session_path) reads <session>.jsonl and every subagent transcript
under <session>/subagents/agent-*.jsonl, and returns a normlog dict.

Rules:
- Only "user" and "assistant" lines count; summary, system, file-history and
  other line types are skipped, and so are the harness's own "<synthetic>"
  assistant lines (error or interrupt notices, no API call behind them).
- Claude Code writes one API response as several assistant lines (one per
  content block) that share requestId and repeat the usage. Lines are grouped
  by requestId (message.id, then uuid, when it is absent) and the usage is
  counted once per group, taking the largest value of each field.
- A request's start and end are the earliest and latest timestamp of its
  lines. The lines are written as content blocks finish, so start is the
  time of the first finished block, not when the request was sent; the
  prompt send time is closer to the timestamp of the parent user line.
- Tokens: input_tokens = usage.input_tokens + cache_creation_input_tokens
  (both are prompt tokens the server had to process; the cache write is a
  side effect), cached_tokens = cache_read_input_tokens. The cache-creation
  count is also kept on its own as the extra key cache_creation_tokens.
- Each request also carries the extra key model (message.model).
- request_class is "subagent" for subagent transcripts and sidechain lines,
  else "main".
- A tool call's time is its tool_use line's timestamp. Its error flag and
  result size come from the matching tool_result; result_size is the
  character count of a string result or of the text blocks of a list result.
  A call with no result has is_error false and result_size 0.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from modelbench import normlog

_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


def parse_time(stamp: str) -> float:
    """ISO 8601 timestamp (with Z or an offset) to epoch seconds."""
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def _read(path: Path) -> list[dict]:
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            item = json.loads(raw)
            if isinstance(item, dict):
                lines.append(item)
    return lines


def _result_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(b.get("text", ""))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return 0


def session_files(session_path: str | Path) -> list[tuple[Path, str]]:
    """The session file and its subagent transcripts, each with its request class."""
    path = Path(session_path)
    subdir = path.with_suffix("") / "subagents"
    files = [(path, "main")]
    files += [(p, "subagent") for p in sorted(subdir.glob("agent-*.jsonl"))]
    return files


def normalise(session_path: str | Path) -> dict:
    """Return the normlog dict for one Claude Code session and its subagents."""
    groups: dict[str, dict] = {}
    calls: dict[str, dict] = {}
    results: dict[str, dict] = {}
    for path, file_class in session_files(session_path):
        for line in _read(path):
            kind, msg = line.get("type"), line.get("message")
            if kind not in ("user", "assistant") or not isinstance(msg, dict):
                continue
            content = msg.get("content")
            blocks = content if isinstance(content, list) else []
            if kind == "user":
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        results[b.get("tool_use_id")] = b
                continue
            if msg.get("model") == "<synthetic>":
                continue
            key = line.get("requestId") or msg.get("id") or line.get("uuid")
            when = parse_time(line["timestamp"])
            req_class = "subagent" if file_class == "subagent" or line.get("isSidechain") else "main"
            g = groups.setdefault(
                key,
                {"times": [], "usage": dict.fromkeys(_USAGE_KEYS, 0), "class": req_class,
                 "model": msg.get("model")},
            )
            g["times"].append(when)
            usage = msg.get("usage") or {}
            for k in _USAGE_KEYS:
                g["usage"][k] = max(g["usage"][k], int(usage.get(k) or 0))
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    calls[b["id"]] = {
                        "id": b["id"],
                        "request_id": key,
                        "time": when,
                        "name": b.get("name", ""),
                        "arguments": b.get("input") or {},
                    }
    requests = []
    for key, g in groups.items():
        u = g["usage"]
        req = {
            "id": key,
            "start": min(g["times"]),
            "end": max(g["times"]),
            "input_tokens": u["input_tokens"] + u["cache_creation_input_tokens"],
            "cached_tokens": u["cache_read_input_tokens"],
            "output_tokens": u["output_tokens"],
            "request_class": g["class"],
            "cache_creation_tokens": u["cache_creation_input_tokens"],
        }
        if g["model"]:
            req["model"] = g["model"]
        requests.append(req)
    tool_calls = []
    for call in calls.values():
        res = results.get(call["id"], {})
        call["is_error"] = bool(res.get("is_error", False))
        call["result_size"] = _result_size(res.get("content"))
        tool_calls.append(call)
    log = {
        "format": normlog.FORMAT_VERSION,
        "harness": "claude-code",
        "requests": sorted(requests, key=lambda r: r["start"]),
        "tool_calls": sorted(tool_calls, key=lambda c: c["time"]),
    }
    normlog.check(log)
    return log
