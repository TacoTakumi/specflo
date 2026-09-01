"""'specflo agent list' across transports (pi-interactive-transport T-08).

REQ-05: one listing shows v1 broker agents and discovered TUI sessions -
managed and adopted - with transport (rpc/tui) and ownership
(managed/adopted) distinguishable per row.

REQ-18: a stale record of a killed session is never presented as attachable
(marked dead or absent), and a cleanly exited session's husk - the retained
events.jsonl with no record - is not listed at all.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.statefiles import ENV_STATE_DIR

STUB = Path(__file__).parent / "stub_pi.py"
RUNNER = Path(__file__).parent / "harness_runner.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to serve v2 sessions"
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
    base = tmp_path / "state"
    env = {**os.environ, ENV_STATE_DIR: str(base)}
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"):
        env.pop(key, None)
    procs: list[subprocess.Popen] = []
    v1_names: list[str] = []

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

    def start_v1(name: str) -> None:
        scenario = tmp_path / f"scenario-{name}.json"
        scenario.write_text(json.dumps({"reply": "v1"}), encoding="utf-8")
        result = run_cli(
            "start", name, "--cwd", ".", "--pi-cmd",
            f"{sys.executable} {STUB} {scenario}", "--no-herdr",
        )
        assert result.returncode == 0, result.stderr
        v1_names.append(name)

    def serve_v2(name: str | None, cwd: Path) -> subprocess.Popen:
        """A live v2 session: managed under ``name``, adopted when None."""
        scenario = cwd / "scenario.json"
        scenario.write_text(json.dumps({"reply": "v2"}), encoding="utf-8")
        run_env = dict(env)
        if name is not None:
            run_env["SPECFLO_AGENT_NAME"] = name
            run_env["SPECFLO_AGENT_MANAGED"] = "1"
        proc = subprocess.Popen(
            ["node", str(RUNNER), str(scenario)],
            cwd=cwd,
            env=run_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    yield run_cli, start_v1, serve_v2, base, tmp_path

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    for name in v1_names:
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


def rows_by_name(result) -> dict[str, dict]:
    assert result.returncode == 0, result.stderr
    return {row["name"]: row for row in json.loads(result.stdout)}


def test_list_distinguishes_transport_and_ownership(rig):
    run_cli, start_v1, serve_v2, base, tmp_path = rig
    start_v1("broker1")
    managed_cwd = tmp_path / "managed-proj"
    managed_cwd.mkdir()
    serve_v2("worker", managed_cwd)
    adopted_cwd = tmp_path / "adopted-proj"
    adopted_cwd.mkdir()
    serve_v2(None, adopted_cwd)
    assert wait_until(lambda: (base / "worker" / "sock").exists())
    assert wait_until(lambda: (base / "adopted-proj" / "sock").exists())

    rows = rows_by_name(run_cli("list", "--json"))
    assert set(rows) == {"broker1", "worker", "adopted-proj"}

    assert rows["broker1"]["transport"] == "rpc"
    assert rows["broker1"]["ownership"] == "managed"
    assert rows["broker1"]["alive"] is True

    assert rows["worker"]["transport"] == "tui"
    assert rows["worker"]["ownership"] == "managed"
    assert rows["worker"]["alive"] is True

    assert rows["adopted-proj"]["transport"] == "tui"
    assert rows["adopted-proj"]["ownership"] == "adopted"
    assert rows["adopted-proj"]["alive"] is True

    # The plain listing carries the same distinctions per row.
    text = run_cli("list")
    assert text.returncode == 0
    lines = {line.split()[0]: line for line in text.stdout.strip().split("\n")}
    assert "rpc" in lines["broker1"] and "managed" in lines["broker1"]
    assert "tui" in lines["worker"] and "managed" in lines["worker"]
    assert "tui" in lines["adopted-proj"] and "adopted" in lines["adopted-proj"]


def test_stale_record_never_presented_as_attachable(rig):
    run_cli, start_v1, serve_v2, base, tmp_path = rig
    # An ungracefully killed served session: socket file and record left, the
    # pid long gone.
    dead = subprocess.run(["true"])  # a pid guaranteed dead
    stale = base / "ghost"
    stale.mkdir(parents=True)
    (stale / "sock").write_text("")
    (stale / "status.json").write_text(
        json.dumps(
            {
                "name": "ghost",
                "state": "working",
                "pid": dead.args and 99999999,
                "transport": "tui",
                "ownership": "adopted",
            }
        )
    )
    rows = rows_by_name(run_cli("list", "--json"))
    assert "ghost" not in rows or (
        rows["ghost"]["state"] == "dead" and rows["ghost"]["alive"] is False
    )


def test_clean_exit_husk_is_not_listed(rig):
    run_cli, start_v1, serve_v2, base, tmp_path = rig
    husk = base / "finished"
    husk.mkdir(parents=True)
    (husk / "events.jsonl").write_text('{"ts":"t","type":"agent_start"}\n')
    result = run_cli("list", "--json")
    assert result.returncode == 0, result.stderr
    assert "finished" not in {row["name"] for row in json.loads(result.stdout)}
