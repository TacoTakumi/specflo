"""The chat pump: the daemon follows each live agent's socket into the project's chat log.

A fake agent serves a control socket where the real host would: it answers
a status request so discovery finds it alive, and pushes whatever frames a
test emits to every connected client, the way the extension broadcasts.
"""

import json
import socket
import threading
import time

import pytest

from specflo import daemon
from specflo.agent.protocol import encode_frame, FrameDecoder
from specflo.agent.statefiles import ENV_STATE_DIR, AgentPaths
from specflo.daemon import chat, chatlog, seat
from specflo.daemon.app import create_app


def wait_until(cond, timeout=10.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


class FakeAgent:
    """A control socket for agent ``name`` under ``base``: status answered, frames pushed."""

    def __init__(self, base, name, state="idle"):
        self.paths = AgentPaths.resolve(name, base)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.paths.socket))
        self.server.listen()
        self.clients = []
        self._lock = threading.Lock()
        self._closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self._lock:
                self.clients.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        decoder = FrameDecoder()
        while not self._closed.is_set():
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                break
            for frame in decoder.feed(chunk):
                if frame.get("type") == "status":
                    self._send(conn, {
                        "type": "response", "id": frame.get("id"), "command": "status", "success": True,
                        "data": {"status": {"state": self.state, "transport": "rpc"}, "paths": {}},
                    })
        with self._lock:
            if conn in self.clients:
                self.clients.remove(conn)
        conn.close()

    def _send(self, conn, frame):
        try:
            conn.sendall(encode_frame(frame))
        except OSError:
            pass

    @property
    def connected(self):
        with self._lock:
            return len(self.clients)

    def emit(self, frame):
        with self._lock:
            clients = list(self.clients)
        for conn in clients:
            self._send(conn, frame)

    def drop_clients(self):
        """Close every client's connection the way a dying host does: FIN first."""
        with self._lock:
            clients, self.clients = self.clients, []
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def close(self):
        self._closed.set()
        self.drop_clients()
        self.server.close()
        self.paths.socket.unlink(missing_ok=True)


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def base(tmp_path, monkeypatch):
    base = tmp_path / "state"
    monkeypatch.setenv(ENV_STATE_DIR, str(base))
    return base


@pytest.fixture
def agent(root, base):
    name = seat.agent_name("login-fix")
    seat.record_agent(root, "login-fix", name)
    fake = FakeAgent(base, name)
    yield fake
    fake.close()


@pytest.fixture
def pump(root, agent):
    pump = chat.Pump(root, "login-fix", agent.paths.name)
    pump.start()
    assert wait_until(lambda: agent.connected == 1)
    yield pump
    pump.stop()


def entries(root, slug="login-fix"):
    return [(e.kind, e.author, e.text) for e in chatlog.open_log(root, slug).read_from(0)]


def user_message(text):
    return {"type": "message_start", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def test_a_user_message_logs_one_entry_with_the_seat_it_names(root, agent, pump):
    agent.emit(user_message("requester: Hello, I need a login fix"))
    agent.emit(user_message("Looking now."))
    agent.emit({"type": "message_start", "message": {"role": "user", "content": "developer: from the pane"}})
    agent.emit(user_message("daemon: The developer seat has taken the gate"))

    assert wait_until(lambda: len(entries(root)) == 4)
    assert entries(root) == [
        ("user", "requester", "Hello, I need a login fix"),
        ("user", "developer", "Looking now."),
        ("user", "developer", "from the pane"),
        ("user", "daemon", "The developer seat has taken the gate"),
    ]


def test_text_deltas_assemble_into_one_assistant_entry_at_message_end(root, agent, pump):
    message = {"role": "assistant", "content": [{"type": "text", "text": "Hello there"}]}
    agent.emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
    for delta in ("Hel", "lo ", "there"):
        agent.emit({"type": "message_update", "message": message, "assistantMessageEvent": {"type": "text_delta", "delta": delta}})
    agent.emit({"type": "message_update", "message": message, "assistantMessageEvent": {"type": "toolcall_start"}})
    agent.emit({"type": "message_end", "message": message})
    agent.emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
    agent.emit({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "Unstreamed"}]}})

    assert wait_until(lambda: len(entries(root)) == 2)
    assert entries(root) == [
        ("assistant", agent.paths.name, "Hello there"),
        ("assistant", agent.paths.name, "Unstreamed"),
    ]


