"""Normalise a pi session JSONL file (session format v3) into the normlog format.

Requests (one per model call pi records usage for, in file order, all branches):
    assistant message           main; start = message.timestamp (ms), end = entry timestamp
    toolResult message w/ usage subagent; nested model work a tool did; start = the call's time
    usage entry (e.g. cache_warm) background
    compaction / branch_summary compaction, only when the entry carries usage; start = end
                                = entry timestamp

Tokens: input_tokens = usage.input + usage.cacheWrite (prompt tokens processed
without a cache hit), cached_tokens = usage.cacheRead, output_tokens =
usage.output (reasoning is already inside it). cacheWrite is also kept as the
extra key cache_write_tokens, so input + cached + output equals totalTokens.
Extra key model: the model id the entry names (the physical model that answered),
omitted when the entry names none.

Tool calls come from assistant toolCall blocks and are matched to toolResult
messages by toolCallId: is_error = isError, result_size = characters of the
result's text blocks. A call with no result gets is_error true, result_size 0
and missing_result true. Every other entry type and message role is skipped:
it is not a model call.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from modelbench import normlog

_SIDE_ENTRY_CLASS = {"usage": "background", "compaction": "compaction", "branch_summary": "compaction"}


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _request(rid: str, start: float, end: float, usage: dict, cls: str, model: Any) -> dict:
    cache_write = int(usage.get("cacheWrite") or 0)
    req = {
        "id": rid,
        "start": start,
        "end": max(start, end),
        "input_tokens": int(usage.get("input") or 0) + cache_write,
        "cached_tokens": int(usage.get("cacheRead") or 0),
        "output_tokens": int(usage.get("output") or 0),
        "request_class": cls,
        "cache_write_tokens": cache_write,
    }
    if isinstance(model, str) and model:
        req["model"] = model
    return req


def _text_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    return sum(len(b.get("text", "")) for b in content or [] if b.get("type") == "text")


def normalise(entries: Iterable[dict]) -> dict:
    """Build a normalised log from parsed pi session entries."""
    requests: list[dict] = []
    calls: list[dict] = []
    open_calls: dict[str, dict] = {}  # pi toolCallId -> call still waiting for its result
    call_ids: set[str] = set()
    for n, entry in enumerate(entries):
        etype = entry.get("type")
        if etype == "session":
            continue
        eid = entry.get("id") or f"line{n}"
        end = _epoch(entry["timestamp"]) if entry.get("timestamp") else 0.0
        if etype in _SIDE_ENTRY_CLASS:
            if isinstance(entry.get("usage"), dict):
                requests.append(_request(eid, end, end, entry["usage"], _SIDE_ENTRY_CLASS[etype], entry.get("model")))
            continue
        if etype != "message":
            continue
        msg = entry.get("message") or {}
        role = msg.get("role")
        if role == "assistant":
            start = msg["timestamp"] / 1000 if "timestamp" in msg else end
            requests.append(_request(eid, start, end, msg.get("usage") or {}, "main", msg.get("model")))
            for block in msg.get("content") or []:
                if block.get("type") != "toolCall":
                    continue
                cid = block["id"]
                unique = cid if cid not in call_ids else f"{cid}#{n}"
                call_ids.add(unique)
                call = {
                    "id": unique, "request_id": eid, "time": max(start, end), "name": block["name"],
                    "arguments": block.get("arguments") or {}, "is_error": True, "result_size": 0,
                    "missing_result": True,
                }
                calls.append(call)
                open_calls[cid] = call
        elif role == "toolResult":
            call = open_calls.pop(msg.get("toolCallId"), None)
            if call is not None:
                call["is_error"] = bool(msg.get("isError"))
                call["result_size"] = _text_size(msg.get("content"))
                del call["missing_result"]
            if isinstance(msg.get("usage"), dict):
                start = call["time"] if call else end
                requests.append(_request(eid, start, end, msg["usage"], "subagent", None))
    log = {"format": normlog.FORMAT_VERSION, "harness": "pi", "requests": requests, "tool_calls": calls}
    normlog.check(log)
    return log


def normalise_file(path: str | Path) -> dict:
    """Read a pi session JSONL file and normalise it."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return normalise(json.loads(line) for line in lines if line.strip())
