"""The project page: where a project stands, who it waits on, and its artifacts as they are.

One hosted project's page shows its phase, status, and execution mode, the
role the current phase waits on, and every artifact the project has so far
with the same text doc show prints. The one control it carries is the take
on an open gate, offered only to the identity the gate waits on and guarded
by the session secret; everything else is a read.
"""

import json
import os
import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.agent.statefiles import ENV_STATE_DIR
from specflo.daemon import auth, routes, seat, web
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products
from specflo.daemon.workitems import WorkItems
from specflo.doc import ARTIFACTS
from specflo.service.local import LocalProjectService
from specflo.workflow import PHASES
from waits import wait_until


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    client = TestClient(create_app(root), follow_redirects=False)
    token = auth.mint_token(root, "developer")
    response = client.post(web.SIGNIN_PATH, data={"identity": "developer", "token": token})
    assert response.status_code == 303, response.text
    return client


def service(root):
    return LocalProjectService(root, config.load_config(root))


def seed(root):
    """Product ``thing`` whose one work item spawned ``login-fix``, now in the spec phase."""
    with store_module.open_store(root) as store:
        Products(store).add("Thing", slug="thing", today="2026-09-06")
        WorkItems(store).add("thing", "Fix the login", today="2026-09-06")
        svc = service(root)
        WorkItems(store).spawn(1, svc, name="Login fix")
    svc.advance_project("login-fix")
    svc.start_spec("login-fix")
    return "login-fix"


def page(client, slug):
    return client.get(web.PROJECT_PATH.format(slug=slug))


def facts(html):
    """The ``<dl class="facts">`` as a dict of term to detail text."""
    match = re.search(r'<dl class="facts">(.*?)</dl>', html, re.S)
    assert match, "no facts list"
    pairs = re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", match.group(1), re.S)
    return {
        unescape(re.sub(r"<[^>]+>", "", term)).strip(): unescape(re.sub(r"<[^>]+>", "", detail)).strip()
        for term, detail in pairs
    }


def artifacts(html):
    """Each artifact section's verbatim text (or None when not yet created), by name."""
    found = {}
    for name, body in re.findall(r'<section id="artifact-(\w+)">(.*?)</section>', html, re.S):
        pre = re.search(r'<pre class="artifact">(.*?)</pre>', body, re.S)
        found[name] = unescape(pre.group(1)) if pre else None
    return found


MUTATING = ("<form", "<button", "<input", "<select", "<textarea", 'method="post"', "hx-post", "hx-put", "hx-patch", "hx-delete")


def test_the_page_shows_where_the_project_stands_and_who_it_waits_on(client, root):
    slug = seed(root)

    response = page(client, slug)

    assert response.status_code == 200
    html = response.text
    assert "<h1>Login fix</h1>" in html
    assert facts(html) == {
        "Phase": "spec",
        "Status": "active",
        "Execution": "linear",
        "Waiting on": "developer",
    }


def gated(root, slug, role="requester", note="Open points: the name, pricing"):
    """Open a gate on ``slug`` as the agent, the way the brainstorm agent does."""
    agent = LocalProjectService(root, config.load_config(root), actor="agent")
    return agent.open_gate(slug, role, note=note).gate


def gate_section(html):
    match = re.search(r'<section id="gate">(.*?)</section>', html, re.S)
    return unescape(re.sub(r"<[^>]+>", " ", match.group(1))) if match else None


def test_an_open_gate_names_the_role_the_project_waits_on(client, root):
    slug = seed(root)
    gate = gated(root, slug)

    html = page(client, slug).text

    assert facts(html)["Waiting on"] == "requester"
    section = gate_section(html)
    assert section is not None
    assert "Open points: the name, pricing" in section
    assert "agent" in section and gate.opened_at in section
    assert f'datetime="{gate.opened_at}"' in html


def test_a_taken_gate_falls_back_to_the_phase_table(client, root):
    slug = seed(root)
    gated(root, slug)
    service(root).take_gate(slug)

    html = page(client, slug).text

    assert facts(html)["Waiting on"] == "developer"
    assert gate_section(html) is None


def test_a_gate_without_a_note_or_an_opener_says_so(client, root):
    slug = seed(root)
    service(root).open_gate(slug, "developer")

    html = page(client, slug).text

    assert facts(html)["Waiting on"] == "developer"
    section = gate_section(html)
    assert "No note" in section and "unnamed" in section


def test_the_gate_note_is_escaped(client, root):
    slug = seed(root)
    gated(root, slug, note="<script>alert(1)</script> & done")

    html = page(client, slug).text

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; done" in html


