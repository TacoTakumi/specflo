"""The Claude Code session normaliser: requestId deduplication, subagents, tool calls."""

import json
from pathlib import Path

from modelbench import norm_cc, normlog

SAMPLES = Path(__file__).resolve().parent / "samples"
SESSION = SAMPLES / "cc-session.jsonl"
SUBAGENT = SAMPLES / "cc-session" / "subagents" / "agent-a1b2c3.jsonl"


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _assistant(path: Path) -> list[dict]:
    return [d for d in _lines(path) if d.get("type") == "assistant" and "requestId" in d]


def _dedup_usage(path: Path) -> dict[str, dict]:
    """First usage seen per requestId (the split lines repeat the same usage)."""
    out: dict[str, dict] = {}
    for d in _assistant(path):
        out.setdefault(d["requestId"], d["message"]["usage"])
    return out


def test_output_passes_validation():
    log = norm_cc.normalise(SESSION)
    normlog.check(log)
    assert log["harness"] == "claude-code"


def test_request_count_is_distinct_request_ids():
    log = norm_cc.normalise(SESSION)
    ids = {d["requestId"] for p in (SESSION, SUBAGENT) for d in _assistant(p)}
    assert len(log["requests"]) == len(ids)
    assert {r["id"] for r in log["requests"]} == ids
    # The sample does split one response over several lines.
    assert len(_assistant(SESSION)) > len({d["requestId"] for d in _assistant(SESSION)})


def test_token_sums_are_deduplicated_totals():
    log = norm_cc.normalise(SESSION)
    usage = {**_dedup_usage(SESSION), **_dedup_usage(SUBAGENT)}

    def total(key: str) -> int:
        return sum(u[key] for u in usage.values())

    reqs = log["requests"]
    assert sum(r["output_tokens"] for r in reqs) == total("output_tokens")
    assert sum(r["cached_tokens"] for r in reqs) == total("cache_read_input_tokens")
    assert sum(r["input_tokens"] for r in reqs) == total("input_tokens") + total(
        "cache_creation_input_tokens"
    )
    assert sum(r["cache_creation_tokens"] for r in reqs) == total("cache_creation_input_tokens")
    naive = sum(d["message"]["usage"]["output_tokens"] for d in _assistant(SESSION))
    assert naive > sum(r["output_tokens"] for r in reqs if r["request_class"] == "main")


def test_subagent_requests_are_tagged():
    log = norm_cc.normalise(SESSION)
    classes = {r["id"]: r["request_class"] for r in log["requests"]}
    sub_ids = {d["requestId"] for d in _assistant(SUBAGENT)}
    assert sub_ids and all(classes[i] == "subagent" for i in sub_ids)
    assert all(c == "main" for i, c in classes.items() if i not in sub_ids)


def test_request_times_span_its_lines_and_model_is_kept():
    log = norm_cc.normalise(SESSION)
    req = next(r for r in log["requests"] if r["id"] == "req_A")
    times = [norm_cc.parse_time(d["timestamp"]) for d in _assistant(SESSION) if d["requestId"] == "req_A"]
    assert (req["start"], req["end"]) == (min(times), max(times))
    assert req["end"] > req["start"]
    assert {r["model"] for r in log["requests"]} == {"bench-model-x"}


def test_tool_calls_carry_name_arguments_error_and_size():
    log = norm_cc.normalise(SESSION)
    calls = {c["id"]: c for c in log["tool_calls"]}
    assert set(calls) == {"toolu_01", "toolu_02", "toolu_03", "toolu_04", "toolu_S1"}
    bash = calls["toolu_02"]
    assert bash["name"] == "Bash"
    assert bash["arguments"] == {"command": "uv run pytest", "description": "Run tests"}
    assert bash["is_error"] is True
    assert bash["result_size"] == len("Exit code 1\nplaceholder failure")
    assert bash["request_id"] == "req_B"
    assert calls["toolu_01"]["is_error"] is False
    # A list-of-blocks result counts the text of its blocks.
    assert calls["toolu_03"]["result_size"] == len("placeholder edit ok")
    assert calls["toolu_S1"]["request_id"] == "req_S1"


def test_skipped_lines_leave_no_trace(tmp_path):
    lines = _lines(SESSION)
    kept = [d for d in lines if d.get("type") in ("user", "assistant")]
    kept = [d for d in kept if d.get("message", {}).get("model") != "<synthetic>"]
    (tmp_path / "s.jsonl").write_text("".join(json.dumps(d) + "\n" for d in kept))
    full, trimmed = norm_cc.normalise(SESSION), norm_cc.normalise(tmp_path / "s.jsonl")
    main = [r for r in full["requests"] if r["request_class"] == "main"]
    assert main == trimmed["requests"]


def test_dump_round_trip(tmp_path):
    log = norm_cc.normalise(SESSION)
    normlog.dump(log, tmp_path / "out.json")
    assert normlog.load(tmp_path / "out.json") == log
