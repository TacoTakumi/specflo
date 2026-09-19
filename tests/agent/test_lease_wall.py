"""The lease wall: a pooled host lets only the lease holder drive its pi."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import read_status

STUB = Path(__file__).parent / "stub_pi.py"

POOL = "pool-secret"
HOLDER = "holder-secret"
DRIVING = ("prompt", "steer", "follow_up", "new_session", "abort", "stop")


def captured(path: Path) -> list[dict]:
    """Every frame the stub pi received on stdin."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def frame_for(ctype: str, **extra) -> dict:
    frame = {"type": ctype, **extra}
    if ctype in ("prompt", "steer", "follow_up"):
        frame["message"] = "intruder text"
    return frame


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    base = tmp_path / "state"

    def make(name: str, scenario: dict | None = None):
        capture = tmp_path / f"capture-{name}.jsonl"
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(
            json.dumps({"reply": "ok", "capture": str(capture), **(scenario or {})}),
            encoding="utf-8",
        )
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file)],
                cwd=tmp_path,
                base_dir=base,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        return host, capture

    yield make, base
    for host in hosts:
        host.close()


def lease(name: str, base: Path) -> None:
    """Bind the pool and one lease the way the pool daemon does at grant."""
    with connect(name, base_dir=base) as daemon:
        daemon.pool_bind(POOL)
        daemon.lease_bind(POOL, HOLDER)


def test_pool_bind_is_accepted_once(make_host):
    make, base = make_host
    make("w1")
    with connect("w1", base_dir=base) as client:
        first = client.request({"type": "pool_bind", "pool_token": POOL})
        assert first["success"] is True
        assert first["command"] == "pool_bind"
        # the pool that is bound may say so again; no other pool is let in
        again = client.request({"type": "pool_bind", "pool_token": POOL})
        assert again["success"] is True
        other = client.request({"type": "pool_bind", "pool_token": "another-pool"})
        assert other["success"] is False
        assert other["error"]


def test_pool_bind_needs_a_token(make_host):
    make, base = make_host
    make("w2")
    with connect("w2", base_dir=base) as client:
        for frame in ({"type": "pool_bind"}, {"type": "pool_bind", "pool_token": ""}):
            assert client.request(frame)["success"] is False
        # a refused bind leaves the host unbound, so a proper one still lands
        assert client.request({"type": "pool_bind", "pool_token": POOL})["success"]


def test_lease_verbs_are_refused_before_a_pool_binding(make_host):
    make, base = make_host
    _, capture = make("w3")
    with connect("w3", base_dir=base) as client:
        bind = client.request(
            {"type": "lease_bind", "pool_token": POOL, "lease_token": HOLDER}
        )
        clear = client.request({"type": "lease_clear", "pool_token": POOL})
        assert bind["success"] is False
        assert clear["success"] is False
        # no wall went up: an untokened prompt still runs
        client.send({"type": "prompt", "message": "go"})
        client.read_until(lambda f: f.get("type") == "agent_settled", timeout=5)
    assert [f["type"] for f in captured(capture)] == ["prompt"]


def test_lease_verbs_need_the_pool_token(make_host):
    make, base = make_host
    make("w4")
    with connect("w4", base_dir=base) as client:
        client.pool_bind(POOL)
        for bad in ({}, {"pool_token": "wrong"}, {"pool_token": HOLDER}):
            refused = client.request(
                {"type": "lease_bind", "lease_token": HOLDER, **bad}
            )
            assert refused["success"] is False
        # the refused binds raised no wall
        assert client.request({"type": "get_last_assistant_text"})["success"] is True

        assert client.request(
            {"type": "lease_bind", "pool_token": POOL, "lease_token": HOLDER}
        )["success"] is True
        for bad in ({}, {"pool_token": "wrong"}, {"pool_token": HOLDER}):
            refused = client.request({"type": "lease_clear", **bad})
            assert refused["success"] is False
        # the refused clears left the wall standing
        assert client.request({"type": "get_last_assistant_text"})["success"] is False


def test_lease_bind_needs_a_lease_token_and_a_free_host(make_host):
    make, base = make_host
    make("w5")
    with connect("w5", base_dir=base) as client:
        client.pool_bind(POOL)
        assert client.request({"type": "lease_bind", "pool_token": POOL})[
            "success"
        ] is False
        client.lease_bind(POOL, HOLDER)
        # one lease at a time: the daemon clears before it binds the next
        over = client.request(
            {"type": "lease_bind", "pool_token": POOL, "lease_token": "next-holder"}
        )
        assert over["success"] is False
        with connect("w5", base_dir=base, lease_token=HOLDER) as holder:
            assert holder.request({"type": "get_last_assistant_text"})["success"]


@pytest.mark.parametrize(
    "credentials",
    [{}, {"lease_token": "wrong"}, {"pool_token": "wrong"}, {"lease_token": POOL}],
    ids=["none", "wrong-lease-token", "wrong-pool-token", "pool-token-as-lease-token"],
)
def test_driving_frames_without_a_valid_token_are_refused(make_host, credentials):
    make, base = make_host
    host, capture = make("w6")
    lease("w6", base)
    events_before = host.paths.events.read_text()

    with connect("w6", base_dir=base) as intruder:
        for ctype in DRIVING:
            response = intruder.request(frame_for(ctype, **credentials))
            assert response["type"] == "response"
            assert response["command"] == ctype
            assert response["success"] is False
            assert "lease" in response["error"]

    # an authorised round trip through pi orders the check: whatever the
    # refused frames would have caused has happened by the time it answers
    with connect("w6", base_dir=base, lease_token=HOLDER) as holder:
        assert holder.request({"type": "get_last_assistant_text"})["success"] is True
    assert [f["type"] for f in captured(capture)] == ["get_last_assistant_text"]
    # the log grew by that probe's answer alone: no refused frame left a line
    added = host.paths.events.read_text()[len(events_before) :].splitlines()
    assert [json.loads(line)["command"] for line in added] == [
        "get_last_assistant_text"
    ]
    # the refused stop did not stop anything
    assert read_status(host.paths.status)["state"] == "idle"
    assert host.proc.poll() is None


