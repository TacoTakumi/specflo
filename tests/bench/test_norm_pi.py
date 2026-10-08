"""The pi session normaliser, checked against a synthetic pi v3 session sample."""

import json
from pathlib import Path

import pytest

from modelbench import norm_pi, normlog

SAMPLE = Path(__file__).parent / "samples" / "pi-session.jsonl"


def _raw() -> list[dict]:
    return [json.loads(line) for line in SAMPLE.read_text(encoding="utf-8").splitlines() if line]


def _messages(role: str) -> list[dict]:
    return [e for e in _raw() if e["type"] == "message" and e["message"]["role"] == role]


def _all_usage() -> list[dict]:
    """Every usage object pi counts in the session totals."""
    out = []
    for e in _raw():
        if e["type"] == "message" and "usage" in e["message"]:
            out.append(e["message"]["usage"])
        elif e["type"] in ("usage", "compaction", "branch_summary") and "usage" in e:
            out.append(e["usage"])
    return out


@pytest.fixture(scope="module")
def log() -> dict:
    return norm_pi.normalise_file(SAMPLE)


def test_output_passes_normlog_validation(log, tmp_path):
    assert normlog.validate(log) == []
    assert log["harness"] == "pi"
    normlog.dump(log, tmp_path / "norm.json")
    assert normlog.load(tmp_path / "norm.json") == log


def test_main_request_count_equals_assistant_message_count(log):
    main = [r for r in log["requests"] if r["request_class"] == "main"]
    assert len(main) == len(_messages("assistant"))
    assert [r["id"] for r in main] == [e["id"] for e in _messages("assistant")]


def test_side_calls_with_usage_become_their_own_requests(log):
    side = [r for r in log["requests"] if r["request_class"] != "main"]
    assert len(log["requests"]) == len(_all_usage())
    assert sorted(r["request_class"] for r in side) == [
        "background", "compaction", "compaction", "subagent"
    ]


def test_token_sums_equal_the_sessions_own_usage_totals(log):
    usage = _all_usage()
    reqs = log["requests"]
    assert sum(r["input_tokens"] for r in reqs) == sum(u["input"] + u["cacheWrite"] for u in usage)
    assert sum(r["cached_tokens"] for r in reqs) == sum(u["cacheRead"] for u in usage)
    assert sum(r["output_tokens"] for r in reqs) == sum(u["output"] for u in usage)
    assert sum(r["cache_write_tokens"] for r in reqs) == sum(u["cacheWrite"] for u in usage)
    total = sum(r["input_tokens"] + r["cached_tokens"] + r["output_tokens"] for r in reqs)
    assert total == sum(u["totalTokens"] for u in usage)


def test_main_token_sums_equal_assistant_usage(log):
    usage = [e["message"]["usage"] for e in _messages("assistant")]
    main = [r for r in log["requests"] if r["request_class"] == "main"]
    assert sum(r["output_tokens"] for r in main) == sum(u["output"] for u in usage)
    assert sum(r["cached_tokens"] for r in main) == sum(u["cacheRead"] for u in usage)


def test_main_requests_carry_the_message_model_and_times(log):
    by_id = {r["id"]: r for r in log["requests"]}
    for e in _messages("assistant"):
        req = by_id[e["id"]]
        assert req["model"] == e["message"]["model"]
        assert req["start"] == e["message"]["timestamp"] / 1000
        assert req["end"] >= req["start"]


def test_tool_calls_carry_name_arguments_error_flag_and_result_size(log):
    results = {e["message"]["toolCallId"]: e["message"] for e in _messages("toolResult")}
    calls = {c["id"]: c for c in log["tool_calls"]}
    raw_calls = [
        (e["id"], block)
        for e in _messages("assistant")
        for block in e["message"]["content"]
        if block["type"] == "toolCall"
    ]
    assert len(calls) == len(raw_calls)
    for entry_id, block in raw_calls:
        call = calls[block["id"]]
        result = results[block["id"]]
        assert call["request_id"] == entry_id
        assert call["name"] == block["name"]
        assert call["arguments"] == block["arguments"]
        assert call["is_error"] is result["isError"]
        assert call["result_size"] == sum(len(b["text"]) for b in result["content"])
    assert any(c["is_error"] for c in log["tool_calls"])


def test_a_call_without_a_result_is_flagged(tmp_path):
    entries = [
        {"type": "session", "version": 3, "id": "s", "timestamp": "2026-09-01T10:00:00.000Z", "cwd": "/work/fixture"},
        {"type": "message", "id": "m1", "parentId": None, "timestamp": "2026-09-01T10:00:02.000Z",
         "message": {"role": "assistant", "model": "bench-model-a", "stopReason": "aborted", "timestamp": 1788256801000,
                     "usage": {"input": 5, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 6},
                     "content": [{"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "true"}}]}},
    ]
    log = norm_pi.normalise(entries)
    normlog.check(log)
    (call,) = log["tool_calls"]
    assert call["is_error"] is True and call["result_size"] == 0 and call["missing_result"] is True
