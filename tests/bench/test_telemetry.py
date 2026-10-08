"""Server telemetry read from the llama-swap log inside a run's wall-clock window."""

from pathlib import Path

import pytest

from modelbench import telemetry

SAMPLES = Path(__file__).parent / "samples"
START = "2026-10-07T21:00:00-03:00"
END = "2026-10-07T21:10:00-03:00"


def test_window_totals_match_hand_counts():
    t = telemetry.extract(SAMPLES / "llama-swap-ts.log", START, END)
    assert t["available"] is True
    # Tasks 40 and 41 (llama-server) and one Strata reply; task 39 is before
    # the window, task 42 and the second Strata reply are after it.
    assert t["tasks"] == 3
    assert t["prefill"] == {
        "tokens": 1050,
        "seconds": pytest.approx(1.75),
        "tokens_per_second": pytest.approx(600.0),
    }
    # 120 + 64 tokens in 4.0 + 2.0 s, plus Strata's 200 tokens at 80 tok/s.
    assert t["decode"] == {
        "tokens": 384,
        "seconds": pytest.approx(8.5),
        "tokens_per_second": pytest.approx(384 / 8.5, abs=0.01),
    }
    # Prompt = release n_tokens - decoded + 1: task 40 is 900 (none cached),
    # task 41 is 1050 with 900 reused.
    assert t["cache"] == {
        "prompt_tokens": 1950,
        "reused_tokens": 900,
        "reuse_ratio": pytest.approx(900 / 1950, abs=1e-4),
    }
    assert t["draft"] == {
        "accepted": 80,
        "generated": 120,
        "acceptance": pytest.approx(80 / 120, abs=1e-4),
    }


def test_accepts_datetime_bounds():
    from datetime import datetime

    t = telemetry.extract(
        SAMPLES / "llama-swap-ts.log",
        datetime.fromisoformat(START),
        datetime.fromisoformat(END),
    )
    assert t["tasks"] == 3


def test_empty_window_is_available_with_zero_counts():
    t = telemetry.extract(
        SAMPLES / "llama-swap-ts.log",
        "2026-10-08T00:00:00-03:00",
        "2026-10-08T01:00:00-03:00",
    )
    assert t["available"] is True
    assert t["tasks"] == 0
    assert t["decode"]["tokens_per_second"] is None
    assert t["cache"]["reuse_ratio"] is None


def test_log_without_wall_clock_column_is_unavailable():
    t = telemetry.extract(SAMPLES / "llama-swap-plain.log", START, END)
    assert t["available"] is False
    assert "wall-clock" in t["reason"]


def test_missing_log_is_unavailable(tmp_path):
    t = telemetry.extract(tmp_path / "nope.log", START, END)
    assert t["available"] is False
    assert "not found" in t["reason"]
