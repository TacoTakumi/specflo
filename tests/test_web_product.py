"""The product page: vision, pieces, backlog, and projects, with the archive folded away.

One product's page shows its vision text, the pieces it declares, its
backlog in order, and the projects spawned from its work items. Complete
projects are the archive: hidden by default and shown when the archived
filter is on, through a plain link that changes nothing on the daemon.
"""

import json
import os
import re
import subprocess
import sys
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specflo import config, daemon
from specflo.agent.statefiles import ENV_STATE_DIR
from specflo.daemon import auth, seat, web
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products
from specflo.daemon.workitems import WorkItems
from specflo.service.local import LocalProjectService

VISION = "Ship the thing.\nThen keep it shipping.\n"


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    client = TestClient(create_app(root), follow_redirects=False)
    token = auth.mint_token(root, "requester")
    response = client.post(web.SIGNIN_PATH, data={"identity": "requester", "token": token})
    assert response.status_code == 303, response.text
    return client


def service(root):
    return LocalProjectService(root, config.load_config(root))


def seed(root):
    """``thing``: a vision, pieces web and api, four items, and three projects.

    Item 1 spawned an active project, item 3 a project since completed, and
    item 4 a project since shelved. ``other`` has nothing but a name.
    """
    with store_module.open_store(root) as store:
        products = Products(store)
        products.add("Thing", slug="thing", today="2026-09-06")
        products.set_vision("thing", VISION)
        products.add_piece("thing", "web")
        products.add_piece("thing", "api")
        products.add("Other", slug="other", today="2026-09-06")
        items = WorkItems(store)
        items.add("thing", "Fix the login", piece="web", issue="https://issues/1", today="2026-09-06")
        items.add("thing", "Tidy the readme", kind="idea", dev_path="one-prompt", today="2026-09-06")
        items.add("thing", "Old work", kind="roadmap", piece="api", today="2026-09-06")
        items.add("thing", "Paused work", today="2026-09-06")
        items.set_status(3, "done")
        svc = service(root)
        items.spawn(1, svc, name="Login fix")
        finished = items.spawn(3, svc, name="Old work").project
        svc.complete_project(finished.slug)
        paused = items.spawn(4, svc, name="Paused work").project
        svc.shelve_project(paused.slug, reason="later")


def page(client, slug, **params):
    return client.get(web.PRODUCT_PATH.format(slug=slug), params=params)


def section(html, name):
    """The text of the ``<section id=name>``, tags stripped."""
    match = re.search(rf'<section id="{name}">(.*?)</section>', html, re.S)
    assert match, f"no section {name!r}"
    return unescape(re.sub(r"<[^>]+>", " ", match.group(1)))


def rows(html, name):
    """Each row of the table in ``<section id=name>`` as its cells' text."""
    match = re.search(rf'<section id="{name}">(.*?)</section>', html, re.S)
    assert match, f"no section {name!r}"
    return [
        [unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)]
        for body in re.findall(r"<tbody>.*?</tbody>", match.group(1), re.S)
        for body in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)
    ]


def test_the_page_shows_the_vision_pieces_and_backlog(client, root):
    seed(root)

    response = page(client, "thing")

    assert response.status_code == 200
    html = response.text
    assert "<h1>Thing</h1>" in html
    assert "Ship the thing." in section(html, "vision")
    assert "Then keep it shipping." in section(html, "vision")
    assert re.findall(r"<li>(\w+)</li>", section_html(html, "pieces")) == ["web", "api"]
    backlog = rows(html, "backlog")
    assert [row[1] for row in backlog] == [
        "Fix the login", "Tidy the readme", "Old work", "Paused work",
    ]
    assert backlog[0] == [
        "1", "Fix the login", "fix", "full", "web", "open", "https://issues/1", "Login fix",
    ]
    assert backlog[1][2:6] == ["idea", "one-prompt", "", "open"]
    assert backlog[2][5] == "done"


def section_html(html, name):
    match = re.search(rf'<section id="{name}">(.*?)</section>', html, re.S)
    assert match, f"no section {name!r}"
    return match.group(1)


def test_backlog_links_reach_the_issue_and_the_spawned_project(client, root):
    seed(root)

    html = section_html(page(client, "thing").text, "backlog")

    assert 'href="https://issues/1"' in html
    assert f'href="{web.PROJECT_PATH.format(slug="login-fix")}"' in html


