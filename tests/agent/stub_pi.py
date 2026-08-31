#!/usr/bin/env python3
"""Scriptable stub pi: speaks the pi RPC protocol on stdio, driven by a scenario file.

Usage: python stub_pi.py <scenario.json>

Scenario keys:
  mode      "reply" (default) - answer a prompt with a settled run
            "dialog"          - emit each entry of "dialogs" as an
                                extension_ui_request, wait for the matching
                                extension_ui_response, then reply and settle
            "never_settle"    - start the run, then emit nothing more
                                (abort settles it)
            "exit"            - start the run, then exit with "exit_code"
  reply     assistant text for a settled run (default "stub reply")
  stream    bool - emit message_update text_delta chunks before message_end
  dialogs   list of extension_ui_request templates, e.g.
            {"method": "confirm", "title": "Push?"} - "id" is generated
  exit_code process exit code for mode "exit" (default 1)
  capture   path - append every frame received on stdin, one JSON line each

The stub is the spec-sanctioned test double for pi (spec In scope); it mirrors
the real event shapes from pi docs/rpc.md: a correlated "response" per command,
then agent_start / turn_start / message_* / turn_end / agent_settled.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterator

from specflo.agent.protocol import encode_frame, read_frames


def emit(obj: Any) -> None:
    sys.stdout.buffer.write(encode_frame(obj))
    sys.stdout.buffer.flush()


def assistant_message(text: str, stop_reason: str = "stop") -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": stop_reason,
    }


def respond(cmd: dict, command: str, success: bool = True, **extra: Any) -> None:
    resp: dict = {"type": "response", "command": command, "success": success}
    if "id" in cmd:
        resp["id"] = cmd["id"]
    resp.update(extra)
    emit(resp)


class Stub:
    def __init__(self, scenario: dict) -> None:
        self.scenario = scenario
        self.mode = scenario.get("mode", "reply")
        self.reply = scenario.get("reply", "stub reply")
        self.capture_path = scenario.get("capture")
        self.frames: Iterator[Any] = self._captured_frames()
        self.run_open = False
        self.last_text: str | None = None

    def _captured_frames(self) -> Iterator[Any]:
        for frame in read_frames(sys.stdin.buffer):
            if self.capture_path:
                with open(self.capture_path, "ab") as f:
                    f.write(encode_frame(frame))
            yield frame

    # -- run pieces ---------------------------------------------------------

    def start_run(self) -> None:
        self.run_open = True
        emit({"type": "agent_start"})
        emit({"type": "turn_start"})

    def settle_run(self, text: str, stop_reason: str = "stop") -> None:
        if stop_reason == "stop":
            self.last_text = text
        message = assistant_message(text, stop_reason)
        emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
        if self.scenario.get("stream"):
            emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start", "contentIndex": 0}})
            mid = max(1, len(text) // 2)
            for delta in (text[:mid], text[mid:]):
                if delta:
                    emit({
                        "type": "message_update",
                        "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": delta},
                    })
            emit({
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_end", "contentIndex": 0, "content": text},
            })
        emit({"type": "message_end", "message": message})
        emit({"type": "turn_end", "message": message, "toolResults": []})
        emit({"type": "agent_settled"})
        self.run_open = False

    def run_dialogs(self) -> None:
        for n, template in enumerate(self.scenario.get("dialogs", []), start=1):
            request = {"type": "extension_ui_request", "id": f"dlg-{n}", **template}
            emit(request)
            if template.get("method") not in ("select", "confirm", "input", "editor"):
                continue  # fire-and-forget
            for frame in self.frames:
                if (
                    frame.get("type") == "extension_ui_response"
                    and frame.get("id") == request["id"]
                ):
                    break
                self.handle_out_of_band(frame)

    def handle_out_of_band(self, cmd: dict) -> None:
        """Commands arriving while a run is blocked on a dialog."""
        if cmd.get("type") == "abort":
            respond(cmd, "abort")
            if self.run_open:
                self.settle_run("", stop_reason="aborted")
        else:
            respond(cmd, str(cmd.get("type")), success=False, error="stub: busy")

    # -- command loop -------------------------------------------------------

    def handle_prompt(self, cmd: dict) -> None:
        if self.run_open:
            # mirror real pi: a prompt during streaming needs streamingBehavior
            if cmd.get("streamingBehavior") in ("steer", "followUp"):
                respond(cmd, "prompt")  # queued; the open run continues
            else:
                respond(
                    cmd,
                    "prompt",
                    success=False,
                    error="agent is streaming; specify streamingBehavior",
                )
            return
        respond(cmd, "prompt")
        self.start_run()
        if self.mode == "exit":
            sys.exit(int(self.scenario.get("exit_code", 1)))
        if self.mode == "never_settle":
            return
        if self.mode == "dialog":
            self.run_dialogs()
        self.settle_run(self.reply)

    def main(self) -> None:
        for cmd in self.frames:
            ctype = cmd.get("type")
            if ctype == "prompt":
                self.handle_prompt(cmd)
            elif ctype == "abort":
                respond(cmd, "abort")
                if self.run_open:
                    self.settle_run("", stop_reason="aborted")
            elif ctype == "get_last_assistant_text":
                respond(
                    cmd,
                    "get_last_assistant_text",
                    data={"text": self.last_text},
                )
            elif ctype == "extension_ui_response":
                pass  # stale/unsolicited; ignore like pi does
            else:
                respond(cmd, str(ctype), success=False, error="stub: unhandled command")


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as f:
        scenario = json.load(f)
    Stub(scenario).main()


if __name__ == "__main__":
    main()