def test_other_passthrough_frames_are_walled_too(make_host):
    make, base = make_host
    _, capture = make("w7")
    lease("w7", base)
    with connect("w7", base_dir=base) as intruder:
        for ctype in ("get_last_assistant_text", "set_model", "extension_ui_response"):
            assert intruder.request({"type": ctype})["success"] is False
        # the liveness probe stays open: start, list and the ledger rely on it
        assert intruder.status()["status"]["state"] == "idle"
        # ...but a token presented with it is still checked
        refused = intruder.request({"type": "status", "lease_token": "wrong"})
        assert refused["success"] is False
    assert captured(capture) == []


def test_holder_token_passes_and_never_reaches_pi(make_host):
    make, base = make_host
    host, capture = make("w8")
    lease("w8", base)
    with connect("w8", base_dir=base, lease_token=HOLDER) as holder:
        request_id = holder.send({"type": "prompt", "message": "go"})
        seen = []
        while not any(f.get("type") == "agent_settled" for f in seen):
            seen.append(holder.read(timeout=5))
        response = next(f for f in seen if f.get("type") == "response")
        assert response["id"] == request_id
        assert response["success"] is True
        # pi answers these itself: the wall let them through
        for ctype in ("steer", "follow_up", "new_session"):
            answer = holder.request(frame_for(ctype))
            assert answer["error"] == "stub: unhandled command"
        assert holder.request({"type": "abort"})["success"] is True
        assert holder.status()["status"]["state"] == "idle"

    frames = captured(capture)
    assert [f["type"] for f in frames] == [
        "prompt",
        "steer",
        "follow_up",
        "new_session",
        "abort",
    ]
    assert frames[0]["message"] == "go"
    assert not any("lease_token" in f or "pool_token" in f for f in frames)
    log = host.paths.events.read_text()
    assert '"host_forward"' in log
    assert HOLDER not in log and POOL not in log


def test_holder_stop_stops_the_host(make_host):
    make, base = make_host
    host, _ = make("w9")
    lease("w9", base)
    with connect("w9", base_dir=base, lease_token=HOLDER) as holder:
        assert holder.stop()["success"] is True
    assert host.wait_stopped(timeout=10)
    assert read_status(host.paths.status)["state"] == "stopped"


def test_pool_token_drives_a_leased_member(make_host):
    make, base = make_host
    host, capture = make("w10")
    lease("w10", base)
    with connect("w10", base_dir=base) as daemon:
        assert daemon.request({"type": "abort", "pool_token": POOL})["success"] is True
        # pi got the frame without the credential
        assert [sorted(f) for f in captured(capture)] == [["id", "type"]]
        assert daemon.request({"type": "stop", "pool_token": POOL})["success"] is True
    assert host.wait_stopped(timeout=10)


def test_lease_clear_takes_the_wall_down_and_the_next_lease_raises_it(make_host):
    make, base = make_host
    _, capture = make("w11")
    lease("w11", base)
    with connect("w11", base_dir=base) as daemon:
        daemon.lease_clear(POOL)
        # clearing twice is harmless: the daemon may retry an ending
        daemon.lease_clear(POOL)
        daemon.send({"type": "prompt", "message": "unleased"})
        daemon.read_until(lambda f: f.get("type") == "agent_settled", timeout=5)
        daemon.lease_bind(POOL, "second-holder")
    with connect("w11", base_dir=base, lease_token=HOLDER) as former:
        assert former.request({"type": "get_last_assistant_text"})["success"] is False
    with connect("w11", base_dir=base, lease_token="second-holder") as holder:
        assert holder.request({"type": "get_last_assistant_text"})["success"] is True
    assert [f["type"] for f in captured(capture)] == [
        "prompt",
        "get_last_assistant_text",
    ]


def test_unbound_host_forwards_frames_verbatim(make_host):
    make, base = make_host
    _, capture = make("w12")
    with connect("w12", base_dir=base, lease_token="stale-token") as client:
        request_id = client.send({"type": "prompt", "message": "go"})
        client.read_until(lambda f: f.get("type") == "agent_settled", timeout=5)
    # no pool binding: the host is a plain pipe, exactly as before
    assert captured(capture) == [
        {
            "id": request_id,
            "type": "prompt",
            "message": "go",
            "lease_token": "stale-token",
        }
    ]


def test_client_binding_helpers_raise_when_refused(make_host):
    make, base = make_host
    make("w13")
    with connect("w13", base_dir=base) as client:
        with pytest.raises(RuntimeError):
            client.lease_bind(POOL, HOLDER)
        client.pool_bind(POOL)
        with pytest.raises(RuntimeError):
            client.pool_bind("another-pool")
        with pytest.raises(RuntimeError):
            client.lease_clear("wrong")
