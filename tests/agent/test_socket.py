"""T-04: Unix-socket control surface - host verbs, passthrough, client library."""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.client import AgentClient, HostUnreachableError, connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import read_status

STUB = Path(__file__).parent / "stub_pi.py"


def wait_until(cond, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    base = tmp_path / "state"

    def make(name: str, scenario: dict) -> PiHost:
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
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
        return host

    yield make, base
    for host in hosts:
        host.close()


def test_status_request_returns_one_lf_terminated_correlated_line(make_host):
    make, base = make_host
    host = make("s1", {"reply": "ok"})

    # raw socket: assert the wire format itself
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(5)
    raw.connect(str(host.paths.socket))
    raw.sendall(b'{"id":"req-42","type":"status"}\n')
    buf = b""
    while b"\n" not in buf:
        buf += raw.recv(65536)
    raw.close()
    line, rest = buf.split(b"\n", 1)
    assert rest == b""  # exactly one LF-terminated response
    response = json.loads(line)
    assert response["id"] == "req-42"
    assert response["type"] == "response"
    assert response["command"] == "status"
    assert response["success"] is True
    status = response["data"]["status"]
    assert status["name"] == "s1"
    assert status["state"] == "idle"
    paths = response["data"]["paths"]
    assert Path(paths["events"]).is_file()
    assert Path(paths["status"]).is_file()
    assert Path(paths["socket"]).is_socket()


def test_passthrough_prompt_round_trips_stub_events(make_host):
    make, base = make_host
    host = make("s2", {"reply": "the reply", "stream": True})
    with connect("s2", base_dir=base) as client:
        request_id = client.send({"type": "prompt", "message": "go"})
        seen = []
        while not any(f.get("type") == "agent_settled" for f in seen):
            seen.append(client.read(timeout=5))

    types = [f.get("type") for f in seen]
    # pi's own response frame came back, correlated by our id
    response = next(f for f in seen if f.get("type") == "response")
    assert response["id"] == request_id
    assert response["command"] == "prompt"
    assert response["success"] is True
    assert "agent_start" in types
    message_end = next(f for f in seen if f.get("type") == "message_end")
    assert message_end["message"]["content"][0]["text"] == "the reply"


def test_events_broadcast_to_other_connected_clients(make_host):
    make, base = make_host
    make("s3", {"reply": "shared"})
    with connect("s3", base_dir=base) as watcher, connect("s3", base_dir=base) as driver:
        driver.send({"type": "prompt", "message": "go"})
        seen = []
        while not any(f.get("type") == "agent_settled" for f in seen):
            seen.append(watcher.read(timeout=5))
    assert any(f.get("type") == "agent_start" for f in seen)


def test_request_queues_interleaved_frames_for_later_reads(make_host):
    make, base = make_host
    host = make("s4", {"reply": "ok"})
    with connect("s4", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        assert wait_until(
            lambda: read_status(host.paths.status)["state"] == "idle"
            and any(
                '"agent_settled"' in line
                for line in host.paths.events.read_text().split("\n")
            )
        )
        # a request issued now still returns its own response...
        data = client.status()
        assert data["status"]["state"] == "idle"
        # ...and the prompt-cycle frames remain readable afterwards, in order
        types = []
        while "agent_settled" not in types:
            types.append(client.read(timeout=5).get("type"))
        assert types.index("agent_start") < types.index("agent_settled")


def test_passthrough_to_dead_pi_returns_error_response(make_host):
    make, base = make_host
    host = make("s5", {"reply": "ok"})
    host.proc.kill()
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "exited")
    with connect("s5", base_dir=base) as client:
        response = client.request({"type": "prompt", "message": "go"}, timeout=5)
    assert response["success"] is False
    assert response["command"] == "prompt"
    assert "error" in response


def test_stop_verb_lands_stopped_state_and_closes_surface(make_host):
    make, base = make_host
    host = make("s6", {"reply": "ok"})
    with connect("s6", base_dir=base) as client:
        response = client.stop(timeout=5)
    assert response["success"] is True
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "stopped")
    assert wait_until(lambda: host.proc.poll() is not None)
    # the surface is gone: fresh connections are refused
    assert wait_until(lambda: not host.paths.socket.exists())
    with pytest.raises(HostUnreachableError):
        connect("s6", base_dir=base)
    # state files are retained
    assert host.paths.events.is_file()
    assert host.paths.status.is_file()


def test_malformed_request_gets_parse_error_response(make_host):
    make, base = make_host
    host = make("s7", {"reply": "ok"})
    raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    raw.settimeout(5)
    raw.connect(str(host.paths.socket))
    raw.sendall(b"this is not json\n")
    buf = b""
    while b"\n" not in buf:
        buf += raw.recv(65536)
    raw.close()
    response = json.loads(buf.split(b"\n", 1)[0])
    assert response["type"] == "response"
    assert response["success"] is False
    assert response["command"] == "parse"


def test_connect_without_host_raises_unreachable(tmp_path):
    with pytest.raises(HostUnreachableError):
        AgentClient(tmp_path / "absent" / "sock")


def test_idle_client_survives_broadcast_send_timeout(make_host, monkeypatch):
    # regression (found by the T-14 live smoke): a broadcast sets a send
    # timeout on the shared socket; the recv loop must not treat the
    # resulting idle-timeout as a dead client
    from specflo.agent import host as host_module

    monkeypatch.setattr(host_module, "_SUBSCRIBER_SEND_TIMEOUT", 0.3)
    make, base = make_host
    make("s8", {"reply": "ok"})
    with connect("s8", base_dir=base) as client:
        client.request({"type": "prompt", "message": "go"}, timeout=5)
        time.sleep(1.0)  # idle well past the send timeout, nothing flowing
        assert client.status()["status"]["name"] == "s8"
