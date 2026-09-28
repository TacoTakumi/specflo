"""Lease activity: the host stamps last_activity in status.json for the holder.

The pool daemon computes a lease's expiry from this stamp, so only what the
holder does - and a turn that is running - may move it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from specflo.agent import host as host_module
from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import read_status
from waits import wait_until

STUB = Path(__file__).parent / "stub_pi.py"

POOL = "pool-secret"
HOLDER = "holder-secret"

STATUS_KEYS = {
    "name",
    "state",
    "host_pid",
    "pi_pid",
    "context_percent",
    "herdr_workspace",
    "herdr_tab",
    "herdr_pane",
    "last_activity",
}


class FakeClock:
    """A clock the test moves by hand; reads as minutes past a fixed hour."""

    def __init__(self) -> None:
        self.minute = 0

    def at(self, minute: int) -> str:
        return f"2026-01-01T00:{minute:02d}:00.000+00:00"

    def __call__(self) -> str:
        return self.at(self.minute)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_host(tmp_path, clock):
    hosts = []
    base = tmp_path / "state"

    def make(name: str, scenario: dict | None = None, **kwargs) -> PiHost:
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(
            json.dumps({"reply": "ok", **(scenario or {})}), encoding="utf-8"
        )
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file)],
                cwd=tmp_path,
                base_dir=base,
                clock=clock,
                **kwargs,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        return host

    yield make, base
    for host in hosts:
        host.close()


def lease(name: str, base: Path) -> None:
    """Bind the pool and one lease the way the pool daemon does at grant."""
    with connect(name, base_dir=base) as daemon:
        daemon.pool_bind(POOL)
        daemon.lease_bind(POOL, HOLDER)


def activity(host: PiHost) -> str:
    return read_status(host.paths.status)["last_activity"]


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "status"},
        {"type": "new_session"},
        {"type": "get_last_assistant_text"},
    ],
    ids=["status", "new_session", "last"],
)
def test_a_holder_command_stamps_the_activity_time(make_host, clock, frame):
    make, base = make_host
    host = make("a1")
    lease("a1", base)
    assert activity(host) == clock.at(0)

    clock.minute = 9
    with connect("a1", base_dir=base, lease_token=HOLDER) as holder:
        holder.request(frame)
    assert activity(host) == clock.at(9)
    # the stamp moved alone: the snapshot is otherwise what it was
    status = read_status(host.paths.status)
    assert set(status) == STATUS_KEYS
    assert status["state"] == "idle"
    assert status["pi_pid"] == host.proc.pid


def test_a_holder_status_answer_carries_its_own_stamp(make_host, clock):
    make, base = make_host
    make("a2")
    lease("a2", base)
    clock.minute = 4
    with connect("a2", base_dir=base, lease_token=HOLDER) as holder:
        assert holder.status()["status"]["last_activity"] == clock.at(4)


def test_a_holder_prompt_and_its_wait_stamp_the_activity_time(make_host, clock):
    make, base = make_host
    host = make("a3", {"mode": "never_settle"})
    lease("a3", base)

    clock.minute = 3
    with connect("a3", base_dir=base, lease_token=HOLDER) as holder:
        assert holder.request({"type": "prompt", "message": "go"})["success"]
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "working")
    assert activity(host) == clock.at(3)

    # a wait opens with the holder's status frame, then only listens
    clock.minute = 7
    with connect("a3", base_dir=base, lease_token=HOLDER) as waiter:
        assert waiter.status()["status"]["state"] == "working"
        assert activity(host) == clock.at(7)
        clock.minute = 8
        waiter.send({"type": "abort"})
        waiter.read_until(lambda f: f.get("type") == "agent_settled", timeout=5)
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "idle")
    assert activity(host) == clock.at(8)


@pytest.mark.parametrize(
    "credentials",
    [{}, {"lease_token": "wrong"}, {"pool_token": "wrong"}],
    ids=["none", "wrong-lease-token", "wrong-pool-token"],
)
def test_a_refused_command_leaves_the_activity_time_alone(
    make_host, clock, credentials
):
    make, base = make_host
    host = make("a4")
    lease("a4", base)

    clock.minute = 11
    with connect("a4", base_dir=base) as intruder:
        for ctype in ("prompt", "new_session", "get_last_assistant_text", "stop"):
            refused = intruder.request({"type": ctype, "message": "x", **credentials})
            assert refused["success"] is False
        if credentials:
            assert intruder.request({"type": "status", **credentials})[
                "success"
            ] is False
    assert activity(host) == clock.at(0)


def test_the_bare_liveness_probe_is_not_holder_activity(make_host, clock):
    make, base = make_host
    host = make("a5")
    lease("a5", base)
    clock.minute = 11
    with connect("a5", base_dir=base) as anyone:
        assert anyone.status()["status"]["last_activity"] == clock.at(0)
    assert activity(host) == clock.at(0)


def test_the_daemon_driving_a_member_is_not_holder_activity(make_host, clock):
    make, base = make_host
    host = make("a6")
    lease("a6", base)
    clock.minute = 11
    with connect("a6", base_dir=base) as daemon:
        assert daemon.request({"type": "status", "pool_token": POOL})["success"]
        assert daemon.request({"type": "abort", "pool_token": POOL})["success"]
    assert activity(host) == clock.at(0)


def test_a_working_turn_refreshes_the_activity_time(make_host, clock, monkeypatch):
    monkeypatch.setattr(host_module, "_ACTIVITY_REFRESH", 0.0)
    make, base = make_host
    dialogs = [{"method": "confirm", "title": "Proceed?"}]
    host = make("a7", {"mode": "dialog", "dialogs": dialogs}, auto_answer=False)
    lease("a7", base)

    clock.minute = 5
    with connect("a7", base_dir=base, lease_token=HOLDER) as holder:
        holder.send({"type": "prompt", "message": "go"})
        holder.read_until(
            lambda f: f.get("type") == "extension_ui_request", timeout=5
        )
    assert activity(host) == clock.at(5)

    # pi is still mid-turn at minute 20; the daemon's frame moves nothing by
    # itself, but the event pi emits in answer shows the turn is alive
    clock.minute = 20
    with connect("a7", base_dir=base) as daemon:
        busy = daemon.request({"type": "get_last_assistant_text", "pool_token": POOL})
        assert busy["error"] == "stub: busy"
    assert wait_until(lambda: activity(host) == clock.at(20))
    assert read_status(host.paths.status)["state"] == "working"


def test_the_working_refresh_is_throttled(make_host, clock, monkeypatch):
    monkeypatch.setattr(host_module, "_ACTIVITY_REFRESH", 3600.0)
    make, base = make_host
    dialogs = [{"method": "confirm", "title": "Proceed?"}]
    host = make("a8", {"mode": "dialog", "dialogs": dialogs}, auto_answer=False)
    lease("a8", base)

    clock.minute = 5
    with connect("a8", base_dir=base, lease_token=HOLDER) as holder:
        holder.send({"type": "prompt", "message": "go"})
        holder.read_until(
            lambda f: f.get("type") == "extension_ui_request", timeout=5
        )
    clock.minute = 6
    with connect("a8", base_dir=base) as daemon:
        # pi answers in order, so by the second answer the pump is done
        # with the first one
        for _ in range(2):
            daemon.request({"type": "get_last_assistant_text", "pool_token": POOL})
        assert activity(host) == clock.at(5)
        # the end of the turn is a transition, and that always lands
        clock.minute = 25
        daemon.send({"type": "abort", "pool_token": POOL})
        daemon.read_until(lambda f: f.get("type") == "agent_settled", timeout=5)
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "idle")
    assert activity(host) == clock.at(25)


def test_a_host_with_no_lease_keeps_its_status_file_as_it_was(make_host, clock):
    make, base = make_host
    host = make("a9")
    before = read_status(host.paths.status)
    assert set(before) == STATUS_KEYS
    assert before["last_activity"] == clock.at(0)

    clock.minute = 2
    with connect("a9", base_dir=base, lease_token="stale-token") as client:
        assert client.status()["status"] == before
        client.request({"type": "get_last_assistant_text"})
    # no lease, nothing to renew: commands leave the file alone
    assert read_status(host.paths.status) == before

    # a pool binding without a lease changes nothing either
    with connect("a9", base_dir=base) as daemon:
        daemon.pool_bind(POOL)
        daemon.request({"type": "status", "pool_token": POOL})
    assert read_status(host.paths.status) == before

    # state changes still stamp, as they always have
    clock.minute = 3
    with connect("a9", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        client.read_until(lambda f: f.get("type") == "agent_settled", timeout=5)
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "idle")
    assert activity(host) == clock.at(3)


def test_the_default_clock_writes_a_utc_timestamp(tmp_path):
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps({"reply": "ok"}), encoding="utf-8")
    host = PiHost(
        "a10",
        [sys.executable, str(STUB), str(scenario_file)],
        cwd=tmp_path,
        base_dir=tmp_path / "state",
    ).start()
    try:
        assert activity(host).endswith("+00:00")
    finally:
        host.close()
