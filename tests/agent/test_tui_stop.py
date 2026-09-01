"""Ownership-aware stop (pi-interactive-transport T-09, REQ-06).

Managed tui agent: stop SIGTERMs the recorded pi pid - the extension's own
shutdown removes socket and record - and releases the pane registration.
Adopted session: stop never signals the process; it removes specflo's record,
says so distinctly, and exits with the distinct detached code. v1 stop stays
untouched (its own suite runs beside this one).
"""

from __future__ import annotations

import json
import os
import shutil
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

    def serve_v2(
        name: str | None, cwd: Path, extra_env: dict | None = None
    ) -> subprocess.Popen:
        scenario = cwd / "scenario.json"
        scenario.write_text(json.dumps({"reply": "v2"}), encoding="utf-8")
        run_env = dict(env)
        if name is not None:
            run_env["SPECFLO_AGENT_NAME"] = name
            run_env["SPECFLO_AGENT_MANAGED"] = "1"
        if extra_env:
            run_env.update(extra_env)
        proc = subprocess.Popen(
            ["node", str(RUNNER), str(scenario)],
            cwd=cwd,
            env=run_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(proc)
        return proc

    def herdr_calls() -> list[list[str]]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().split("\n") if line]

    yield run_cli, serve_v2, herdr_calls, base, tmp_path

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_managed_stop_ends_pi_and_cleans_up(rig):
    run_cli, serve_v2, herdr_calls, base, tmp_path = rig
    cwd = tmp_path / "managed-proj"
    cwd.mkdir()
    proc = serve_v2("worker", cwd)
    assert wait_until(lambda: (base / "worker" / "status.json").exists())

    result = run_cli("stop", "worker")
    assert result.returncode == 0, result.stderr
    assert "stopped" in result.stdout
    assert wait_until(lambda: proc.poll() is not None), "pi process must be gone"
    assert not (base / "worker" / "sock").exists()
    assert not (base / "worker" / "status.json").exists()


def test_managed_stop_releases_the_recorded_pane(rig):
    run_cli, serve_v2, herdr_calls, base, tmp_path = rig
    # A managed record whose session died ungracefully, pane registration
    # still held: stop cleans the record and releases the pane.
    dead = subprocess.Popen(["true"])
    dead.wait()
    record = base / "worker"
    record.mkdir(parents=True)
    (record / "sock").write_text("")
    (record / "status.json").write_text(
        json.dumps(
            {
                "name": "worker",
                "state": "working",
                "pid": dead.pid,
                "transport": "tui",
                "ownership": "managed",
                "herdr_pane": "w1:p9",
            }
        )
    )
    result = run_cli("stop", "worker")
    assert result.returncode == 0, result.stderr
    assert not (record / "sock").exists()
    assert not (record / "status.json").exists()
    releases = [call for call in herdr_calls() if call[:2] == ["pane", "release-agent"]]
    assert releases
    # The release must use the source the extension registered under, or
    # herdr refuses the authority change (review round 1, finding 2).
    for call in releases:
        assert call[call.index("--source") + 1] == "specflo-pi-extension"


def test_managed_stop_releases_a_live_sessions_recorded_pane(rig):
    # The live path of the same finding: a real served session records its
    # handshake pane, so stop can release it without any fabricated record.
    run_cli, serve_v2, herdr_calls, base, tmp_path = rig
    cwd = tmp_path / "live-managed"
    cwd.mkdir()
    fake_herdr_bin = tmp_path / "bin" / "herdr"
    proc = serve_v2("worker", cwd, extra_env={
        "SPECFLO_AGENT_PANE": "w9:p3",
        "SPECFLO_HERDR_BIN": str(fake_herdr_bin),
    })
    assert wait_until(lambda: (base / "worker" / "status.json").exists())
    snapshot = json.loads((base / "worker" / "status.json").read_text())
    assert snapshot["herdr_pane"] == "w9:p3"

    result = run_cli("stop", "worker")
    assert result.returncode == 0, result.stderr
    assert wait_until(lambda: proc.poll() is not None)
    assert wait_until(
        lambda: any(
            call[:2] == ["pane", "release-agent"] and call[2] == "w9:p3"
            for call in herdr_calls()
        )
    )


def test_release_source_matches_the_extension_constant():
    # Cross-language drift pin: the Python release source must be the very
    # string the extension registers panes under.
    from specflo.agent.tui import EXTENSION_HERDR_SOURCE

    herdr_ts = (
        Path(__file__).resolve().parents[2]
        / "src" / "specflo" / "extension" / "src" / "control" / "herdr.ts"
    )
    assert f'"{EXTENSION_HERDR_SOURCE}"' in herdr_ts.read_text()


def test_adopted_stop_detaches_without_killing(rig):
    run_cli, serve_v2, herdr_calls, base, tmp_path = rig
    cwd = tmp_path / "adopted-proj"
    cwd.mkdir()
    proc = serve_v2(None, cwd)
    assert wait_until(lambda: (base / "adopted-proj" / "status.json").exists())

    result = run_cli("stop", "adopted-proj")
    assert result.returncode == 13, result.stderr
    assert "detach" in result.stdout.lower()
    assert "stopped" not in result.stdout
    time.sleep(0.3)
    assert proc.poll() is None, "an adopted session must never be killed"
    assert not (base / "adopted-proj" / "status.json").exists()
    # The retained event log is not specflo's registration; it stays.
    assert (base / "adopted-proj" / "events.jsonl").exists()
