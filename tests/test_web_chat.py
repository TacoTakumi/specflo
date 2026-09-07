"""Posting from the project page: the message reaches the agent's socket as a prompt.

A fake agent serves a control socket where the real host would: it answers
status with the state a test gives it, and answers every prompt command
with success while keeping what it was asked, so a test can read what the
socket received. It never settles on its own: a post that returns while the
fake still reports working has returned before the run ended.
"""

import re
import socket
import threading
import time
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.agent.protocol import FrameDecoder, encode_frame
from specflo.agent.statefiles import ENV_STATE_DIR, AgentPaths
from specflo.daemon import auth, chat, chatlog, seat, web
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products
from specflo.daemon.workitems import WorkItems
from specflo.service.local import LocalProjectService

# How long a post may take when the agent answers at once, in seconds.
POST_BOUND = 5.0


class FakeAgent:
    """A control socket for agent ``name``: status answered, prompts acknowledged and kept."""

    def __init__(self, base, name, state="idle"):
        self.paths = AgentPaths.resolve(name, base)
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self.state = state
        self.prompts = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.paths.socket))
        self.server.listen()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._closed.is_set():
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(conn)
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
                kind = frame.get("type")
                if kind == "status":
                    self._send(conn, {
                        "type": "response", "id": frame.get("id"), "command": "status", "success": True,
                        "data": {"status": {"state": self.state, "transport": "rpc"}, "paths": {}},
                    })
                elif kind == "prompt":
                    with self._lock:
                        self.prompts.append(frame)
                    self._send(conn, {"type": "response", "id": frame.get("id"), "command": "prompt", "success": True})
        conn.close()

    def _send(self, conn, frame):
        try:
            conn.sendall(encode_frame(frame))
        except OSError:
            pass

    def received(self):
        with self._lock:
            return list(self.prompts)

    def close(self):
        self._closed.set()
        with self._lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.close()
        self.server.close()
        self.paths.socket.unlink(missing_ok=True)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "state"))
    return daemon.prepare_root(tmp_path / "daemon")


def brainstorming(root):
    """Product ``thing`` whose one work item spawned ``login-fix``, still in brainstorm."""
    with store_module.open_store(root) as store:
        Products(store).add("Thing", slug="thing", today="2026-09-06")
        WorkItems(store).add("thing", "Fix the login", today="2026-09-06")
        WorkItems(store).spawn(1, LocalProjectService(root, config.load_config(root)), name="Login fix")
    return "login-fix"


@pytest.fixture
def slug(root):
    return brainstorming(root)


def fake_agent(root, tmp_path, slug, state="idle"):
    name = seat.agent_name(slug)
    seat.record_agent(root, slug, name)
    return FakeAgent(tmp_path / "state", name, state=state)


@pytest.fixture
def idle_agent(root, tmp_path, slug):
    fake = fake_agent(root, tmp_path, slug)
    yield fake
    fake.close()


@pytest.fixture
def working_agent(root, tmp_path, slug):
    fake = fake_agent(root, tmp_path, slug, state="working")
    yield fake
    fake.close()


def signed_in(root, identity):
    client = TestClient(create_app(root), follow_redirects=False)
    token = auth.mint_token(root, identity)
    response = client.post(web.SIGNIN_PATH, data={"identity": identity, "token": token})
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def requester(root):
    return signed_in(root, seat.REQUESTER_SEAT)


@pytest.fixture
def developer(root):
    return signed_in(root, seat.DEVELOPER_SEAT)


def chat_url(slug):
    return web.CHAT_PATH.format(slug=slug)


def post(client, slug, text, secret=None):
    fields = {"text": text, "session": client.cookies[web.SESSION_COOKIE] if secret is None else secret}
    return client.post(chat_url(slug), data=fields)