def test_an_open_gate_on_an_inactive_project_shows_nothing_to_wait_on(client, root):
    slug = seed(root)
    gated(root, slug)
    service(root).shelve_project(slug)

    html = page(client, slug).text

    assert facts(html)["Waiting on"] == "nobody"
    assert gate_section(html) is None


def test_the_waiting_role_table_covers_every_phase_with_the_developer(root):
    assert list(web.WAITING_ROLES) == PHASES
    assert set(web.WAITING_ROLES.values()) == {"developer"}


@pytest.mark.parametrize("close", ["complete_project", "shelve_project"])
def test_a_project_no_longer_active_waits_on_nobody(client, root, close):
    slug = seed(root)
    getattr(service(root), close)(slug)

    shown = facts(page(client, slug).text)

    assert shown["Status"] == ("complete" if close == "complete_project" else "shelved")
    assert shown["Waiting on"] == "nobody"


def test_each_artifact_renders_the_text_doc_show_prints(client, root):
    slug = seed(root)
    svc = service(root)

    shown = artifacts(page(client, slug).text)

    assert list(shown) == list(ARTIFACTS)
    for name in ARTIFACTS:
        if svc.has_artifact(slug, name):
            assert shown[name] == svc.show_document(slug, name), name
        else:
            assert shown[name] is None, name
    assert shown["spec"] is not None and shown["brainstorm"] is not None
    assert shown["plan"] is None


def test_an_artifact_not_yet_created_says_so(client, root):
    slug = seed(root)

    html = page(client, slug).text

    plan = re.search(r'<section id="artifact-plan">(.*?)</section>', html, re.S).group(1)
    assert "not created yet" in plan
    assert "<pre" not in plan


def test_the_page_has_no_form_or_mutating_control_without_a_gate_to_take(client, root):
    slug = seed(root)

    html = page(client, slug).text.lower()

    for marker in MUTATING:
        assert marker not in html, marker


# --- the take control ---------------------------------------------------------


def take_form(html):
    """The take form, or None: ``(action, hidden fields)``."""
    match = re.search(r'<form[^>]*id="take"[^>]*>(.*?)</form>', html, re.S)
    if not match:
        return None
    action = re.search(r'action="([^"]*)"', match.group(0)).group(1)
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', match.group(1)))
    return action, fields


def take_url(slug):
    return web.TAKE_PATH.format(slug=slug)


def audit_records(root):
    path = root / routes.AUDIT_FILENAME
    return [json.loads(line) for line in path.read_text().splitlines()] if path.is_file() else []


