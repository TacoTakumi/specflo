"""Adoption live test on the rig (pi-interactive-transport T-13).

A hand-started pi - launched with no env handshake, the way a person opens a
session - is discovered (REQ-02): the extension binds the socket and writes
an adopted record keyed by the cwd basename. 'specflo agent list' shows the
adopted row (REQ-05); status and one prompt attach over the socket; and stop
detaches - the record goes, pi survives - with the distinct output and exit
code (REQ-06). Marked 'rig' (deselected by default), skipped unless pi is
present; herdr is not needed - the hand-started session runs pi in RPC mode,
which is just as adoptable as a TUI one.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from specflo.agent.statefiles import ENV_STATE_DIR
from test_tui_e2e import STUB_MODEL, STUB_PROVIDER, StubProviderHandler, wait_until

pytestmark = pytest.mark.rig

requires_pi = pytest.mark.skipif(
    shutil.which("pi") is None, reason="a real pi is required"
)


@pytest.fixture
def rig(tmp_path):
    StubProviderHandler.turns = ["adopted reply text"]
    StubProviderHandler.served = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

    home = tmp_path / "home"
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True)
    scratch = tmp_path / "scratch-proj"
    scratch.mkdir()
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    STUB_PROVIDER: {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "no-key",
                        "authHeader": False,
                        "models": [
                            {
                                "id": STUB_MODEL,
                                "name": "specflo rig stub",
                                "contextWindow": 100000,
                                "maxTokens": 4096,
                                "cost": {
                                    "input": 0,
                                    "output": 0,
                                    "cacheRead": 0,
                                    "cacheWrite": 0,
                                },
                            }
                        ],
                    }
                }
            }
        )
    )
    (agent_dir / "settings.json").write_text(json.dumps({"packages": []}))
    install = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from specflo.cli import main; sys.exit(main())",
            "extension",
            "install",
        ],
        cwd=scratch,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert install.returncode == 0, install.stderr

    base = tmp_path / "state"
    env = {**os.environ, ENV_STATE_DIR: str(base), "HOME": str(home)}
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED", "SPECFLO_AGENT_PANE"):
        env.pop(key, None)

    def run_cli(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
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

    procs: list[subprocess.Popen] = []

    def hand_start_pi() -> subprocess.Popen:
        """pi started the way a person starts it: no handshake at all."""
        proc = subprocess.Popen(
            [
                "pi",
                "--mode",
                "rpc",
                "--provider",
                STUB_PROVIDER,
                "--model",
                STUB_MODEL,
                "--session-dir",
                str(tmp_path / "sessions"),
            ],
            cwd=scratch,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        return proc

    yield run_cli, hand_start_pi, base, scratch

    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
    server.shutdown()
    server.server_close()


@requires_pi
def test_adoption_discover_attach_detach(rig):
    run_cli, hand_start_pi, base, scratch = rig
    name = scratch.name

    pi = hand_start_pi()
    record = base / name / "status.json"
    assert wait_until(lambda: record.exists(), timeout=30), "no discovery record"
    snapshot = json.loads(record.read_text())
    assert snapshot["ownership"] == "adopted"
    assert snapshot["transport"] == "tui"
    assert snapshot["cwd"] == str(scratch)
    assert snapshot["pid"] == pi.pid

    # Discovery shows the adopted row.
    listing = run_cli("list", "--json")
    assert listing.returncode == 0, listing.stderr
    rows = {row["name"]: row for row in json.loads(listing.stdout)}
    assert rows[name]["ownership"] == "adopted"
    assert rows[name]["transport"] == "tui"
    assert rows[name]["alive"] is True

    # Attach: status over the socket, then one settle-blocked prompt.
    status = run_cli("status", name, "--json")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["alive"] is True

    prompt = run_cli("prompt", name, "hello adopted session")
    assert prompt.returncode == 0, prompt.stderr
    assert prompt.stdout == "adopted reply text\n"

    # Stop is a detach here: record gone, pi alive, distinct output and code.
    stop = run_cli("stop", name)
    assert stop.returncode == 13, stop.stderr
    assert "detach" in stop.stdout.lower()
    assert not record.exists()
    assert not (base / name / "sock").exists()
    time.sleep(0.3)
    assert pi.poll() is None, "an adopted session must survive stop"

    # And a clean pi exit removes nothing it no longer has - but the retained
    # event log stays (REQ-02's cleanup already happened at detach).
    assert (base / name / "events.jsonl").exists()
