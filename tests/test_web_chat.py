"""Posting from the project page: the message reaches the agent's socket as a prompt.

A fake agent serves a control socket where the real host would: it answers
status with the state a test gives it, and answers every prompt command
with success while keeping what it was asked, so a test can read what the
socket received. It never settles on its own: a post that returns while the
fake still reports working has returned before the run ended.

The transcript half: the page renders the project's chat log as one line
per entry in the monospace region and connects the region to the stream
from the last id it rendered; the stream replays from the id a client
names and then follows the log as the pump fills it.
"""

import re
import socket
import threading
import time
from html import unescape
from types import SimpleNamespace

import httpx
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


# --- the transcript: the page renders the log, the stream follows it -----------


def stream_url(slug, after=None):
    url = web.CHAT_STREAM_PATH.format(slug=slug)
    return url if after is None else f"{url}?after={after}"


def log(root, slug="login-fix"):
    return chatlog.open_log(root, slug)


def seeded_log(root, slug):
    """Three entries: a requester line, the assistant's reply, a state change."""
    entries = [
        log(root, slug).append(chat.USER_KIND, seat.REQUESTER_SEAT, "Hello, I need the login fixed"),
        log(root, slug).append(chat.ASSISTANT_KIND, seat.agent_name(slug), "Tell me what fails.\nOne case at a time."),
        log(root, slug).append(chat.STATE_KIND, seat.agent_name(slug), chat.IDLE),
    ]
    return entries


def transcript(html):
    """The transcript region's opening tag attributes and its lines: ``(attrs, [(id, label, time, text)])``."""
    match = re.search(r'<div id="transcript"([^>]*)>(.*?)</div>\s*</section>', html, re.S)
    if not match:
        return None
    attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', match.group(1)))
    return attrs, lines(match.group(2))


def lines(fragment):
    found = []
    for entry_id, body in re.findall(r'<div class="line[^"]*" id="entry-(\d+)">(.*?)</div>', fragment, re.S):
        label = re.search(r'<span class="author">(.*?)</span>', body, re.S).group(1)
        when = re.search(r'<time datetime="([^"]*)">', body).group(1)
        text = re.search(r'<span class="text">(.*?)</span>', body, re.S).group(1)
        found.append((int(entry_id), unescape(label), when, unescape(text)))
    return found


def read_events(stream_lines, count):
    """The next ``count`` events off a stream's line iterator: ``(id, data)`` each; comments are skipped."""
    events, event_id, data = [], None, []
    for line in stream_lines:
        if line == "":
            if data or event_id is not None:
                events.append((event_id, "\n".join(data)))
                event_id, data = None, []
                if len(events) == count:
                    return events
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            event_id = value
        elif field == "data":
            data.append(value)
    return events


def test_a_fresh_page_renders_every_entry_as_one_line_in_the_monospace_region(requester, root, slug, idle_agent):
    entries = seeded_log(root, slug)

    html = requester.get(web.PROJECT_PATH.format(slug=slug)).text

    attrs, shown = transcript(html)
    assert attrs["class"] == "transcript"
    assert shown == [
        (1, seat.REQUESTER_SEAT, entries[0].time, "Hello, I need the login fixed"),
        (2, seat.agent_name(slug), entries[1].time, "Tell me what fails.\nOne case at a time."),
        (3, chat.STATE_KIND, entries[2].time, chat.IDLE),
    ]
    base = (web.TEMPLATES_DIR / "base.html").read_text()
    rule = re.search(r"\.transcript\s*\{([^}]*)\}", base)
    assert rule and "monospace" in rule.group(1)


def test_the_page_connects_the_region_to_the_stream_from_the_last_rendered_id(requester, root, slug, idle_agent):
    seeded_log(root, slug)

    attrs, _ = transcript(requester.get(web.PROJECT_PATH.format(slug=slug)).text)

    assert attrs["hx-sse:connect"] == stream_url(slug, after=3)
    assert attrs["hx-swap"] == "beforeend"


def test_a_page_with_no_agent_to_follow_shows_the_transcript_without_a_connection(requester, root, slug):
    seeded_log(root, slug)
    LocalProjectService(root, config.load_config(root)).advance_project(slug)

    attrs, shown = transcript(requester.get(web.PROJECT_PATH.format(slug=slug)).text)

    assert len(shown) == 3
    assert "hx-sse:connect" not in attrs


@pytest.fixture
def live(live_daemon, tmp_path, monkeypatch):
    """The stream over a real socket: a live daemon, a brainstorming project, a fake agent, a signed-in requester.

    The test client buffers a whole response before it returns, so an open
    stream never comes back through it; these tests read the stream as a
    browser does, one event at a time, and closing the client is the
    disconnect the server sees.
    """
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "state"))
    root = live_daemon["root"]
    slug = brainstorming(root)
    fake = fake_agent(root, tmp_path, slug)
    client = httpx.Client(base_url=live_daemon["url"], follow_redirects=False, timeout=10.0)
    token = auth.mint_token(root, seat.REQUESTER_SEAT)
    response = client.post(web.SIGNIN_PATH, data={"identity": seat.REQUESTER_SEAT, "token": token})
    assert response.status_code == 303, response.text
    yield SimpleNamespace(root=root, slug=slug, agent=fake, client=client)
    client.close()
    fake.close()


def test_a_reconnecting_page_receives_only_what_it_missed_once_and_in_order(live):
    entries = seeded_log(live.root, live.slug)

    with live.client.stream("GET", stream_url(live.slug), headers={"Last-Event-ID": "1"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        events = read_events(response.iter_lines(), count=2)

    assert [event_id for event_id, _ in events] == ["2", "3"]
    assert lines(events[0][1]) == [(2, seat.agent_name(live.slug), entries[1].time, "Tell me what fails.\nOne case at a time.")]
    assert lines(events[1][1]) == [(3, chat.STATE_KIND, entries[2].time, chat.IDLE)]


def test_the_header_wins_over_the_query_and_the_query_serves_a_first_connection(live):
    seeded_log(live.root, live.slug)

    with live.client.stream("GET", stream_url(live.slug, after=2)) as response:
        [(event_id, _)] = read_events(response.iter_lines(), count=1)
    assert event_id == "3"

    with live.client.stream("GET", stream_url(live.slug, after=0), headers={"Last-Event-ID": "2"}) as response:
        [(event_id, _)] = read_events(response.iter_lines(), count=1)
    assert event_id == "3"


def test_a_fresh_connection_replays_the_whole_transcript_then_follows_new_entries(live):
    seeded_log(live.root, live.slug)

    with live.client.stream("GET", stream_url(live.slug)) as response:
        stream_lines = response.iter_lines()
        first = read_events(stream_lines, count=3)
        late = log(live.root, live.slug).append(chat.USER_KIND, seat.DEVELOPER_SEAT, "from the pane")
        [(event_id, data)] = read_events(stream_lines, count=1)

    assert [event_id for event_id, _ in first] == ["1", "2", "3"]
    assert event_id == "4"
    assert lines(data) == [(4, seat.DEVELOPER_SEAT, late.time, "from the pane")]


def test_the_stream_needs_a_session_and_a_project(root, requester, slug):
    client = TestClient(create_app(root), follow_redirects=False)
    response = client.get(stream_url(slug))
    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH

    assert requester.get(stream_url("nope")).status_code == 404
