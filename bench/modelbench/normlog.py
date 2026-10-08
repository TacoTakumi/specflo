"""The normalised session-log format: the contract between the normalisers and metrics.

The pi and Claude Code normalisers each write one of these; the metrics and
diagnostics read only this. A log is a plain JSON object:

    {
      "format": 1,
      "harness": "pi" | "claude-code",
      "requests": [Request, ...],
      "tool_calls": [ToolCall, ...]
    }

Request (one model request; Claude Code lines are deduplicated by requestId first):
    id             str, unique among requests
    start, end     float epoch seconds, end >= start
    input_tokens   int >= 0, uncached prompt tokens
    cached_tokens  int >= 0, prompt tokens served from the cache
    output_tokens  int >= 0
    request_class  one of REQUEST_CLASSES

ToolCall (one tool call the model made):
    id             str, unique among tool calls
    request_id     str naming the request that issued the call, or null
    time           float epoch seconds when the call was issued
    name           str, the tool name as the harness reports it
    arguments      object, the call's arguments as given
    is_error       bool, the harness's error flag on the result
    result_size    int >= 0, length of the result text in characters

Request classes (closed set):
    main        a turn of the agent driving the run
    subagent    a turn of a subagent the main agent started
    background  a harness side call (titles, summaries, permission classifier)
    compaction  a call that compacts or summarises the context
    other       anything the normaliser cannot place

Timestamps are what metrics order on; events() gives the merged order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

FORMAT_VERSION = 1
HARNESSES = ("pi", "claude-code")
REQUEST_CLASSES = ("main", "subagent", "background", "compaction", "other")


class ValidationError(ValueError):
    """A log breaks the format; .errors lists each problem."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _nonneg_int(v: Any) -> str | None:
    return None if _is_int(v) and v >= 0 else "must be a non-negative integer"


def _number(v: Any) -> str | None:
    return None if _is_num(v) else "must be a number"


def _string(v: Any) -> str | None:
    return None if isinstance(v, str) and v else "must be a non-empty string"


def _request_class(v: Any) -> str | None:
    return None if v in REQUEST_CLASSES else "must be one of " + ", ".join(REQUEST_CLASSES)


def _boolean(v: Any) -> str | None:
    return None if isinstance(v, bool) else "must be a boolean"


def _obj(v: Any) -> str | None:
    return None if isinstance(v, dict) else "must be an object"


def _opt_string(v: Any) -> str | None:
    return None if v is None else _string(v)


_REQUEST_FIELDS = {
    "id": _string,
    "start": _number,
    "end": _number,
    "input_tokens": _nonneg_int,
    "cached_tokens": _nonneg_int,
    "output_tokens": _nonneg_int,
    "request_class": _request_class,
}

_TOOL_CALL_FIELDS = {
    "id": _string,
    "request_id": _opt_string,
    "time": _number,
    "name": _string,
    "arguments": _obj,
    "is_error": _boolean,
    "result_size": _nonneg_int,
}


def _check_items(
    log: dict, key: str, fields: dict, errors: list[str]
) -> list[tuple[int, dict]]:
    items = log.get(key)
    if key not in log:
        errors.append(f"{key}: missing")
        return []
    if not isinstance(items, list):
        errors.append(f"{key}: must be a list")
        return []
    seen: set = set()
    good = []
    for i, item in enumerate(items):
        where = f"{key}[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        ok = True
        for name, check in fields.items():
            if name not in item:
                errors.append(f"{where}.{name}: missing")
                ok = False
                continue
            problem = check(item[name])
            if problem:
                errors.append(f"{where}.{name}: {problem}")
                ok = False
        if isinstance(item.get("id"), str):
            if item["id"] in seen:
                errors.append(f"{where}.id: duplicate id {item['id']!r}")
            seen.add(item["id"])
        if ok:
            good.append((i, item))
    return good


def validate(log: Any) -> list[str]:
    """Return every problem as 'path: reason' (e.g. 'tool_calls[1].is_error: missing')."""
    if not isinstance(log, dict):
        return ["log: must be an object"]
    errors: list[str] = []
    if log.get("format") != FORMAT_VERSION:
        errors.append(f"format: must be {FORMAT_VERSION}")
    if log.get("harness") not in HARNESSES:
        errors.append("harness: must be one of " + ", ".join(HARNESSES))
    requests = _check_items(log, "requests", _REQUEST_FIELDS, errors)
    for i, req in requests:
        if req["end"] < req["start"]:
            errors.append(f"requests[{i}].end: must not be before start")
    raw_requests = log.get("requests")
    linkable = isinstance(raw_requests, list)  # no reference noise if requests is broken
    request_ids = {
        r.get("id") for r in raw_requests if isinstance(r, dict)
    } if linkable else set()
    for i, call in _check_items(log, "tool_calls", _TOOL_CALL_FIELDS, errors):
        rid = call["request_id"]
        if linkable and rid is not None and rid not in request_ids:
            errors.append(f"tool_calls[{i}].request_id: no request with id {rid!r}")
    return errors


def check(log: Any) -> None:
    """Raise ValidationError if the log breaks the format."""
    errors = validate(log)
    if errors:
        raise ValidationError(errors)


def load(path: str | Path) -> dict:
    """Read and validate a normalised log from a JSON file."""
    log = json.loads(Path(path).read_text(encoding="utf-8"))
    check(log)
    return log


def dump(log: dict, path: str | Path) -> None:
    """Validate a normalised log and write it as JSON; nothing is written if invalid."""
    check(log)
    Path(path).write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")


def events(log: dict) -> Iterator[tuple[str, dict]]:
    """Yield ('request', r) and ('tool_call', c) in time order (request start, call time)."""
    merged = [(r["start"], 0, "request", r) for r in log["requests"]]
    merged += [(c["time"], 1, "tool_call", c) for c in log["tool_calls"]]
    for _, _, kind, item in sorted(merged, key=lambda e: (e[0], e[1])):
        yield kind, item
