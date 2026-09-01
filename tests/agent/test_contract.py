"""The shared socket contract, one suite over both transports (T-06, REQ-09/REQ-10).

Every test runs twice via ``contract_rig``: once against the v1 broker host
(stub pi), once against the v2 control module served by the Node harness
runner. The bodies drive only the unmodified Python client and CLI verbs -
status, prompt to settle with the final text, the busy refusal, steer
delivery, wait, last, log, and the distinct exit codes - so a contract
regression in either transport fails the same test.

The rig lives here rather than in a tests/agent/conftest.py because the test
tree is packageless (rootdir import mode): a second module named ``conftest``
shadows tests/conftest.py for the suites that do ``from conftest import ...``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from specflo.agent.client import HostUnreachableError, connect
from specflo.agent.statefiles import ENV_STATE_DIR

STUB = Path(__file__).parent / "stub_pi.py"
RUNNER = Path(__file__).parent / "harness_runner.mjs"


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def _connectable(name: str, base: Path) -> bool:
    try:
        with connect(name, base_dir=base, connect_timeout=1.0):
            return True
    except HostUnreachableError:
        return False


@dataclass
class ContractRig:
    """One served agent behind either transport, driven via the real CLI."""

    transport: str
    base: Path
    tmp_path: Path
    env: dict
    _v2_procs: list = field(default_factory=list)
    _v1_names: list = field(default_factory=list)

    def run_cli(self, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
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
            env=self.env,
            timeout=timeout,
        )

    def start(self, name: str, scenario: dict) -> Path:
        """Serve one agent under ``name``; returns the delivery-capture path."""
        capture = self.tmp_path / f"capture-{name}.jsonl"
        scenario = {**scenario, "capture": str(capture)}
        scenario_file = self.tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        if self.transport == "v1":
            result = self.run_cli(
                "start",
                name,
                "--cwd",
                ".",
                "--pi-cmd",
                f"{sys.executable} {STUB} {scenario_file}",
                "--no-herdr",
            )
            assert result.returncode == 0, result.stderr
            self._v1_names.append(name)
        else:
            proc = subprocess.Popen(
                ["node", str(RUNNER), str(scenario_file)],
                cwd=self.tmp_path,
                env={
                    **self.env,
                    "SPECFLO_AGENT_NAME": name,
                    "SPECFLO_AGENT_MANAGED": "1",
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._v2_procs.append(proc)
            if not wait_until(lambda: _connectable(name, self.base)):
                proc.terminate()
                raise AssertionError(
                    f"runner never served: {proc.stderr.read() if proc.stderr else ''}"
                )
        return capture

    def stop_serving(self, name: str) -> None:
        """Make the agent unreachable, each transport's own way."""
        if self.transport == "v1":
            self.run_cli("stop", name)
        else:
            for proc in self._v2_procs:
                proc.terminate()
                proc.wait(timeout=10)
        assert wait_until(lambda: not _connectable(name, self.base))

    def cleanup(self) -> None:
        for proc in self._v2_procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        for name in self._v1_names:
            status_file = self.base / name / "status.json"
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


@pytest.fixture(params=["v1", "v2"])
def contract_rig(request, tmp_path):
    if request.param == "v2" and shutil.which("node") is None:
        pytest.skip("node is required to serve the v2 control module")
    base = tmp_path / "state"
    env = {**os.environ, ENV_STATE_DIR: str(base)}
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"):
        env.pop(key, None)
    rig = ContractRig(transport=request.param, base=base, tmp_path=tmp_path, env=env)
    yield rig
    rig.cleanup()


def agent_state(rig, name: str) -> str:
    return json.loads((rig.base / name / "status.json").read_text())["state"]


def captured_prompts(capture) -> list[dict]:
    if not capture.exists():
        return []
    return [
        json.loads(line)
        for line in capture.read_text(encoding="utf-8").split("\n")
        if line
    ]


def test_contract_status_answers_live(contract_rig):
    contract_rig.start("c1", {"reply": "hi"})
    result = contract_rig.run_cli("status", "c1", "--json")
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    assert probe["name"] == "c1"
    assert probe["alive"] is True
    assert probe["state"] == "idle"


def test_contract_blocking_prompt_prints_final_text(contract_rig):
    contract_rig.start("c1", {"reply": "the known reply"})
    result = contract_rig.run_cli("prompt", "c1", "hello")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "the known reply\n"


def test_contract_busy_refusal_delivers_nothing(contract_rig):
    capture = contract_rig.start("c1", {"mode": "never_settle"})
    first = contract_rig.run_cli("prompt", "c1", "first", "--no-wait")
    assert first.returncode == 0, first.stderr
    assert wait_until(lambda: agent_state(contract_rig, "c1") == "working")
    second = contract_rig.run_cli("prompt", "c1", "second")
    assert second.returncode == 10
    messages = [frame["message"] for frame in captured_prompts(capture)]
    assert messages == ["first"]


def test_contract_steer_delivers_mid_run(contract_rig):
    capture = contract_rig.start("c1", {"mode": "never_settle"})
    assert contract_rig.run_cli("prompt", "c1", "first", "--no-wait").returncode == 0
    assert wait_until(lambda: agent_state(contract_rig, "c1") == "working")
    steered = contract_rig.run_cli("prompt", "c1", "adjust course", "--steer", "--no-wait")
    assert steered.returncode == 0, steered.stderr
    assert wait_until(
        lambda: any(
            frame.get("streamingBehavior") == "steer" and frame["message"] == "adjust course"
            for frame in captured_prompts(capture)
        )
    )


def test_contract_wait_returns_at_settle_and_times_out(contract_rig):
    contract_rig.start("c1", {"reply": "done"})
    # Idle agent: wait returns immediately with 0.
    assert contract_rig.run_cli("wait", "c1").returncode == 0
    contract_rig.start("c2", {"mode": "never_settle"})
    assert contract_rig.run_cli("prompt", "c2", "go", "--no-wait").returncode == 0
    assert wait_until(lambda: agent_state(contract_rig, "c2") == "working")
    result = contract_rig.run_cli("wait", "c2", "--timeout", "1")
    assert result.returncode == 11


def test_contract_prompt_timeout_exits_11(contract_rig):
    contract_rig.start("c1", {"mode": "never_settle"})
    result = contract_rig.run_cli("prompt", "c1", "hello", "--timeout", "1")
    assert result.returncode == 11


def test_contract_last_prints_the_final_text(contract_rig):
    contract_rig.start("c1", {"reply": "remember me"})
    assert contract_rig.run_cli("prompt", "c1", "hello").returncode == 0
    result = contract_rig.run_cli("last", "c1")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "remember me\n"


def test_contract_log_carries_the_run_events(contract_rig):
    contract_rig.start("c1", {"reply": "logged"})
    assert contract_rig.run_cli("prompt", "c1", "hello").returncode == 0
    result = contract_rig.run_cli("log", "c1")
    assert result.returncode == 0, result.stderr
    types = [json.loads(line)["type"] for line in result.stdout.splitlines() if line]
    assert "agent_start" in types
    assert "agent_settled" in types
    assert types.index("agent_start") < types.index("agent_settled")


def test_contract_unreachable_exits_12(contract_rig):
    contract_rig.start("c1", {"reply": "gone soon"})
    contract_rig.stop_serving("c1")
    result = contract_rig.run_cli("prompt", "c1", "anyone there")
    assert result.returncode == 12
