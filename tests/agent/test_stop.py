"""T-07: graceful stop - abort in-flight, bounded grace with kill escalation."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import ENV_STATE_DIR, read_status

STUB = Path(__file__).parent / "stub_pi.py"


def wait_until(cond, timeout=10.0, interval=0.02):
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

    def make(name: str, scenario: dict, grace: float = 5.0):
        capture = tmp_path / f"capture-{name}.jsonl"
        scenario = {**scenario, "capture": str(capture)}
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file)],
                cwd=tmp_path,
                base_dir=base,
                grace=grace,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        return host, capture

    yield make, base
    for host in hosts:
        host.close()


def captured_types(capture: Path) -> list[str]:
    if not capture.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in capture.read_text(encoding="utf-8").split("\n")
        if line
    ]


def test_stop_mid_run_aborts_before_termination(make_host):
    make, base = make_host
    host, capture = make("g1", {"mode": "never_settle"})
    with connect("g1", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        assert wait_until(lambda: host.state == "working")
        response = client.stop(timeout=10)
    assert response["success"] is True

    assert wait_until(lambda: read_status(host.paths.status)["state"] == "stopped")
    assert host.proc.poll() is not None
    # the stub received abort after the prompt, before dying
    assert captured_types(capture) == ["prompt", "abort"]
    # abort settled the run: pi exited on terminate, not kill
    assert host.proc.returncode == -signal.SIGTERM

    events = read_events(host.paths.events)
    types = [e["type"] for e in events]
    assert "host_stop_requested" in types
    assert "host_abort_sent" in types
    assert types.index("host_abort_sent") < types.index("process_exit")
    assert "host_escalated_kill" not in types
    # final state line is stopped; logs retained and readable
    state_lines = [e["state"] for e in events if e["type"] == "host_state"]
    assert state_lines[-1] == "stopped"
    assert read_status(host.paths.status)["state"] == "stopped"
    assert not host.paths.socket.exists()


def test_stop_escalates_to_kill_within_grace(make_host):
    make, base = make_host
    host, capture = make(
        "g2",
        {"mode": "never_settle", "ignore_sigterm": True, "ignore_abort": True},
        grace=0.5,
    )
    with connect("g2", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        assert wait_until(lambda: host.state == "working")
        started = time.monotonic()
        client.stop(timeout=10)
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "stopped")
    elapsed = time.monotonic() - started
    assert elapsed < 5.0  # both grace windows (abort + term) plus slack
    assert host.proc.returncode == -signal.SIGKILL

    types = [e["type"] for e in read_events(host.paths.events)]
    assert "host_abort_sent" in types
    assert "host_escalated_kill" in types


def test_stop_idle_agent_sends_no_abort(make_host):
    make, base = make_host
    host, capture = make("g3", {"reply": "ok"})
    with connect("g3", base_dir=base) as client:
        client.stop(timeout=10)
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "stopped")
    assert "abort" not in captured_types(capture)
    types = [e["type"] for e in read_events(host.paths.events)]
    assert "host_abort_sent" not in types


# -- the CLI verb, against a detached host ----------------------------------


@pytest.fixture
def rig(tmp_path):
    base = tmp_path / "state"
    env = {**os.environ, ENV_STATE_DIR: str(base)}

    def run_cli(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from specflo.cli import main; sys.exit(main())",
                "agent",
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=45,
        )

    yield run_cli, base, tmp_path

    if base.is_dir():
        for agent_dir in base.iterdir():
            status_file = agent_dir / "status.json"
            if not status_file.is_file():
                continue
            snapshot = json.loads(status_file.read_text())
            for key in ("pi_pid", "host_pid"):
                pid = snapshot.get(key)
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


def test_cli_stop_takes_down_host_and_pi(rig):
    run_cli, base, tmp_path = rig
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(json.dumps({"mode": "never_settle"}), encoding="utf-8")
    result = run_cli(
        "start", "c1", "--cwd", ".", "--pi-cmd",
        f"{sys.executable} {STUB} {scenario_file}",
    )
    assert result.returncode == 0, result.stderr
    result = run_cli("prompt", "c1", "go", "--no-wait")
    assert result.returncode == 0, result.stderr
    snapshot = json.loads((base / "c1" / "status.json").read_text())

    result = run_cli("stop", "c1")
    assert result.returncode == 0, result.stderr
    assert "stopped agent 'c1'" in result.stdout

    for pid in (snapshot["host_pid"], snapshot["pi_pid"]):
        with pytest.raises(OSError):
            os.kill(pid, 0)
    final = json.loads((base / "c1" / "status.json").read_text())
    assert final["state"] == "stopped"
    # retained logs stay readable through the CLI
    result = run_cli("log", "c1")
    assert result.returncode == 0
    assert '"host_state"' in result.stdout

    # stop is friendly when already stopped; unknown names are not
    result = run_cli("stop", "c1")
    assert result.returncode == 0
    assert "already stopped" in result.stdout
    assert run_cli("stop", "ghost").returncode == 12
