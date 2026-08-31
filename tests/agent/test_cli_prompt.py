"""T-06: prompt and retrieval verbs - blocking reply, exit codes, busy semantics."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
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


@pytest.fixture
def rig(tmp_path):
    base = tmp_path / "state"
    env = {**os.environ, ENV_STATE_DIR: str(base)}
    cli_argv = [
        sys.executable,
        "-c",
        "import sys; from specflo.cli import main; sys.exit(main())",
        "agent",
    ]

    def run_cli(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            [*cli_argv, *args], capture_output=True, text=True, env=env,
            timeout=timeout,
        )

    def spawn_cli(*args: str) -> subprocess.Popen:
        return subprocess.Popen(
            [*cli_argv, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

    started: list[str] = []

    def start_agent(name: str, scenario: dict) -> Path:
        scenario_file = tmp_path / f"scenario-{name}.json"
        capture = tmp_path / f"capture-{name}.jsonl"
        scenario = {**scenario, "capture": str(capture)}
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        result = run_cli(
            "start", name, "--cwd", ".", "--pi-cmd",
            f"{sys.executable} {STUB} {scenario_file}",
        )
        assert result.returncode == 0, result.stderr
        started.append(name)
        return capture

    yield run_cli, spawn_cli, start_agent, base

    for name in started:
        status_file = base / name / "status.json"
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


def captured_prompts(capture: Path) -> list[dict]:
    if not capture.exists():
        return []
    frames = [
        json.loads(line)
        for line in capture.read_text(encoding="utf-8").split("\n")
        if line
    ]
    return [f for f in frames if f.get("type") == "prompt"]


def agent_state(base: Path, name: str) -> str:
    return json.loads((base / name / "status.json").read_text())["state"]


def test_blocking_prompt_prints_known_reply(rig):
    run_cli, spawn_cli, start_agent, base = rig
    start_agent("a1", {"reply": "the known reply", "stream": True})
    result = run_cli("prompt", "a1", "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "the known reply\n"


def test_prompt_timeout_exits_11(rig):
    run_cli, spawn_cli, start_agent, base = rig
    start_agent("a1", {"mode": "never_settle"})
    result = run_cli("prompt", "a1", "hello", "--timeout", "1")
    assert result.returncode == 11


def test_prompt_to_stopped_host_exits_12(rig):
    run_cli, spawn_cli, start_agent, base = rig
    start_agent("a1", {"reply": "ok"})
    with connect("a1", base_dir=base) as client:
        client.stop()
    result = run_cli("prompt", "a1", "hello")
    assert result.returncode == 12


def test_busy_plain_prompt_refused_with_10_and_nothing_delivered(rig):
    run_cli, spawn_cli, start_agent, base = rig
    capture = start_agent("a1", {"mode": "never_settle"})
    result = run_cli("prompt", "a1", "first", "--no-wait")
    assert result.returncode == 0, result.stderr
    assert wait_until(lambda: agent_state(base, "a1") == "working")

    result = run_cli("prompt", "a1", "second")
    assert result.returncode == 10
    prompts = captured_prompts(capture)
    assert [p["message"] for p in prompts] == ["first"]  # nothing delivered


def test_steer_and_follow_up_deliver_shaped_prompt_commands(rig):
    run_cli, spawn_cli, start_agent, base = rig
    capture = start_agent("a1", {"mode": "never_settle"})
    run_cli("prompt", "a1", "first", "--no-wait")
    assert wait_until(lambda: agent_state(base, "a1") == "working")

    result = run_cli("prompt", "a1", "adjust course", "--steer", "--no-wait")
    assert result.returncode == 0, result.stderr
    result = run_cli("prompt", "a1", "afterwards", "--follow-up", "--no-wait")
    assert result.returncode == 0, result.stderr

    def delivered():
        return {
            p["message"]: p.get("streamingBehavior") for p in captured_prompts(capture)
        }

    assert wait_until(lambda: len(delivered()) == 3)
    shapes = delivered()
    assert shapes["adjust course"] == "steer"
    assert shapes["afterwards"] == "followUp"
    assert shapes["first"] is None


def test_no_wait_then_wait_then_last_recover_the_reply(rig):
    run_cli, spawn_cli, start_agent, base = rig
    start_agent(
        "a1",
        {
            "mode": "dialog",
            "reply": "recovered reply",
            "dialogs": [{"method": "confirm", "title": "Proceed?"}],
        },
    )
    # the stub blocks on its dialog, so --no-wait provably returns pre-settle
    result = run_cli("prompt", "a1", "go", "--no-wait")
    assert result.returncode == 0, result.stderr
    assert wait_until(lambda: agent_state(base, "a1") == "working")

    waiter = spawn_cli("wait", "a1", "--timeout", "20")
    time.sleep(0.3)
    assert waiter.poll() is None  # still blocked: the run has not settled

    # the dialog request predates our connection; recover its id from the log
    events_file = base / "a1" / "events.jsonl"

    def dialog_request():
        for line in events_file.read_text(encoding="utf-8").split("\n"):
            if line and '"extension_ui_request"' in line:
                return json.loads(line)
        return None

    assert wait_until(lambda: dialog_request() is not None)
    with connect("a1", base_dir=base) as client:
        client.send(
            {
                "type": "extension_ui_response",
                "id": dialog_request()["id"],
                "confirmed": True,
            }
        )

    assert waiter.wait(timeout=15) == 0

    result = run_cli("last", "a1")
    assert result.returncode == 0
    assert result.stdout == "recovered reply\n"


def test_wait_on_idle_agent_returns_immediately(rig):
    run_cli, spawn_cli, start_agent, base = rig
    start_agent("a1", {"reply": "ok"})
    result = run_cli("wait", "a1")
    assert result.returncode == 0


def test_log_prints_events_and_follow_streams_settle(rig):
    run_cli, spawn_cli, start_agent, base = rig
    start_agent("a1", {"reply": "logged"})

    follower = spawn_cli("log", "a1", "--follow")
    lines: queue.Queue = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(l) for l in follower.stdout], daemon=True
    ).start()

    result = run_cli("prompt", "a1", "go")
    assert result.returncode == 0

    seen = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            seen.append(lines.get(timeout=1))
        except queue.Empty:
            continue
        if '"agent_settled"' in seen[-1]:
            break
    follower.kill()
    follower.wait(timeout=5)
    assert any('"agent_settled"' in line for line in seen)
    assert any('"host_state"' in line for line in seen)

    # non-follow log prints the same history and exits
    result = run_cli("log", "a1")
    assert result.returncode == 0
    assert '"agent_start"' in result.stdout


def test_exit_codes_stated_in_help(rig):
    run_cli, spawn_cli, start_agent, base = rig
    for verb in ("prompt", "wait", "last", "log"):
        result = run_cli(verb, "--help")
        assert result.returncode == 0
        for token in ("10", "11", "12", "busy", "timeout", "unreachable"):
            assert token in result.stdout, (verb, token)


def test_last_and_wait_unreachable_exit_12(rig):
    run_cli, spawn_cli, start_agent, base = rig
    assert run_cli("last", "ghost").returncode == 12
    assert run_cli("wait", "ghost").returncode == 12
    assert run_cli("log", "ghost").returncode == 12
