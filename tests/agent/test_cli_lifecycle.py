"""T-05: CLI lifecycle verbs - detached start, live-checked status/list, name collisions."""

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
from specflo.agent.statefiles import ENV_STATE_DIR

STUB = Path(__file__).parent / "stub_pi.py"


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.fixture
def rig(tmp_path):
    """CLI runner with an isolated state dir, plus teardown of leftover hosts."""
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
            timeout=30,
        )

    def stub_cmd(scenario: dict, tag: str = "s") -> str:
        scenario_file = tmp_path / f"scenario-{tag}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        return f"{sys.executable} {STUB} {scenario_file}"

    yield run_cli, stub_cmd, base

    if base.is_dir():
        for agent_dir in base.iterdir():
            status_file = agent_dir / "status.json"
            if not status_file.is_file():
                continue
            snapshot = json.loads(status_file.read_text())
            for key in ("pi_pid", "host_pid"):
                pid = snapshot.get(key)
                if pid and pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass


def start_agent(run_cli, stub_cmd, name, scenario=None, tag=None):
    result = run_cli(
        "start", name, "--cwd", ".", "--pi-cmd",
        stub_cmd(scenario or {"reply": "ok"}, tag or name),
    )
    assert result.returncode == 0, result.stderr
    return result


def status_json(run_cli, name):
    result = run_cli("status", name, "--json")
    return result, json.loads(result.stdout)


def test_start_detaches_and_status_reports_req05_fields(rig):
    run_cli, stub_cmd, base = rig
    start_agent(run_cli, stub_cmd, "a1")
    # the invoking CLI process has exited; host and stub must be alive
    result, probe = status_json(run_cli, "a1")
    assert result.returncode == 0
    assert probe["alive"] is True
    assert probe["state"] == "idle"
    status = probe["status"]
    for field in (
        "name", "state", "host_pid", "pi_pid", "context_percent",
        "herdr_workspace", "herdr_tab", "herdr_pane", "last_activity",
    ):
        assert field in status
    assert pid_alive(status["host_pid"])
    assert pid_alive(status["pi_pid"])
    # host is a detached process, not a child of this test or any shell
    assert status["host_pid"] != os.getpid()
    for key in ("socket", "events", "status"):
        assert Path(probe["paths"][key]).exists()


def test_status_cycle_idle_working_idle_stopped(rig):
    run_cli, stub_cmd, base = rig
    start_agent(run_cli, stub_cmd, "a1", scenario={"mode": "never_settle"})
    assert status_json(run_cli, "a1")[1]["state"] == "idle"

    with connect("a1", base_dir=base) as client:
        client.send({"type": "prompt", "message": "go"})
        assert wait_until(
            lambda: status_json(run_cli, "a1")[1]["state"] == "working"
        )
        client.send({"type": "abort"})
        assert wait_until(lambda: status_json(run_cli, "a1")[1]["state"] == "idle")
        client.stop()

    result, probe = status_json(run_cli, "a1")
    assert result.returncode == 0  # cleanly stopped is not an error
    assert probe["state"] == "stopped"
    assert probe["alive"] is False


def test_colliding_start_refused_and_name_reusable(rig):
    run_cli, stub_cmd, base = rig
    start_agent(run_cli, stub_cmd, "a1")
    first_pid = status_json(run_cli, "a1")[1]["status"]["host_pid"]

    result = run_cli("start", "a1", "--cwd", ".", "--pi-cmd", stub_cmd({"reply": "x"}, "dup"))
    assert result.returncode != 0
    assert "a1" in result.stderr
    assert "already running" in result.stderr
    # the original host is untouched
    _, probe = status_json(run_cli, "a1")
    assert probe["status"]["host_pid"] == first_pid

    with connect("a1", base_dir=base) as client:
        client.stop()
    assert wait_until(lambda: not pid_alive(first_pid))

    start_agent(run_cli, stub_cmd, "a1", tag="again")
    _, probe = status_json(run_cli, "a1")
    assert probe["state"] == "idle"
    assert probe["status"]["host_pid"] != first_pid


def test_dead_host_detected_while_sibling_stays_live(rig):
    run_cli, stub_cmd, base = rig
    start_agent(run_cli, stub_cmd, "doomed")
    start_agent(run_cli, stub_cmd, "healthy")
    doomed_pid = status_json(run_cli, "doomed")[1]["status"]["host_pid"]

    os.kill(doomed_pid, signal.SIGKILL)
    assert wait_until(lambda: not pid_alive(doomed_pid))

    result, probe = status_json(run_cli, "doomed")
    assert result.returncode == 12
    assert probe["state"] == "dead"  # not the stale idle from status.json
    assert probe["status"]["state"] == "idle"  # the stale snapshot, exposed as such

    listing = run_cli("list")
    assert listing.returncode == 0
    lines = {line.split()[0]: line for line in listing.stdout.strip().split("\n")}
    assert "dead" in lines["doomed"]
    assert "idle" in lines["healthy"]

    # a dead name is reusable
    start_agent(run_cli, stub_cmd, "doomed", tag="reuse")
    assert status_json(run_cli, "doomed")[1]["state"] == "idle"


def test_unknown_agent_exits_unreachable(rig):
    run_cli, stub_cmd, base = rig
    result = run_cli("status", "ghost")
    assert result.returncode == 12
    assert "ghost" in result.stderr

    result = run_cli("status", "ghost", "--json")
    assert result.returncode == 12
    assert json.loads(result.stdout)["state"] == "unknown"


def test_list_without_agents(rig):
    run_cli, stub_cmd, base = rig
    result = run_cli("list")
    assert result.returncode == 0
    assert "no agents" in result.stdout