def test_complete_projects_are_hidden_until_the_archived_filter_is_on(client, root):
    seed(root)

    default = page(client, "thing").text
    shown = rows(default, "projects")
    assert [row[0] for row in shown] == ["Login fix", "Paused work"]
    assert [row[2] for row in shown] == ["active", "shelved"]
    assert "Old work" not in section(default, "projects")
    assert "1 archived" in section(default, "projects")

    archived = page(client, "thing", archived="1").text
    shown = rows(archived, "projects")
    assert [row[0] for row in shown] == ["Login fix", "Old work", "Paused work"]
    assert [row[2] for row in shown] == ["active", "complete", "shelved"]


def test_the_archived_filter_is_a_plain_link_both_ways(client, root):
    seed(root)
    path = web.PRODUCT_PATH.format(slug="thing")

    default = section_html(page(client, "thing").text, "projects")
    assert f'href="{path}?archived=1"' in default
    assert "<form" not in default

    archived = section_html(page(client, "thing", archived="1").text, "projects")
    assert f'href="{path}"' in archived
    assert "<form" not in archived


def test_project_rows_show_phase_and_execution_and_link_to_the_project(client, root):
    seed(root)

    html = page(client, "thing").text

    assert rows(html, "projects")[0] == ["Login fix", "brainstorm", "active", "linear"]
    assert f'href="{web.PROJECT_PATH.format(slug="login-fix")}"' in section_html(html, "projects")


def test_a_bare_product_says_what_it_lacks(client, root):
    seed(root)

    html = page(client, "other").text

    assert "No vision yet" in section(html, "vision")
    assert "No pieces declared" in section(html, "pieces")
    assert "No work items" in section(html, "backlog")
    assert "No projects" in section(html, "projects")


def test_an_unknown_product_is_a_404(client, root):
    seed(root)

    assert page(client, "nothing").status_code == 404


def test_the_product_page_needs_a_session(root):
    seed(root)
    browser = TestClient(create_app(root), follow_redirects=False)

    response = browser.get(web.PRODUCT_PATH.format(slug="thing"))

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_a_backlog_row_names_its_project_even_while_the_archive_is_folded_away(client, root):
    seed(root)

    backlog = rows(page(client, "thing").text, "backlog")

    assert backlog[2][1] == "Old work" and backlog[2][7] == "Old work"


def test_an_issue_that_is_not_a_web_link_is_shown_as_text_not_a_link(client, root):
    seed(root)
    from specflo.daemon.store import WorkItem

    with store_module.open_store(root) as store:
        store.add_work_item(WorkItem(
            id=0, product="thing", title="Planted", kind="fix", issue="javascript:alert(1)",
            dev_path="full", status="open", created="2026-09-06",
        ))

    html = section_html(page(client, "thing").text, "backlog")

    assert "javascript:alert(1)" in html
    assert 'href="javascript:alert(1)"' not in html


def test_an_unknown_product_is_a_page_of_the_ui_not_json(client, root):
    seed(root)

    response = page(client, "nothing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "No product 'nothing'." in unescape(response.text) and "signed in as" in response.text


def test_a_bare_product_names_the_verb_that_sets_a_vision(client, root):
    seed(root)

    assert "specflo product set-vision" in section(page(client, "other").text, "vision")


# --- the start-project control ------------------------------------------------

STUB_PI = Path(__file__).parent / "agent" / "stub_pi.py"


@pytest.fixture
def rpc_rig(root, tmp_path, monkeypatch):
    """An isolated agent state dir, the rpc transport on the root, and a stub pi."""
    monkeypatch.setenv(ENV_STATE_DIR, str(tmp_path / "state"))
    for key in ("SPECFLO_AGENT_SERVE", "SPECFLO_AGENT_NAME", "SPECFLO_AGENT_MANAGED"):
        monkeypatch.delenv(key, raising=False)
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"reply": "ok"}), encoding="utf-8")
    monkeypatch.setattr(seat, "DEFAULT_PI_CMD", f"{sys.executable} {STUB_PI} {scenario}")
    yield
    for name in seat.agent_mapping(root).values():
        subprocess.run(
            [sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())", "agent", "stop", name],
            capture_output=True, text=True, env=dict(os.environ), timeout=30,
        )


