"""T-03: host core - stdio holder, event pump, lifecycle state, exit detection."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.host import PiHost
from specflo.agent.statefiles import AgentPaths, read_status

STUB = Path(__file__).parent / "stub_pi.py"


def wait_until(cond, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def read_events(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").split("\n")
        if line
    ]


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    base = tmp_path / "state"

    def make(name: str, scenario: dict) -> PiHost:
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        host = PiHost(
            name,
            [sys.executable, str(STUB), str(scenario_file)],
            cwd=tmp_path,
            base_dir=base,
        ).start()
        hosts.append(host)
        return host

    yield make
    for host in hosts:
        host.close()


def test_prompt_cycle_records_events_and_transitions(make_host, tmp_path):
    host = make_host("h1", {"reply": "done"})
    status = read_status(host.paths.status)
    assert status["state"] == "idle"
    assert status["name"] == "h1"
    assert status["host_pid"] is not None
    assert status["pi_pid"] == host.proc.pid
    assert status["last_activity"]

    host.send({"id": "p1", "type": "prompt", "message": "go"})
    assert wait_until(
        lambda: any(
            e["type"] == "agent_settled" for e in read_events(host.paths.events)
        )
        and read_status(host.paths.status)["state"] == "idle"
    )

    events = read_events(host.paths.events)
    keyed = [
        (e["type"], e.get("state"))
        for e in events
        if e["type"] in ("host_state", "agent_start", "agent_settled")
    ]
    assert keyed == [
        ("host_state", "starting"),
        ("host_state", "idle"),
        ("agent_start", None),
        ("host_state", "working"),
        ("agent_settled", None),
        ("host_state", "idle"),
    ]
    # every pi frame was pumped to disk, response and message events included
    types = [e["type"] for e in events]
    assert "response" in types
    assert "message_end" in types
    # timestamps are chronological
    ts = [e["ts"] for e in events]
    assert ts == sorted(ts)


def test_working_state_visible_during_open_run(make_host):
    host = make_host("h2", {"mode": "never_settle"})
    host.send({"id": "p1", "type": "prompt", "message": "go"})
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "working")
    # stays working: the run never settles
    time.sleep(0.2)
    assert read_status(host.paths.status)["state"] == "working"


def test_killed_pi_lands_exited_within_5s(make_host):
    host = make_host("h3", {"reply": "done"})
    host.proc.kill()
    start = time.monotonic()
    assert wait_until(
        lambda: read_status(host.paths.status)["state"] == "exited", timeout=5.0
    )
    assert time.monotonic() - start < 5.0
    events = read_events(host.paths.events)
    exit_events = [e for e in events if e["type"] == "process_exit"]
    assert len(exit_events) == 1
    assert exit_events[0]["exit_code"] == -9


def test_stub_clean_exit_lands_exited_with_code(make_host):
    host = make_host("h4", {"mode": "exit", "exit_code": 3})
    host.send({"id": "p1", "type": "prompt", "message": "go"})
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "exited")
    events = read_events(host.paths.events)
    assert [e for e in events if e["type"] == "process_exit"][0]["exit_code"] == 3
    # the run that died mid-flight was recorded before the exit
    types = [e["type"] for e in events]
    assert types.index("agent_start") < types.index("process_exit")


def test_send_after_exit_raises(make_host):
    host = make_host("h5", {"reply": "done"})
    host.proc.kill()
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "exited")
    with pytest.raises(RuntimeError):
        host.send({"type": "prompt", "message": "go"})