def test_the_take_runs_its_socket_call_off_the_event_loop(client, root, monkeypatch):
    # The take tells the agent over its socket; the call below only
    # completes from a worker thread the loop is free to serve, so a take
    # on the loop would fail here.
    slug = brainstorming(root)
    gated(root, slug, role="developer")
    announced = []

    def announce_off_loop(root_, project):
        anyio.from_thread.run(anyio.sleep, 0)
        announced.append(project.slug)
        return True

    monkeypatch.setattr(seat, "announce_take", announce_off_loop)

    response = client.post(take_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 303, response.text
    assert announced == [slug]
    assert service(root).load_project(slug).gate.taken_by == "developer"


def test_the_take_control_is_offered_only_to_the_identity_the_gate_waits_on(client, root):
    slug = seed(root)
    gated(root, slug, role="requester")

    assert take_form(page(client, slug).text) is None
    for marker in MUTATING:
        assert marker not in page(client, slug).text.lower(), marker

    service(root).take_gate(slug)
    gated(root, slug, role="developer")
    form = take_form(page(client, slug).text)

    assert form is not None
    action, fields = form
    assert action == take_url(slug)
    assert fields == {"session": client.cookies[web.SESSION_COOKIE]}
    assert take_form(signed_in(root, "requester").get(web.PROJECT_PATH.format(slug=slug)).text) is None


def signed_in(root, identity):
    client = TestClient(create_app(root), follow_redirects=False)
    token = auth.mint_token(root, identity)
    assert client.post(web.SIGNIN_PATH, data={"identity": identity, "token": token}).status_code == 303
    return client


def test_taking_through_the_page_closes_the_gate_as_the_session_identity(client, root):
    slug = seed(root)
    gated(root, slug, role="developer")

    response = client.post(take_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 303
    assert response.headers["location"] == web.PROJECT_PATH.format(slug=slug)
    gate = service(root).load_project(slug).gate
    assert not gate.is_open
    assert gate.taken_by == "developer" and gate.taken_at
    html = page(client, slug).text
    assert gate_section(html) is None and take_form(html) is None
    assert facts(html)["Waiting on"] == "developer"
    last = audit_records(root)[-1]
    assert last["identity"] == "developer" and last["operation"] == "take_gate"
    assert last["project"] == slug


def test_a_take_without_the_session_secret_is_refused(client, root):
    slug = seed(root)
    gated(root, slug, role="developer")

    missing = client.post(take_url(slug), data={})
    wrong = client.post(take_url(slug), data={"session": "not-the-secret"})

    assert missing.status_code == 403 and wrong.status_code == 403
    assert service(root).load_project(slug).gate.is_open
    assert audit_records(root) == []


def test_a_take_by_an_identity_the_gate_does_not_wait_on_is_refused(client, root):
    slug = seed(root)
    gated(root, slug, role="requester")

    response = client.post(take_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 403
    assert service(root).load_project(slug).gate.is_open
    assert audit_records(root) == []


def test_a_session_value_with_a_non_ascii_character_is_refused_like_any_wrong_one(client, root):
    # A cross-site or hand-built post may carry any text; the guard must
    # answer with its 403 page, not fall over on the comparison.
    slug = brainstorming(root)
    gated(root, slug, role="developer")
    posts = {
        "take": (take_url(slug), {"session": "\u00e9"}),
        "start agent": (start_agent_url(slug), {"session": "s\u00e9cret"}),
        "chat": (web.CHAT_PATH.format(slug=slug), {"session": "\u00e9", "text": "hi"}),
        "start project": (web.START_PROJECT_PATH.format(slug="thing", item_id=1), {"session": "\u00e9"}),
    }

    for name, (url, fields) in posts.items():
        response = client.post(url, data=fields)
        assert response.status_code == 403, (name, response.status_code)
        assert "did not come from this session" in response.text, name

    assert service(root).load_project(slug).gate.is_open
    assert audit_records(root) == []


def test_a_take_on_a_shelved_project_with_an_open_gate_is_refused(client, root):
    # The page shows no gate for a project that is not active; a post to the
    # take route must not close the gate the page did not offer.
    slug = seed(root)
    gated(root, slug, role="developer")
    service(root).shelve_project(slug)

    response = client.post(take_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 409
    gate = service(root).load_project(slug).gate
    assert gate.is_open and gate.role == "developer" and not gate.taken_by
    assert audit_records(root) == []


def test_the_take_checks_the_gate_as_it_stands_when_it_runs_not_as_the_page_showed_it(client, root):
    # Between the developer's page load and the post, the gate was taken and
    # reopened for the requester: the record at take time decides.
    slug = seed(root)
    gated(root, slug, role="developer")
    assert take_form(page(client, slug).text) is not None
    service(root).take_gate(slug)
    gated(root, slug, role="requester", note="Back to the requester")

    response = client.post(take_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 403
    gate = service(root).load_project(slug).gate
    assert gate.is_open and gate.role == "requester"
    assert audit_records(root) == []


def test_a_take_with_no_open_gate_is_refused_as_a_page(client, root):
    slug = seed(root)

    response = client.post(take_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 409
    assert "No open gate" in response.text
    assert audit_records(root) == []


def test_a_take_needs_a_session(root):
    slug = seed(root)
    gated(root, slug, role="developer")
    browser = TestClient(create_app(root), follow_redirects=False)

    response = browser.post(take_url(slug), data={"session": "anything"})

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
    assert service(root).load_project(slug).gate.is_open


def test_a_take_on_an_unknown_project_is_a_404_page(client, root):
    seed(root)

    response = client.post(take_url("nope"), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 404


def test_the_page_links_back_to_its_product(client, root):
    slug = seed(root)

    html = page(client, slug).text

    assert f'href="{web.PRODUCT_PATH.format(slug="thing")}"' in html
    assert "Thing" in html


def test_a_project_made_outside_any_product_still_has_a_page(client, root):
    service(root).create_project("Loose", summary="no product")

    response = page(client, "loose")

    assert response.status_code == 200
    assert facts(response.text)["Phase"] == "brainstorm"
    assert "no product" in response.text


def test_an_unknown_project_is_a_404_page(client, root):
    seed(root)

    response = page(client, "nothing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "No project 'nothing'." in unescape(response.text)


def test_the_project_page_needs_a_session(root):
    slug = seed(root)
    browser = TestClient(create_app(root), follow_redirects=False)

    response = browser.get(web.PROJECT_PATH.format(slug=slug))

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_an_unreadable_project_is_a_500_page_not_a_traceback(client, root):
    seed(root)
    broken = root / daemon.PROJECTS_DIRNAME / "broken"
    broken.mkdir(parents=True)
    (broken / "project.md").write_text("not a project\n")

    response = page(client, "broken")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert 'class="brand"' in response.text
    assert "Project 'broken'" in unescape(response.text)
    assert "Traceback" not in response.text
    assert page(client, "nothing").status_code == 404


def test_a_refusal_while_rendering_any_page_is_a_500_page(client, root, monkeypatch):
    from specflo.errors import SpecfloError

    def refuse(root):
        raise SpecfloError("the store is unreadable")

    monkeypatch.setattr(web, "product_cards", refuse)

    response = client.get(web.HOME_PATH)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert "the store is unreadable" in response.text
    assert "signed in as" in response.text


@pytest.mark.parametrize("raw", ["%2e%2e", "a%2Fb", "Upper", ".hidden"])
def test_a_slug_that_is_not_one_is_a_404_page_before_any_file_is_read(client, root, monkeypatch, raw):
    seed(root)
    loaded = []
    real = LocalProjectService.load_project
    monkeypatch.setattr(
        LocalProjectService, "load_project", lambda self, slug: loaded.append(slug) or real(self, slug)
    )

    response = client.get(f"/projects/{raw}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "No project" in unescape(response.text)
    assert loaded == []


# --- the agent and its start control ------------------------------------------

STUB_PI = Path(__file__).parent / "agent" / "stub_pi.py"


def agent_cli(*args):
    return subprocess.run(
        [sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())", "agent", *args],
        capture_output=True, text=True, env=dict(os.environ), timeout=30,
    )


@pytest.fixture
def rpc_rig(root, tmp_path, monkeypatch):
    """An isolated agent state dir, the rpc transport on the root, and a stub pi."""
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "state"))
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED", "SPECFLO_AGENT_START_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"reply": "ok"}), encoding="utf-8")
    monkeypatch.setattr(seat, "DEFAULT_PI_CMD", f"{sys.executable} {STUB_PI} {scenario}")
    yield
    for name in seat.agent_mapping(root).values():
        agent_cli("stop", name)


def brainstorming(root):
    """Product ``thing`` whose one work item spawned ``login-fix``, still in brainstorm."""
    with store_module.open_store(root) as store:
        Products(store).add("Thing", slug="thing", today="2026-09-06")
        WorkItems(store).add("thing", "Fix the login", today="2026-09-06")
        WorkItems(store).spawn(1, service(root), name="Login fix")
    return "login-fix"


def agent_section(html):
    match = re.search(r'<section id="agent">(.*?)</section>', html, re.S)
    return unescape(re.sub(r"<[^>]+>", " ", match.group(1))) if match else None


def page_once_idle(client, slug, name):
    """The project page once its state line says ``name`` is idle.

    A start returns when the agent has taken its opening prompt, not when
    the run that prompt opens settles. The state line is the chat log's last
    state entry, which the pump writes as the agent reports the settle, so
    a page read at once can still say the agent is working.
    """
    def idle_page():
        html = page(client, slug).text
        return html if f"{name} is idle." in (agent_section(html) or "") else None

    return wait_until(idle_page, message=f"the page to say {name} is idle")


def start_agent_form(html):
    """The start-agent form, or None: ``(action, hidden fields)``."""
    match = re.search(r'<form[^>]*id="start-agent"[^>]*>(.*?)</form>', html, re.S)
    if not match:
        return None
    action = re.search(r'action="([^"]*)"', match.group(0)).group(1)
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', match.group(1)))
    return action, fields


def start_agent_url(slug):
    return web.START_AGENT_PATH.format(slug=slug)


def test_a_brainstorm_project_with_no_agent_on_record_offers_the_start_control(client, root):
    slug = brainstorming(root)

    html = page(client, slug).text

    assert seat.liveness(root, slug) == seat.Liveness(name=None, alive=False, state=seat.MISSING_STATE)
    assert "no agent yet" in agent_section(html)
    action, fields = start_agent_form(html)
    assert action == start_agent_url(slug)
    assert fields == {"session": client.cookies[web.SESSION_COOKIE]}
    assert "Start agent" in html


def test_a_project_past_brainstorm_has_no_agent_section_and_refuses_the_start(client, root):
    slug = seed(root)

    assert agent_section(page(client, slug).text) is None
    response = client.post(start_agent_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 409
    assert "no agent to start" in response.text
    assert seat.agent_mapping(root) == {}


def test_the_control_starts_the_agent_and_the_page_then_shows_it_serving(client, root, rpc_rig):
    slug = brainstorming(root)

    response = client.post(start_agent_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 303
    assert response.headers["location"] == web.PROJECT_PATH.format(slug=slug)
    name = seat.agent_name(slug)
    assert seat.agent_mapping(root) == {slug: name}
    assert config.config_path(seat.seat_dir(root, slug)).is_file()
    html = page_once_idle(client, slug, name)
    assert start_agent_form(html) is None
    assert f"{name} is idle." in agent_section(html)
    last = audit_records(root)[-1]
    assert (last["identity"], last["operation"], last["project"]) == ("developer", "start_agent", slug)


def test_the_control_runs_the_start_off_the_event_loop_so_the_daemon_keeps_answering(client, root, monkeypatch):
    # The agent calls the daemon back while it starts, so a start that held
    # the event loop would wait on itself; the probe below only completes
    # from a worker thread the loop is free to serve.
    slug = brainstorming(root)
    served = []

    def start_off_loop(root_, slug_, **kwargs):
        anyio.from_thread.run(anyio.sleep, 0)
        served.append(slug_)
        return seat.agent_name(slug_)

    monkeypatch.setattr(seat, "start_agent", start_off_loop)
    monkeypatch.setattr(client.app.state.pumps, "ensure", lambda slug_, name: None)

    response = client.post(start_agent_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 303
    assert served == [slug]


def test_a_failed_start_page_names_the_agent_and_none_of_the_clis_words(client, root, monkeypatch):
    slug = brainstorming(root)
    socket_path = root / "state" / "project-login-fix.sock"

    def refuse(root_, slug_, **kwargs):
        raise seat.AgentStartError(
            f"Starting agent failed: already running (live host on {socket_path})",
            agent=seat.agent_name(slug_), slug=slug_,
        )

    monkeypatch.setattr(seat, "start_agent", refuse)

    response = client.post(start_agent_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 502
    assert seat.agent_name(slug) in response.text and slug in response.text
    assert str(root) not in response.text and ".sock" not in response.text and "already running" not in response.text


def test_a_start_without_the_secret_or_beside_a_live_agent_is_refused(client, root, rpc_rig):
    slug = brainstorming(root)
    secret = client.cookies[web.SESSION_COOKIE]
    assert client.post(start_agent_url(slug), data={}).status_code == 403
    assert client.post(start_agent_url(slug), data={"session": "wrong"}).status_code == 403
    assert seat.agent_mapping(root) == {}
    assert client.post(start_agent_url(slug), data={"session": secret}).status_code == 303
    before = json.loads(agent_cli("status", seat.agent_name(slug), "--json").stdout)["status"]["host_pid"]

    response = client.post(start_agent_url(slug), data={"session": secret})

    assert response.status_code == 409
    assert "already serving" in response.text
    after = json.loads(agent_cli("status", seat.agent_name(slug), "--json").stdout)["status"]["host_pid"]
    assert after == before
    assert client.post(start_agent_url("nope"), data={"session": secret}).status_code == 404


def test_an_agent_stopped_out_of_band_brings_the_control_back_and_the_control_restarts_it(client, root, rpc_rig):
    slug = brainstorming(root)
    secret = client.cookies[web.SESSION_COOKIE]
    assert client.post(start_agent_url(slug), data={"session": secret}).status_code == 303
    name = seat.agent_name(slug)

    assert agent_cli("stop", name).returncode == 0

    html = page(client, slug).text
    assert seat.liveness(root, slug).alive is False
    assert f"{name} is {seat.DEAD_STATE}: not serving." in agent_section(html)
    assert start_agent_form(html) is not None
    assert seat.agent_mapping(root) == {slug: name}

    assert client.post(start_agent_url(slug), data={"session": secret}).status_code == 303

    html = page_once_idle(client, slug, name)
    assert start_agent_form(html) is None
    assert f"{name} is idle" in agent_section(html)
    assert seat.liveness(root, slug).alive is True


def test_a_fresh_daemon_over_the_same_root_finds_the_live_agent_by_discovery(client, root, rpc_rig):
    slug = brainstorming(root)
    assert client.post(start_agent_url(slug), data={"session": client.cookies[web.SESSION_COOKIE]}).status_code == 303
    name = seat.agent_name(slug)
    host_pid = json.loads(agent_cli("status", name, "--json").stdout)["status"]["host_pid"]
    audits = len(audit_records(root))

    again = signed_in(root, "developer")
    html = page_once_idle(again, slug, name)

    assert start_agent_form(html) is None
    assert f"{name} is idle" in agent_section(html)
    assert json.loads(agent_cli("status", name, "--json").stdout)["status"]["host_pid"] == host_pid
    assert len(audit_records(root)) == audits
