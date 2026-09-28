"""The shared wait helper: it returns as soon as its condition holds, and its
limits scale by SPECFLO_TEST_WAIT_SCALE."""

import inspect
import time

import pytest

import waits
from waits import SCALE_ENV, scaled, settle, wait_until


@pytest.fixture(autouse=True)
def no_scale(monkeypatch):
    """Each test starts at the default scale, whatever the run was given."""
    monkeypatch.delenv(SCALE_ENV, raising=False)


class FakeTime:
    """A clock for ``waits`` that moves only when it sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(waits, "time", fake)
    return fake


def test_a_condition_that_holds_at_once_returns_at_once():
    start = time.monotonic()
    assert wait_until(lambda: True)
    assert time.monotonic() - start < 0.1


def test_the_first_check_comes_before_any_sleep(clock):
    wait_until(lambda: True)
    assert clock.sleeps == 0


def test_the_conditions_value_is_returned():
    assert wait_until(lambda: {"state": "idle"}) == {"state": "idle"}
    calls = []

    def third_time():
        calls.append(1)
        return len(calls) >= 3 and "ready"

    assert wait_until(third_time, interval=0.01) == "ready"
    assert len(calls) == 3


def test_with_scale_2_a_limit_of_0_2_s_fails_between_0_4_and_1_s(monkeypatch):
    monkeypatch.setenv(SCALE_ENV, "2")
    start = time.monotonic()
    with pytest.raises(AssertionError):
        wait_until(lambda: False, timeout=0.2)
    assert 0.4 <= time.monotonic() - start < 1.0


def test_the_default_limit_is_30_s(clock):
    assert inspect.signature(wait_until).parameters["timeout"].default == 30.0
    with pytest.raises(AssertionError):
        wait_until(lambda: False)
    assert clock.now == pytest.approx(30.0)


def test_the_last_check_comes_at_the_limit(clock):
    assert wait_until(lambda: clock.now >= 1.0 and "late", timeout=1.0) == "late"


def test_a_failure_names_the_message_and_the_limit(monkeypatch, clock):
    monkeypatch.setenv(SCALE_ENV, "3")
    with pytest.raises(AssertionError, match="the lease never ended") as failure:
        wait_until(lambda: False, timeout=2, message="the lease never ended")
    assert "6 s" in str(failure.value)
    assert SCALE_ENV in str(failure.value)


def test_a_failure_without_a_message_names_the_condition(clock):
    def lease_ended():
        return False

    with pytest.raises(AssertionError, match="lease_ended"):
        wait_until(lease_ended, timeout=1)


def test_settle_scales_the_same_way(monkeypatch):
    monkeypatch.setenv(SCALE_ENV, "2")
    start = time.monotonic()
    settle(0.1)
    assert 0.2 <= time.monotonic() - start < 0.5


def test_scaled_limits_follow_the_setting_at_call_time(monkeypatch):
    assert scaled(30) == 30
    monkeypatch.setenv(SCALE_ENV, "2.5")
    assert scaled(4) == 10
    monkeypatch.setenv(SCALE_ENV, "")
    assert scaled(4) == 4


@pytest.mark.parametrize("value", ["slow", "0", "-1"])
def test_a_scale_that_is_not_a_positive_number_is_refused(monkeypatch, value):
    monkeypatch.setenv(SCALE_ENV, value)
    with pytest.raises(ValueError, match=SCALE_ENV):
        scaled(1)