def test_lifecycle_and_dialog_events_log_state_entries(root, agent, pump):
    agent.emit({"type": "agent_start"})
    agent.emit({"type": "ui_prompt_start", "reason": "ui_prompt", "kind": "confirm"})
    agent.emit({"type": "ui_prompt_end", "reason": "ui_prompt", "kind": "confirm"})
    agent.emit({"type": "agent_settled"})
    agent.emit({"type": "ui_prompt_start", "reason": "ui_prompt", "kind": "input"})
    agent.emit({"type": "ui_prompt_end", "reason": "ui_prompt", "kind": "input"})
    agent.emit({"type": "tool_execution_start", "toolName": "bash"})

    assert wait_until(lambda: len(entries(root)) == 6)
    name = agent.paths.name
    assert entries(root) == [
        ("state", name, "working"),
        ("state", name, "needs-attention: confirm"),
        ("state", name, "working"),
        ("state", name, "idle"),
        ("state", name, "needs-attention: input"),
        ("state", name, "idle"),
    ]
    ids = [e.id for e in chatlog.open_log(root, "login-fix").read_from(0)]
    assert ids == sorted(ids) and len(set(ids)) == 6


def test_the_pump_reconnects_after_the_socket_drops(root, agent, pump):
    agent.emit(user_message("requester: before the drop"))
    assert wait_until(lambda: len(entries(root)) == 1)

    agent.drop_clients()

    assert wait_until(lambda: agent.connected == 1 and pump.connections == 2)
    agent.emit(user_message("requester: after the drop"))
    assert wait_until(lambda: len(entries(root)) == 2)
    assert [text for _, _, text in entries(root)] == ["before the drop", "after the drop"]


def test_a_pump_ends_on_its_own_once_the_project_maps_to_no_agent(root, agent, pump):
    seat.forget_agent(root, "login-fix")
    agent.drop_clients()

    assert wait_until(lambda: not pump.alive, timeout=chat.RECONNECT_DELAY + chat.READ_POLL + 5)
    assert agent.connected == 0


def test_a_daemon_start_resumes_a_pump_for_every_mapped_agent_found_alive(root, base):
    live = FakeAgent(base, seat.agent_name("login-fix"))
    seat.record_agent(root, "login-fix", live.paths.name)
    seat.record_agent(root, "dark-mode", seat.agent_name("dark-mode"))
    chatlog.open_log(root, "login-fix").append("user", "requester", "from before the restart")
    app = create_app(root)
    try:
        pumps = app.state.pumps
        assert pumps.running() == {"login-fix": live.paths.name}
        assert wait_until(lambda: live.connected == 1)
        live.emit(user_message("developer: after the restart"))

        assert wait_until(lambda: len(entries(root)) == 2)
        assert [e.id for e in chatlog.open_log(root, "login-fix").read_from(0)] == [1, 2]
        assert entries(root, "dark-mode") == []
    finally:
        app.state.pumps.stop_all()
        live.close()


def test_ensure_runs_one_pump_per_project_and_stop_ends_it(root, agent):
    pumps = chat.Pumps(root)
    try:
        first = pumps.ensure("login-fix", agent.paths.name)
        assert pumps.ensure("login-fix", agent.paths.name) is first
        assert wait_until(lambda: agent.connected == 1)
        assert pumps.running() == {"login-fix": agent.paths.name}

        assert pumps.stop("login-fix") is True
        assert pumps.stop("login-fix") is False
        assert not first.alive and pumps.running() == {}
    finally:
        pumps.stop_all()


def test_subscribe_returns_the_pump_once_it_holds_the_socket(root, agent):
    # What a start hands the seat: by the time subscribe returns the pump is
    # connected, so the opening prompt that follows is seen from its first frame.
    pumps = chat.Pumps(root)
    try:
        pump = pumps.subscribe("login-fix", agent.paths.name)

        assert pump.connections == 1
        # The fake registers the connection on its accept thread, a moment
        # after the pump's connect returns.
        assert wait_until(lambda: agent.connected == 1)
        assert pumps.subscribe("login-fix", agent.paths.name) is pump
    finally:
        pumps.stop_all()


def test_the_label_names_the_author_and_splits_back_off():
    assert chatlog.label("requester", "hi") == "requester: hi"
    assert chatlog.split_label("requester: hi", "developer") == ("requester", "hi")
    assert chatlog.split_label("daemon: hi: there", "developer") == ("daemon", "hi: there")
    assert chatlog.split_label("hi", "developer") == ("developer", "hi")
    assert chatlog.split_label("agent: hi", "developer") == ("developer", "agent: hi")
