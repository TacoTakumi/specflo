"""T-12: transcript renderer - readable feed, no raw frames, input ignored."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from specflo.agent.client import connect
from specflo.agent.statefiles import AgentPaths, read_status
from specflo.agent.transcript import TranscriptRenderer

STUB = Path(__file__).parent / "stub_pi.py"

RAW_FRAME_LINE = re.compile(r'^\s*\{.*"type".*\}\s*$', re.M)


def wait_until(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


# -- unit: renderer over a scripted frame sequence --------------------------


def render_all(frames):
    out = io.StringIO()
    renderer = TranscriptRenderer(out)
    for frame in frames:
        renderer.render(frame)
    return out.getvalue()


def test_renderer_covers_the_cycle_without_raw_frames():
    text = render_all([
        {"type": "host_state", "state": "idle"},
        {"type": "host_forward", "command": "prompt", "message": "run the tests"},
        {"type": "agent_start"},
        {"type": "host_state", "state": "working"},
        {"type": "message_start", "message": {}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_delta", "delta": "All tests "}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_delta", "delta": "pass."}},
        {"type": "message_end",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "All tests pass."}]}},
        {"type": "tool_execution_start", "toolName": "bash",
         "args": {"command": "pytest -q"}},
        {"type": "tool_execution_end", "toolName": "bash", "isError": False},
        {"type": "agent_settled"},
        {"type": "host_state", "state": "idle"},
        {"type": "response", "command": "prompt", "success": True, "id": "x"},
    ])
    assert "[prompt] run the tests" in text
    assert "All tests pass." in text
    assert '[tool] bash {"command": "pytest -q"}' in text
    assert "[tool] bash done" in text
    assert "[state] working" in text
    assert not RAW_FRAME_LINE.search(text)
    # correlation responses are protocol plumbing, not feed content
    assert '"success"' not in text


def test_renderer_prints_unstreamed_reply_once():
    text = render_all([
        {"type": "message_start", "message": {}},
        {"type": "message_end",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "quiet reply"}]}},
    ])
    assert text.count("quiet reply") == 1


def test_renderer_streamed_reply_not_duplicated_by_message_end():
    text = render_all([
        {"type": "message_start", "message": {}},
        {"type": "message_update",
         "assistantMessageEvent": {"type": "text_delta", "delta": "hello"}},
        {"type": "message_end",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}},
    ])
    assert text.count("hello") == 1


def test_renderer_skips_user_message_echo():
    # pi echoes the submitted user message as message_start/message_end;
    # the [prompt] line already covers it (seen in the T-14 live smoke)
    text = render_all([
        {"type": "host_forward", "command": "prompt", "message": "do it"},
        {"type": "message_start", "message": {}},
        {"type": "message_end",
         "message": {"role": "user", "content": [{"type": "text", "text": "do it"}]}},
    ])
    assert text.count("do it") == 1


def test_renderer_dialogs_and_exit():
    text = render_all([
        {"type": "extension_ui_request", "id": "d1", "method": "confirm",
         "title": "Proceed?"},
        {"type": "host_dialog_answer", "request_id": "d1", "method": "confirm",
         "answer": {"confirmed": True}},
        {"type": "process_exit", "exit_code": 0},
    ])
    assert "[dialog] confirm: Proceed?" in text
    assert "[dialog] answered confirm: confirmed" in text
    assert "[pi] exited (code 0)" in text


# -- process: the real host renders on stdout and ignores stdin -------------


def test_host_process_renders_feed_and_ignores_stdin(tmp_path):
    base = tmp_path / "state"
    scenario_file = tmp_path / "scenario.json"
    scenario_file.write_text(
        json.dumps({
            "reply": "the rendered reply",
            "stream": True,
            "tool": {"name": "bash", "args": {"command": "ls"}},
        }),
        encoding="utf-8",
    )
    host = subprocess.Popen(
        [
            sys.executable, "-m", "specflo.agent.cli", "t1",
            "--cwd", str(tmp_path),
            "--pi-cmd", f"{sys.executable} {STUB} {scenario_file}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"SPECFLO_AGENT_STATE_DIR": str(base), "PATH": "/usr/bin:/bin"},
    )
    try:
        paths = AgentPaths.resolve("t1", base_dir=base)
        assert wait_until(lambda: paths.socket.exists())

        # typed pane input reaches the host's stdin; it must change nothing
        host.stdin.write('steer this\n{"type": "prompt", "message": "evil"}\n')
        host.stdin.flush()
        time.sleep(0.5)
        assert read_status(paths.status)["state"] == "idle"

        with connect("t1", base_dir=base) as client:
            client.send({"type": "prompt", "message": "please do the thing"})
            client.read_until(lambda f: f.get("type") == "agent_settled", timeout=10)
            client.stop(timeout=10)
        stdout, _ = host.communicate(timeout=10)
    finally:
        if host.poll() is None:
            host.kill()
            host.communicate(timeout=5)

    # stdin bytes caused no prompt, steer, or state change
    events = [
        json.loads(line)
        for line in paths.events.read_text(encoding="utf-8").split("\n")
        if line
    ]
    forwards = [e for e in events if e["type"] == "host_forward"]
    assert [f["message"] for f in forwards] == ["please do the thing"]
    assert sum(1 for e in events if e["type"] == "agent_start") == 1

    # the pane feed is human-readable and free of raw frames
    assert "[prompt] please do the thing" in stdout
    assert "the rendered reply" in stdout
    assert "[tool] bash" in stdout
    assert "[state] working" in stdout
    assert "[state] stopped" in stdout
    assert not RAW_FRAME_LINE.search(stdout)