def chat_form(html):
    """The chat form, or None: ``(action, hidden fields, field names)``."""
    match = re.search(r'<form[^>]*id="chat"[^>]*>(.*?)</form>', html, re.S)
    if not match:
        return None
    action = re.search(r'action="([^"]*)"', match.group(0)).group(1)
    hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', match.group(1)))
    names = set(re.findall(r'name="([^"]+)"', match.group(1)))
    return action, hidden, names


def test_a_requester_post_to_an_idle_agent_is_a_prompt_with_the_requester_label(requester, slug, idle_agent):
    response = post(requester, slug, "Hello, I need the login fixed")

    assert response.status_code == 303, response.text
    assert response.headers["location"] == web.PROJECT_PATH.format(slug=slug)
    [prompt] = idle_agent.received()
    assert prompt["message"] == chatlog.label(seat.REQUESTER_SEAT, "Hello, I need the login fixed")
    assert prompt["message"].startswith(seat.REQUESTER_SEAT + chatlog.LABEL_SEPARATOR)
    assert "streamingBehavior" not in prompt


def test_a_developer_post_while_the_agent_works_is_a_steer_that_returns_before_settle(developer, slug, working_agent):
    started = time.monotonic()
    response = post(developer, slug, "Skip the landscape scan")
    elapsed = time.monotonic() - started

    assert response.status_code == 303, response.text
    assert elapsed < POST_BOUND
    [prompt] = working_agent.received()
    assert prompt["message"] == chatlog.label(seat.DEVELOPER_SEAT, "Skip the landscape scan")
    assert prompt["streamingBehavior"] == "steer"
    # The fake never settles: the post came back with the run still on.
    assert working_agent.state == "working"
    assert seat.liveness(developer.app.state.root, slug).state == "working"


def test_the_page_offers_the_form_with_the_session_secret_to_a_live_agent(requester, slug, idle_agent):
    html = requester.get(web.PROJECT_PATH.format(slug=slug)).text

    action, hidden, names = chat_form(html)
    assert action == chat_url(slug)
    assert hidden == {"session": requester.cookies[web.SESSION_COOKIE]}
    assert "text" in names


def test_a_post_without_the_session_secret_is_refused_and_reaches_no_socket(requester, slug, idle_agent):
    response = post(requester, slug, "forged", secret="not-the-secret")

    assert response.status_code == 403
    assert idle_agent.received() == []


def test_a_post_needs_a_session(root, slug, idle_agent):
    client = TestClient(create_app(root), follow_redirects=False)

    response = client.post(chat_url(slug), data={"text": "hello", "session": ""})

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
    assert idle_agent.received() == []


def test_an_empty_post_sends_nothing(requester, slug, idle_agent):
    response = post(requester, slug, "   ")

    assert response.status_code == 400
    assert idle_agent.received() == []


def test_a_post_to_a_project_with_no_serving_agent_is_a_502_page(requester, slug):
    html = requester.get(web.PROJECT_PATH.format(slug=slug)).text
    assert chat_form(html) is None

    response = post(requester, slug, "anyone there?")

    assert response.status_code == 502
    assert "has no agent to take the message" in unescape(response.text)


def test_a_post_to_a_project_past_the_chat_phase_is_refused(requester, root, slug):
    LocalProjectService(root, config.load_config(root)).advance_project(slug)

    response = post(requester, slug, "too late")

    assert response.status_code == 409


def test_a_post_to_an_unknown_project_is_a_404_page(requester):
    response = post(requester, "nope", "hello")

    assert response.status_code == 404


def test_the_helper_labels_with_the_author_and_refuses_a_project_without_an_agent(root, slug, idle_agent):
    chat.post_message(root, slug, seat.DEVELOPER_SEAT, "from the helper")
    [prompt] = idle_agent.received()
    assert prompt["message"] == "developer: from the helper"

    seat.forget_agent(root, slug)
    with pytest.raises(seat.AgentMessageError):
        chat.post_message(root, slug, seat.DEVELOPER_SEAT, "nobody home")
