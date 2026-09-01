"""Managed end-to-end on the rig (pi-interactive-transport T-12, REQ-17).

Live fire against real pi and real herdr, deselected by default and run with
``pytest -m rig``: a real tui start places pi (against a local stub model
provider) in a herdr pane; a controller prompts over the socket to settle
and captures the final text; a keystroke-injected message enters the same
TUI and both messages land in one transcript in order; herdr shows the
working/idle transitions the extension pushes; stop leaves no process,
socket, record, or pane registration behind.
"""

from __future__ import annotations

import http.server
import json
import shutil
import subprocess
import sys
import threading
import time
import os
from pathlib import Path

import pytest

from specflo.agent.statefiles import ENV_STATE_DIR

pytestmark = pytest.mark.rig

RIG_WORKSPACE = "specflo-rig-test"
STUB_PROVIDER = "stub"
STUB_MODEL = "stub-model"


def _herdr_server_up() -> bool:
    if shutil.which("herdr") is None:
        return False
    try:
        out = subprocess.run(
            ["herdr", "status", "server"], capture_output=True, text=True, timeout=10
        )
        return "status: running" in out.stdout
    except Exception:
        return False


requires_rig = pytest.mark.skipif(
    shutil.which("pi") is None or not _herdr_server_up(),
    reason="a real pi and a running herdr server are required",
)


def wait_until(cond, timeout=30.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class StubProviderHandler(http.server.BaseHTTPRequestHandler):
    """OpenAI-compatible SSE endpoint replaying canned turns, slowly enough
    that herdr's working state is observable mid-run."""

    turns: list[str] = []
    served = 0

    def do_POST(self):  # noqa: N802 - http.server contract
        length = int(self.headers.get("content-length", 0))
        self.rfile.read(length)
        if "chat/completions" not in (self.path or ""):
            self.send_response(404)
            self.end_headers()
            return
        cls = type(self)
        text = cls.turns[min(cls.served, len(cls.turns) - 1)]
        cls.served += 1
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

        def chunk(delta, finish):
            payload = {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": STUB_MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""}, None)
        time.sleep(1.5)  # keep the run visibly 'working'
        chunk({"content": text}, None)
        chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def rig(tmp_path):
    StubProviderHandler.turns = ["socket reply text", "typed reply text"]
    StubProviderHandler.served = 0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

    home = tmp_path / "home"
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
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
            },
            indent=2,
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
        cwd=proj,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert install.returncode == 0, install.stderr

    base = tmp_path / "state"
    env = {
        **os.environ,
        ENV_STATE_DIR: str(base),
        "SPECFLO_AGENT_SPACE": RIG_WORKSPACE,
        "SPECFLO_AGENT_START_TIMEOUT": "60",
    }
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"):
        env.pop(key, None)

    def run_cli(*args: str, timeout: float = 90) -> subprocess.CompletedProcess:
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
        return subprocess.Popen(
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

    state = {"pane": None}

    yield run_cli, spawn_cli, base, home, proj, state

    # Best-effort teardown: nothing of the test's may outlive it, including
    # the dedicated rig workspace (its label is test-owned, safe to close).
    run_cli("stop", "rigagent")
    if state["pane"]:
        tab = ":".join(state["pane"].split(":")[:1]) + ":" + state["pane"].split(":")[1].replace("p", "t")
        subprocess.run(["herdr", "tab", "close", tab], capture_output=True, timeout=10)
    try:
        listing = subprocess.run(
            ["herdr", "workspace", "list"], capture_output=True, text=True, timeout=10
        )
        for workspace in json.loads(listing.stdout)["result"]["workspaces"]:
            if workspace.get("label") == RIG_WORKSPACE:
                subprocess.run(
                    ["herdr", "workspace", "close", workspace["workspace_id"]],
                    capture_output=True,
                    timeout=10,
                )
    except Exception:
        pass
    server.shutdown()
    server.server_close()


def herdr_agent_row(name: str) -> dict | None:
    out = subprocess.run(
        ["herdr", "agent", "list"], capture_output=True, text=True, timeout=10
    )
    if out.returncode != 0:
        return None
    rows = json.loads(out.stdout)["result"]["agents"]
    for row in rows:
        if row.get("agent") == name:
            return row
    return None


def herdr_status_of(name: str) -> str | None:
    row = herdr_agent_row(name)
    return row.get("agent_status") if row else None


@requires_rig
def test_managed_end_to_end_on_the_rig(rig):
    run_cli, spawn_cli, base, home, proj, state = rig

    # A real tui start: pane created, pi runs there, socket comes connectable.
    pi_cmd = f"env HOME={home} pi --provider {STUB_PROVIDER} --model {STUB_MODEL}"
    start = run_cli(
        "start", "rigagent", "--transport", "tui", "--cwd", str(proj),
        "--pi-cmd", pi_cmd,
    )
    assert start.returncode == 0, start.stderr
    pane = start.stdout.split("pane ")[-1].strip().rstrip(")")
    state["pane"] = pane
    assert pane

    record = json.loads((base / "rigagent" / "status.json").read_text())
    pi_pid = record["pid"]
    assert record["ownership"] == "managed"

    # Controller prompt over the socket: blocks to settle, prints final text.
    prompt = spawn_cli("prompt", "rigagent", "hello from the socket")
    assert wait_until(lambda: herdr_status_of("rigagent") == "working", timeout=30), (
        "herdr never showed working during the run"
    )
    stdout, stderr = prompt.communicate(timeout=90)
    assert prompt.returncode == 0, stderr
    assert stdout == "socket reply text\n"
    # herdr renders a reported idle as agent_status "done" (probed live).
    assert wait_until(lambda: herdr_status_of("rigagent") == "done", timeout=30), (
        "herdr never returned to idle after settle"
    )

    # A human at the keyboard: keystrokes into the same TUI.
    subprocess.run(
        ["herdr", "pane", "send-text", pane, "typed at the keyboard"],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        ["herdr", "pane", "send-keys", pane, "enter"],
        check=True, capture_output=True, timeout=10,
    )
    assert wait_until(lambda: herdr_status_of("rigagent") == "working", timeout=30)
    assert run_cli("wait", "rigagent", "--timeout", "60").returncode == 0

    # One transcript, both messages, in order.
    sessions = sorted(
        (home / ".pi" / "agent" / "sessions").rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    assert sessions, "pi wrote no session file under the rig home"
    text = sessions[-1].read_text()
    first = text.find("hello from the socket")
    second = text.find("typed at the keyboard")
    assert first != -1, "socket prompt missing from the transcript"
    assert second != -1, "typed message missing from the transcript"
    assert first < second

    # Stop: process gone, socket and record gone, registration released.
    stop = run_cli("stop", "rigagent")
    assert stop.returncode == 0, stop.stderr
    assert wait_until(lambda: not _pid_alive(pi_pid), timeout=30)
    assert wait_until(lambda: not (base / "rigagent" / "sock").exists(), timeout=15)
    assert not (base / "rigagent" / "status.json").exists()
    assert wait_until(lambda: herdr_agent_row("rigagent") is None, timeout=15), (
        "herdr still lists the agent after stop"
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