def start_forms(html):
    """Every start-project form in the backlog: item id to (action, hidden fields)."""
    found = {}
    for item_id, body in re.findall(r'<form[^>]*id="start-(\d+)"[^>]*>(.*?)</form>', html, re.S):
        action = re.search(r'action="([^"]*)"', re.search(rf'<form[^>]*id="start-{item_id}"[^>]*>', html).group(0)).group(1)
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body))
        found[int(item_id)] = (action, fields)
    return found


def start_url(slug, item_id):
    return web.START_PROJECT_PATH.format(slug=slug, item_id=item_id)


def test_only_a_full_path_item_without_a_project_offers_the_start_control(client, root):
    seed(root)
    with store_module.open_store(root) as store:
        WorkItems(store).add("thing", "Offline mode", today="2026-09-06")  # 5: full, no project

    html = page(client, "thing").text

    forms = start_forms(html)
    assert set(forms) == {5}
    action, fields = forms[5]
    assert action == start_url("thing", 5)
    assert fields == {"session": client.cookies[web.SESSION_COOKIE]}
    assert "Start project" in section(html, "backlog")


def test_submitting_the_control_starts_the_project_and_links_to_it(client, root, rpc_rig):
    seed(root)
    with store_module.open_store(root) as store:
        WorkItems(store).add("thing", "Offline mode", today="2026-09-06")

    response = client.post(start_url("thing", 5), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 303
    assert response.headers["location"] == web.PROJECT_PATH.format(slug="offline-mode")
    html = page(client, "thing").text
    assert start_forms(html) == {}
    assert f'href="{web.PROJECT_PATH.format(slug="offline-mode")}"' in section_html(html, "backlog")
    assert rows(html, "backlog")[4][-1] == "Offline mode"
    assert seat.agent_mapping(root) == {"offline-mode": seat.agent_name("offline-mode")}
    assert config.config_path(seat.seat_dir(root, "offline-mode")).is_file()


def test_a_submit_without_the_session_secret_is_refused(client, root, rpc_rig):
    seed(root)
    with store_module.open_store(root) as store:
        WorkItems(store).add("thing", "Offline mode", today="2026-09-06")

    assert client.post(start_url("thing", 5), data={}).status_code == 403
    assert client.post(start_url("thing", 5), data={"session": "wrong"}).status_code == 403
    assert "offline-mode" not in [p.slug for p in service(root).list_projects()]
    assert seat.agent_mapping(root) == {}


def test_a_repeated_submit_is_refused_naming_the_existing_project(client, root, rpc_rig):
    seed(root)

    response = client.post(start_url("thing", 1), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 409
    assert "login-fix" in response.text
    assert seat.agent_mapping(root) == {}


def test_an_ineligible_item_is_refused_and_an_unknown_one_is_a_404(client, root, rpc_rig):
    seed(root)
    secret = client.cookies[web.SESSION_COOKIE]

    assert client.post(start_url("thing", 2), data={"session": secret}).status_code == 409
    assert client.post(start_url("thing", 99), data={"session": secret}).status_code == 404
    assert client.post(start_url("other", 1), data={"session": secret}).status_code == 404
    assert client.post(start_url("nope", 1), data={"session": secret}).status_code == 404
    assert seat.agent_mapping(root) == {}


def test_a_failed_agent_start_reports_the_failure_and_the_page_links_to_the_project(client, root, rpc_rig, monkeypatch):
    seed(root)
    with store_module.open_store(root) as store:
        WorkItems(store).add("thing", "Offline mode", today="2026-09-06")
    monkeypatch.setenv("SPECFLO_AGENT_START_TIMEOUT", "1")
    monkeypatch.setattr(seat, "DEFAULT_PI_CMD", f"{sys.executable} -c 'import sys; sys.exit(3)'")

    response = client.post(start_url("thing", 5), data={"session": client.cookies[web.SESSION_COOKIE]})

    assert response.status_code == 502
    assert "offline-mode" in response.text and str(root) not in response.text
    html = page(client, "thing").text
    assert start_forms(html) == {}
    assert f'href="{web.PROJECT_PATH.format(slug="offline-mode")}"' in section_html(html, "backlog")
    assert seat.agent_mapping(root) == {}


def test_the_start_control_needs_a_session(root):
    seed(root)
    browser = TestClient(create_app(root), follow_redirects=False)

    response = browser.post(start_url("thing", 1), data={"session": "anything"})

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
