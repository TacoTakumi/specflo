"""T-01: JSONL framing (incl. the U+2028/U+2029 trap) and the scriptable stub pi."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from specflo.agent.protocol import (
    FrameDecoder,
    decode_frame,
    encode_frame,
)

STUB = Path(__file__).parent / "stub_pi.py"


# -- framing ----------------------------------------------------------------


def test_roundtrip_single_frame():
    obj = {"id": "req-1", "type": "prompt", "message": "hello\nworld é"}
    data = encode_frame(obj)
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1  # payload never contains a raw LF
    assert decode_frame(data) == obj


def test_u2028_u2029_do_not_split_frames():
    text = "line\u2028separator\u2029paragraph"
    obj = {"type": "message_end", "text": text}
    data = encode_frame(obj)
    # ensure_ascii=False mirrors pi's JSON.stringify: the characters are raw
    assert "\u2028".encode("utf-8") in data
    assert "\u2029".encode("utf-8") in data
    assert data.count(b"\n") == 1

    decoder = FrameDecoder()
    frames = []
    for i in range(len(data)):  # feed byte-at-a-time across every boundary
        frames.extend(decoder.feed(data[i : i + 1]))
    assert frames == [obj]
    assert frames[0]["text"] == text


def test_decoder_accepts_raw_u2028_input():
    # what a JS peer would emit: raw U+2028 inside a string, LF-terminated
    raw = '{"text":"a\u2028b"}\n'.encode("utf-8")
    frames = FrameDecoder().feed(raw)
    assert frames == [{"text": "a\u2028b"}]


def test_multiple_frames_and_crlf_in_one_chunk():
    data = b'{"a":1}\r\n{"b":2}\n\n{"c":3}\n'
    frames = FrameDecoder().feed(data)
    assert frames == [{"a": 1}, {"b": 2}, {"c": 3}]


def test_partial_frame_buffers_across_feeds():
    decoder = FrameDecoder()
    assert decoder.feed(b'{"a"') == []
    assert decoder.feed(b":1}\n{") == [{"a": 1}]
    assert decoder.feed(b'"b":2}\n') == [{"b": 2}]


# -- stub pi ----------------------------------------------------------------


class StubClient:
    """Spawn the stub pi over a scenario and read its frames via a queue."""

    def __init__(self, scenario: dict, tmp_path: Path):
        scenario_file = tmp_path / "scenario.json"
        scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, str(STUB), str(scenario_file)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        decoder = FrameDecoder()
        while True:
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                break
            for frame in decoder.feed(chunk):
                self.q.put(frame)
        self.q.put({"type": "_stub_eof"})

    def send(self, obj):
        self.proc.stdin.write(encode_frame(obj))
        self.proc.stdin.flush()

    def next_frame(self, timeout: float = 10.0):
        return self.q.get(timeout=timeout)

    def collect_until(self, frame_type: str, timeout: float = 10.0):
        frames = []
        while True:
            frame = self.next_frame(timeout)
            assert frame["type"] != "_stub_eof", f"stub exited early; got {frames}"
            frames.append(frame)
            if frame["type"] == frame_type:
                return frames

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()


@pytest.fixture
def stub(tmp_path):
    clients = []

    def make(scenario: dict) -> StubClient:
        client = StubClient(scenario, tmp_path)
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def message_text(message: dict) -> str:
    return "".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )


def test_stub_prompt_cycle_settles_with_known_reply(stub):
    reply = "known reply\u2028with a line separator"
    client = stub({"reply": reply, "stream": True})
    client.send({"id": "p1", "type": "prompt", "message": "hi"})
    frames = client.collect_until("agent_settled")

    response = frames[0]
    assert response == {
        "type": "response",
        "command": "prompt",
        "success": True,
        "id": "p1",
    }
    types = [f["type"] for f in frames]
    for earlier, later in [
        ("response", "agent_start"),
        ("agent_start", "turn_start"),
        ("turn_start", "message_end"),
        ("message_end", "turn_end"),
        ("turn_end", "agent_settled"),
    ]:
        assert types.index(earlier) < types.index(later), types

    deltas = "".join(
        f["assistantMessageEvent"]["delta"]
        for f in frames
        if f["type"] == "message_update"
        and f["assistantMessageEvent"]["type"] == "text_delta"
    )
    assert deltas == reply
    message_end = next(f for f in frames if f["type"] == "message_end")
    turn_end = next(f for f in frames if f["type"] == "turn_end")
    assert message_text(message_end["message"]) == reply
    assert message_text(turn_end["message"]) == reply


def test_stub_dialog_scenario_waits_for_answer(stub, tmp_path):
    capture = tmp_path / "capture.jsonl"
    client = stub(
        {
            "mode": "dialog",
            "reply": "after dialog",
            "dialogs": [{"method": "confirm", "title": "Push?"}],
            "capture": str(capture),
        }
    )
    client.send({"id": "p1", "type": "prompt", "message": "go"})
    frames = client.collect_until("extension_ui_request")
    request = frames[-1]
    assert request["method"] == "confirm"
    assert request["title"] == "Push?"

    client.send({"type": "extension_ui_response", "id": request["id"], "confirmed": True})
    frames = client.collect_until("agent_settled")
    turn_end = next(f for f in frames if f["type"] == "turn_end")
    assert message_text(turn_end["message"]) == "after dialog"

    captured = [
        json.loads(line)
        for line in capture.read_text(encoding="utf-8").split("\n")
        if line
    ]
    assert {"type": "extension_ui_response", "id": request["id"], "confirmed": True} in captured


def test_stub_never_settle_until_abort(stub):
    client = stub({"mode": "never_settle"})
    client.send({"id": "p1", "type": "prompt", "message": "go"})
    frames = client.collect_until("turn_start")
    assert [f["type"] for f in frames] == ["response", "agent_start", "turn_start"]
    with pytest.raises(queue.Empty):
        client.next_frame(timeout=0.5)

    client.send({"id": "a1", "type": "abort"})
    frames = client.collect_until("agent_settled")
    assert frames[0] == {"type": "response", "command": "abort", "success": True, "id": "a1"}
    turn_end = next(f for f in frames if f["type"] == "turn_end")
    assert turn_end["message"]["stopReason"] == "aborted"


def test_stub_exit_scenario_dies_mid_run(stub):
    client = stub({"mode": "exit", "exit_code": 3})
    client.send({"id": "p1", "type": "prompt", "message": "go"})
    frames = client.collect_until("turn_start")
    assert [f["type"] for f in frames] == ["response", "agent_start", "turn_start"]
    assert client.next_frame(timeout=10)["type"] == "_stub_eof"
    assert client.proc.wait(timeout=10) == 3
