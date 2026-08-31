"""Transcript renderer (REQ-12): the pane's human-readable live feed.

Turns the host's event stream into readable lines on a text stream -
submitted prompts, streamed assistant text, tool executions, dialogs, and
state changes - and never emits raw protocol frames. Purely presentational:
it inspects event *types*, not assistant content (REQ-16), and a rendering
error must never disturb the host (callers wrap render()).

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import json
from typing import Any, TextIO

_ARGS_BRIEF_LEN = 120


class TranscriptRenderer:
    def __init__(self, out: TextIO) -> None:
        self.out = out
        self._stream_open = False
        self._streamed_chars = 0

    # -- plumbing -----------------------------------------------------------

    def _line(self, text: str) -> None:
        if self._stream_open:
            self.out.write("\n")
            self._stream_open = False
        self.out.write(text + "\n")
        self.out.flush()

    def _delta(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()
        self._stream_open = True
        self._streamed_chars += len(text)

    # -- rendering ----------------------------------------------------------

    def render(self, frame: dict[str, Any]) -> None:
        ftype = frame.get("type")
        handler = getattr(self, f"_render_{ftype}", None)
        if handler is not None:
            handler(frame)

    def _render_host_state(self, frame: dict) -> None:
        self._line(f"[state] {frame.get('state')}")

    def _render_host_forward(self, frame: dict) -> None:
        self._line(f"[{frame.get('command')}] {frame.get('message', '')}")

    def _render_message_start(self, frame: dict) -> None:
        self._streamed_chars = 0

    def _render_message_update(self, frame: dict) -> None:
        event = frame.get("assistantMessageEvent") or {}
        if event.get("type") == "text_delta":
            self._delta(event.get("delta", ""))

    def _render_message_end(self, frame: dict) -> None:
        message = frame.get("message") or {}
        if self._streamed_chars == 0 and message.get("role") == "assistant":
            # nothing streamed (non-streaming provider path): print the text.
            # assistant only - pi also echoes the user message as message_end
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if block.get("type") == "text" and block.get("text"):
                        self._line(block["text"])
        elif self._stream_open:
            self.out.write("\n")
            self.out.flush()
            self._stream_open = False
        self._streamed_chars = 0

    def _render_tool_execution_start(self, frame: dict) -> None:
        args = json.dumps(frame.get("args", {}), ensure_ascii=False)
        if len(args) > _ARGS_BRIEF_LEN:
            args = args[: _ARGS_BRIEF_LEN - 3] + "..."
        self._line(f"[tool] {frame.get('toolName')} {args}")

    def _render_tool_execution_end(self, frame: dict) -> None:
        outcome = "failed" if frame.get("isError") else "done"
        self._line(f"[tool] {frame.get('toolName')} {outcome}")

    def _render_extension_ui_request(self, frame: dict) -> None:
        method = frame.get("method")
        if method in ("select", "confirm", "input", "editor"):
            self._line(f"[dialog] {method}: {frame.get('title', '')}")

    def _render_host_dialog_answer(self, frame: dict) -> None:
        answer = frame.get("answer") or {}
        if answer.get("cancelled"):
            brief = "cancelled"
        elif "confirmed" in answer:
            brief = "confirmed" if answer["confirmed"] else "declined"
        else:
            brief = str(answer.get("value", ""))[:80]
        self._line(f"[dialog] answered {frame.get('method')}: {brief}")

    def _render_host_dialog_flood(self, frame: dict) -> None:
        self._line("[dialog] flood threshold hit: auto-answering stopped")

    def _render_process_exit(self, frame: dict) -> None:
        self._line(f"[pi] exited (code {frame.get('exit_code')})")

    def _render_host_error(self, frame: dict) -> None:
        self._line(f"[error] {frame.get('error')}")
