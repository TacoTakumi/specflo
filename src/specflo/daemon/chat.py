"""The chat pump: one socket subscription per live agent, filling the project's chat log.

The daemon does not run an agent's pi; it follows the agent's control
socket like any other client and writes what the session reports into the
project's transcript. Each live agent gets one pump thread that connects,
reads frames until the socket drops, and connects again, so a restarted
agent or a blip in the socket loses nothing after the reconnect. A pump
ends on its own when the project no longer maps to its agent.

What lands in the log, per frame the extension mirrors or broadcasts:

- a ``message_start`` with a user message is one user entry, its author the
  seat the text names itself with, the developer's pane when it names none;
- ``message_update`` text deltas assemble one assistant message that lands
  as one entry at ``message_end``;
- ``agent_start``, ``agent_settled``, ``ui_prompt_start`` and
  ``ui_prompt_end`` are state entries: working, idle, needs-attention with
  the prompt's kind, and back to the run's state when the prompt closes.

The pumps of one daemon process live in one registry the app holds; on
daemon start it resumes a pump for every mapped agent discovery finds alive.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..agent.client import HostUnreachableError, connect
from . import chatlog, seat

# How long a connect waits before the pump counts the socket as down.
CONNECT_TIMEOUT = 5.0
# How long a read blocks before the pump looks at its stop flag again.
READ_POLL = 0.5
# How long a pump waits before it tries the socket again.
RECONNECT_DELAY = 0.5

USER_KIND = "user"
ASSISTANT_KIND = "assistant"
STATE_KIND = "state"
WORKING = "working"
IDLE = "idle"
NEEDS_ATTENTION = "needs-attention"


def message_text(message: object) -> str:
    """The text of a pi message: its string content, or its text blocks joined."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def post_message(root: Path, slug: str, author: str, text: str) -> None:
    """Deliver ``text`` from ``author`` to the project's agent as a prompt.

    The text goes out prefixed with the author's label, so the session
    reports it back with its seat and the pump attributes it. Nothing is
    written to the log here: the entry lands when the agent reports the
    message, like a line typed in the pane. An agent mid-run gets the
    message with steering behaviour; the call returns once the agent has
    taken the prompt, not when it settles. A project with no agent on
    record, or one that does not serve, raises.
    """
    name = seat.agent_for(root, slug)
    if name is None:
        raise seat.AgentMessageError(f"Project {slug!r} has no agent to take the message.")
    seat.send_message(name, chatlog.label(author, text))


class Pump:
    """One agent's subscription: a thread that follows the socket into the log."""

    def __init__(self, root: Path, slug: str, name: str) -> None:
        self.root = Path(root)
        self.slug = slug
        self.name = name
        self.log = chatlog.open_log(root, slug)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"chat-pump-{name}", daemon=True)
        # What the assistant has said so far in the message being streamed.
        self._assembling: list[str] = []
        self._running = False
        self.connections = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=CONNECT_TIMEOUT + READ_POLL)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _mapped(self) -> bool:
        """Whether the project still maps to this pump's agent."""
        return seat.agent_for(self.root, self.slug) == self.name

    def _run(self) -> None:
        while not self._stop.is_set() and self._mapped():
            try:
                with connect(self.name, connect_timeout=CONNECT_TIMEOUT) as client:
                    self.connections += 1
                    while not self._stop.is_set():
                        try:
                            frame = client.read(timeout=READ_POLL)
                        except TimeoutError:
                            continue
                        self.handle(frame)
            except (HostUnreachableError, OSError, RuntimeError, ValueError):
                pass
            self._stop.wait(RECONNECT_DELAY)

    def handle(self, frame: object) -> None:
        """Log what one frame from the socket means for the transcript."""
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "message_start":
            message = frame.get("message")
            role = message.get("role") if isinstance(message, dict) else None
            if role == "user":
                author, text = chatlog.split_label(message_text(message), seat.DEVELOPER_SEAT)
                self.log.append(USER_KIND, author, text)
            elif role == "assistant":
                self._assembling = []
        elif kind == "message_update":
            event = frame.get("assistantMessageEvent")
            if isinstance(event, dict) and event.get("type") == "text_delta":
                self._assembling.append(str(event.get("delta", "")))
        elif kind == "message_end":
            message = frame.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = "".join(self._assembling) or message_text(message)
                self._assembling = []
                if text:
                    self.log.append(ASSISTANT_KIND, self.name, text)
        elif kind == "agent_start":
            self._running = True
            self.log.append(STATE_KIND, self.name, WORKING)
        elif kind == "agent_settled":
            self._running = False
            self.log.append(STATE_KIND, self.name, IDLE)
        elif kind == "ui_prompt_start":
            prompt_kind = str(frame.get("kind") or frame.get("reason") or "prompt")
            self.log.append(STATE_KIND, self.name, f"{NEEDS_ATTENTION}: {prompt_kind}")
        elif kind == "ui_prompt_end":
            self.log.append(STATE_KIND, self.name, WORKING if self._running else IDLE)


class Pumps:
    """The pumps one daemon process runs, one per project at most."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._pumps: dict[str, Pump] = {}
        self._lock = threading.Lock()

    def ensure(self, slug: str, name: str) -> Pump:
        """The running pump for ``slug`` on agent ``name``, started if there is none."""
        with self._lock:
            pump = self._pumps.get(slug)
            if pump is not None and pump.alive and pump.name == name:
                return pump
            if pump is not None:
                pump.stop()
            pump = self._pumps[slug] = Pump(self.root, slug, name)
            pump.start()
            return pump

    def stop(self, slug: str) -> bool:
        """Stop the pump for ``slug``; True when there was one."""
        with self._lock:
            pump = self._pumps.pop(slug, None)
        if pump is None:
            return False
        pump.stop()
        return True

    def stop_all(self) -> None:
        with self._lock:
            pumps = list(self._pumps.values())
            self._pumps.clear()
        for pump in pumps:
            pump.stop()

    def running(self) -> dict[str, str]:
        """Every pumped project's agent name by slug, live threads only."""
        with self._lock:
            return {slug: pump.name for slug, pump in sorted(self._pumps.items()) if pump.alive}

    def resume(self) -> dict[str, str]:
        """Start a pump for every mapped agent discovery finds alive; what was started."""
        started = {}
        for slug, name in seat.agent_mapping(self.root).items():
            if seat.liveness(self.root, slug).alive:
                self.ensure(slug, name)
                started[slug] = name
        return started
