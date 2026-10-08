"""The normalised session-log format: the validator and the load/dump helpers."""

import copy

import pytest

from modelbench import normlog


def _log() -> dict:
    return {
        "format": normlog.FORMAT_VERSION,
        "harness": "pi",
        "requests": [
            {
                "id": "r1",
                "start": 1000.0,
                "end": 1004.5,
                "input_tokens": 1200,
                "cached_tokens": 800,
                "output_tokens": 150,
                "request_class": "main",
            },
            {
                "id": "r2",
                "start": 1005.0,
                "end": 1006.0,
                "input_tokens": 300,
                "cached_tokens": 0,
                "output_tokens": 20,
                "request_class": "background",
            },
        ],
        "tool_calls": [
            {
                "id": "t1",
                "request_id": "r1",
                "time": 1004.6,
                "name": "bash",
                "arguments": {"command": "uv run pytest"},
                "is_error": True,
                "result_size": 512,
            },
            {
                "id": "t2",
                "request_id": "r1",
                "time": 1004.7,
                "name": "read",
                "arguments": {"path": "a.py"},
                "is_error": False,
                "result_size": 40,
            },
        ],
    }


def test_valid_log_has_no_errors():
    assert normlog.validate(_log()) == []
    normlog.check(_log())


def test_tool_call_missing_error_flag_is_rejected():
    log = _log()
    del log["tool_calls"][1]["is_error"]
    errors = normlog.validate(log)
    assert errors == ["tool_calls[1].is_error: missing"]
    with pytest.raises(normlog.ValidationError, match=r"tool_calls\[1\]\.is_error"):
        normlog.check(log)


@pytest.mark.parametrize("field", ["input_tokens", "cached_tokens", "output_tokens"])
def test_request_missing_token_count_is_rejected(field):
    log = _log()
    del log["requests"][0][field]
    assert normlog.validate(log) == [f"requests[0].{field}: missing"]


def test_wrong_types_and_values_are_named():
    log = _log()
    log["requests"][1]["request_class"] = "chat"
    log["requests"][1]["output_tokens"] = -1
    log["requests"][0]["end"] = 999.0
    log["tool_calls"][0]["is_error"] = "no"
    log["tool_calls"][0]["result_size"] = True
    errors = normlog.validate(log)
    assert "requests[1].request_class: must be one of " + ", ".join(
        normlog.REQUEST_CLASSES
    ) in errors
    assert "requests[1].output_tokens: must be a non-negative integer" in errors
    assert "requests[0].end: must not be before start" in errors
    assert "tool_calls[0].is_error: must be a boolean" in errors
    assert "tool_calls[0].result_size: must be a non-negative integer" in errors


def test_unknown_request_reference_and_top_level_shape():
    log = _log()
    log["tool_calls"][0]["request_id"] = "nope"
    assert normlog.validate(log) == [
        "tool_calls[0].request_id: no request with id 'nope'"
    ]
    assert normlog.validate([]) == ["log: must be an object"]
    bad = _log()
    del bad["requests"]
    bad["format"] = 99
    assert normlog.validate(bad) == [
        "format: must be " + str(normlog.FORMAT_VERSION),
        "requests: missing",
    ]


def test_dump_and_load_round_trip(tmp_path):
    path = tmp_path / "session.norm.json"
    normlog.dump(_log(), path)
    assert normlog.load(path) == _log()


def test_dump_refuses_an_invalid_log(tmp_path):
    log = copy.deepcopy(_log())
    del log["requests"][0]["input_tokens"]
    with pytest.raises(normlog.ValidationError):
        normlog.dump(log, tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()


def test_load_rejects_an_invalid_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"format": 1, "harness": "pi", "requests": [{}], "tool_calls": []}')
    with pytest.raises(normlog.ValidationError, match=r"requests\[0\]\.id: missing"):
        normlog.load(path)


def test_events_orders_requests_and_tool_calls_by_time():
    order = [(kind, item["id"]) for kind, item in normlog.events(_log())]
    assert order == [
        ("request", "r1"),
        ("tool_call", "t1"),
        ("tool_call", "t2"),
        ("request", "r2"),
    ]
