"""Behaviour metrics: each value from a hand-built log equals a hand-computed one."""

import json

from modelbench import metrics, normlog


def _req(rid: str, start: float, end: float, out: int, cls: str) -> dict:
    return {
        "id": rid,
        "start": start,
        "end": end,
        "input_tokens": 100,
        "cached_tokens": 0,
        "output_tokens": out,
        "request_class": cls,
    }


def _call(cid: str, rid: str | None, time: float, name: str, err: bool) -> dict:
    return {
        "id": cid,
        "request_id": rid,
        "time": time,
        "name": name,
        "arguments": {},
        "is_error": err,
        "result_size": 10,
    }


def _log() -> dict:
    # Listed out of time order on purpose: the first event is r2 at 1000.0.
    log = {
        "format": normlog.FORMAT_VERSION,
        "harness": "claude-code",
        "requests": [
            _req("r1", 1010.0, 1020.0, 300, "main"),
            _req("r2", 1000.0, 1005.0, 100, "main"),
            _req("r3", 1030.0, 1040.0, 50, "subagent"),
            _req("r4", 1021.0, 1022.0, 999, "background"),  # not a turn
            _req("r5", 1050.0, 1060.0, 800, "compaction"),  # not a turn
            _req("r6", 1041.0, 1045.0, 1000, "main"),
        ],
        "tool_calls": [
            _call("t1", "r2", 1005.5, "bash", True),
            _call("t2", "r1", 1020.5, "read", False),
            _call("t3", "r1", 1020.6, "bash", False),
            _call("t4", "r3", 1040.5, "bash", True),
            _call("t5", None, 1070.0, "edit", True),  # the last event
        ],
    }
    normlog.check(log)
    return log


def test_metrics_equal_hand_computed_values():
    m = metrics.compute(_log())
    # Turns are main + subagent requests: r1, r2, r3, r6; outputs 300, 100, 50, 1000.
    assert m["turns"] == 4
    assert m["output_tokens_per_turn"] == {
        "mean": (300 + 100 + 50 + 1000) / 4,  # 362.5
        "median": (100 + 300) / 2,  # 200.0
        "max": 1000,
    }
    assert m["tool_calls"] == {"bash": 3, "edit": 1, "read": 1}
    assert m["tool_errors"] == 3
    # First event r2 starts at 1000.0; last is tool call t5 at 1070.0.
    assert m["wall_time_s"] == 70.0
    json.dumps(m)


def test_wall_time_ends_at_the_latest_request_end():
    log = _log()
    log["tool_calls"] = []
    # r5 (compaction) ends last at 1060.0; it is not a turn but still counts for time.
    assert metrics.compute(log)["wall_time_s"] == 60.0


def test_empty_log_has_zero_turns_and_no_token_stats():
    log = {"format": 1, "harness": "pi", "requests": [], "tool_calls": []}
    m = metrics.compute(log)
    assert m["turns"] == 0
    assert m["output_tokens_per_turn"] == {"mean": None, "median": None, "max": None}
    assert m["tool_calls"] == {}
    assert m["tool_errors"] == 0
    assert m["wall_time_s"] == 0.0
    json.dumps(m)
