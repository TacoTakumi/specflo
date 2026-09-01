"""Transport selector and managed TUI start (pi-interactive-transport T-07).

REQ-01: --transport selects the path - rpc stays the default v1 broker
(byte-identical, its lifecycle suite passes unmodified), tui is opt-in, and
an unknown value is rejected naming the valid ones.

REQ-04: the tui path creates the herdr tab/pane in the agent space, launches
pi there with the env handshake the extension reads (name, managed flag,
pane id, state dir), and succeeds only once the control socket accepts a
connection - here served by the Node harness runner standing in for pi. On
timeout it exits non-zero and closes the pane.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.statefiles import ENV_STATE_DIR
from test_herdr_adapter import FAKE_HERDR

RUNNER = Path(__file__).parent / "harness_runner.mjs"

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("node") is None,
    reason="node is required to serve the harness socket",
)


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def rig(tmp_path):
    """CLI runner with an isolated state dir and a fake herdr on PATH."""
    base = tmp_path / "state"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "herdr"
    script.write_text(FAKE_HERDR, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "herdr-calls.jsonl"
    env = {
        **os.environ,
        ENV_STATE_DIR: str(base),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_HERDR_LOG": str(log),
    }
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"):
        env.pop(key, None)

    procs: list[subprocess.Popen] = []

    def run_cli(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
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
            timeout=timeout,
        )

    def spawn_cli(*args: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from specflo.cli import main; sys.exit(main())",
                "agent",
                *args,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        procs.append(proc)
        return proc

    def serve_socket(name: str) -> subprocess.Popen:
        """The harness runner stands in for pi-in-the-pane: it binds the socket."""
        scenario = tmp_path / f"scenario-{name}.json"
        scenario.write_text(json.dumps({"reply": "served"}), encoding="utf-8")
        proc = subprocess.Popen(
            ["node", str(RUNNER), str(scenario)],
            cwd=tmp_path,
            env={**env, "SPECFLO_AGENT_NAME": name, "SPECFLO_AGENT_MANAGED": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    def herdr_calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().split("\n") if line]

    yield run_cli, spawn_cli, serve_socket, herdr_calls, base, env

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_unknown_transport_rejected_naming_valid_values(rig):
    run_cli, *_ = rig
    result = run_cli("start", "x1", "--transport", "bogus")
    assert result.returncode != 0
    assert "bogus" in result.stderr
    assert "rpc" in result.stderr
    assert "tui" in result.stderr


def test_tui_start_succeeds_once_the_socket_is_connectable(rig):
    run_cli, spawn_cli, serve_socket, herdr_calls, base, env = rig
    start = spawn_cli("start", "t1", "--transport", "tui", "--cwd", ".")
    # pi does not really run in the fake pane; once the CLI has issued the
    # pane launch, the harness runner serves the socket the way the extension
    # inside pi would.
    assert wait_until(
        lambda: any(call[:2] == ["pane", "run"] for call in herdr_calls())
    )
    serve_socket("t1")
    stdout, stderr = start.communicate(timeout=30)
    assert start.returncode == 0, stderr
    assert "t1" in stdout

    calls = herdr_calls()
    assert any(call[:2] == ["tab", "create"] and "t1" in call for call in calls)
    pane_runs = [call for call in calls if call[:2] == ["pane", "run"]]
    assert len(pane_runs) == 1
    command = pane_runs[0][3]
    assert command.startswith("exec env ")
    assert "SPECFLO_AGENT_NAME=t1" in command
    assert "SPECFLO_AGENT_MANAGED=1" in command
    assert "SPECFLO_AGENT_PANE=" in command
    assert "SPECFLO_AGENT_STATE_DIR=" in command
    assert command.rstrip().endswith(" pi")

    # The discovery record carries the given name and status answers live.
    result = run_cli("status", "t1", "--json")
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    assert probe["name"] == "t1"
    assert probe["alive"] is True


def test_tui_start_timeout_cleans_up_the_pane(rig):
    run_cli, spawn_cli, serve_socket, herdr_calls, base, env = rig
    env["SPECFLO_AGENT_START_TIMEOUT"] = "1"
    result = run_cli("start", "t2", "--transport", "tui", "--cwd", ".")
    assert result.returncode == 11, result.stderr
    assert any(call[:2] == ["tab", "close"] for call in herdr_calls())


def test_tui_start_requires_herdr(rig, tmp_path):
    run_cli, spawn_cli, serve_socket, herdr_calls, base, env = rig
    env["FAKE_HERDR_DOWN"] = "1"
    result = run_cli("start", "t3", "--transport", "tui", "--cwd", ".")
    assert result.returncode != 0
    assert "herdr" in result.stderr.lower()
