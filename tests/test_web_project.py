"""The project page: where a project stands, who it waits on, and its artifacts as they are.

One hosted project's page shows its phase, status, and execution mode, the
role the current phase waits on, and every artifact the project has so far
with the same text doc show prints. It is a read: the page carries no form
and no control that would change the project.
"""

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.daemon import auth, web
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products
from specflo.daemon.workitems import WorkItems
from specflo.doc import ARTIFACTS
from specflo.service.local import LocalProjectService
from specflo.workflow import PHASES


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


def test_the_page_has_no_form_or_mutating_control(client, root):
    slug = seed(root)

    html = page(client, slug).text.lower()

    for marker in MUTATING:
        assert marker not in html, marker


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
