"""The products page: every product with its summary and two counts.

The page is the signed-in browser's landing. It lists every product in
slug order with the first line of its vision as the summary, how many of
its work items are still open, and how many of its projects are active,
all read from the store and the project index on every request.
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
from specflo.service.local import LocalProjectService


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
    """Two products: ``thing`` with three open items and one active project; ``other`` bare.

    ``thing`` also carries a done item, a dropped item, and a complete project,
    none of which count. One of its open items is in progress: still open work.
    """
    with store_module.open_store(root) as store:
        products = Products(store)
        products.add("Thing", slug="thing", repo="git@host:me/thing.git", today="2026-09-06")
        products.set_vision("thing", "Ship the thing.\nThen keep it shipping.\n")
        products.add("Other", slug="other", today="2026-09-06")
        items = WorkItems(store)
        items.add("thing", "Fix the login", today="2026-09-06")  # 1: open, spawns active
        items.add("thing", "Offline mode", kind="roadmap", today="2026-09-06")  # 2: in progress
        items.add("thing", "Dark mode", kind="idea", today="2026-09-06")  # 3: open
        items.add("thing", "Old work", today="2026-09-06")  # 4: done, spawns complete
        items.add("thing", "Bad idea", kind="idea", today="2026-09-06")  # 5: dropped
        items.set_status(2, "in-progress")
        items.set_status(4, "done")
        items.set_status(5, "dropped")
        svc = service(root)
        items.spawn(1, svc)
        finished = items.spawn(4, svc).project
        svc.complete_project(finished.slug)


def rows(html):
    """Each product row's cells as text, keyed by product slug."""
    found = {}
    for slug, body in re.findall(r'<tr data-product="([^"]+)">(.*?)</tr>', html, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)
        found[slug] = [unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in cells]
    return found


def inbox(html):
    """The inbox section: its rows as (slug, cells) in page order, or the empty-state text."""
    match = re.search(r'<section id="inbox">(.*?)</section>', html, re.S)
    assert match, "no inbox section"
    body = match.group(1)
    found = []
    for slug, row in re.findall(r'<li data-project="([^"]+)">(.*?)</li>', body, re.S):
        found.append((slug, unescape(re.sub(r"<[^>]+>", " ", row))))
    empty = re.search(r'<p class="empty">(.*?)</p>', body, re.S)
    return found, (unescape(empty.group(1)).strip() if empty else None)


def seed_gates(root):
    """Three projects under two products: two wait on the developer, one gate is taken.

    ``dark-mode`` was opened after ``login-fix``, so it is the newer of the two.
    """
    with store_module.open_store(root) as store:
        products = Products(store)
        products.add("Thing", slug="thing", today="2026-09-06")
        products.add("Other", slug="other", today="2026-09-06")
        items = WorkItems(store)
        items.add("thing", "Fix the login", today="2026-09-06")
        items.add("other", "Dark mode", today="2026-09-06")
        items.add("thing", "Old work", today="2026-09-06")
        svc = service(root)
        items.spawn(1, svc, name="Login fix")
        items.spawn(2, svc, name="Dark mode")
        items.spawn(3, svc, name="Old work")
    agent = LocalProjectService(root, config.load_config(root), actor="agent")
    agent.open_gate("old-work", "developer", note="Taken already")
    agent.take_gate("old-work", by="developer")
    first = agent.open_gate("login-fix", "developer", note="Open points: the name").gate
    second = agent.open_gate("dark-mode", "developer", note="Open points: the palette").gate
    assert second.opened_at >= first.opened_at
    return first, second


def signed_in(root, identity):
    client = TestClient(create_app(root), follow_redirects=False)
    token = auth.mint_token(root, identity)
    assert client.post(web.SIGNIN_PATH, data={"identity": identity, "token": token}).status_code == 303
    return client


def test_the_inbox_lists_the_open_gates_for_the_signed_in_role_newest_first(client, root):
    first, second = seed_gates(root)

    entries, empty = inbox(client.get(web.HOME_PATH).text)

    assert empty is None
    assert [slug for slug, _ in entries] == ["dark-mode", "login-fix"]
    newer, older = entries[0][1], entries[1][1]
    assert "Other" in newer and "Dark mode" in newer
    assert "Open points: the palette" in newer and second.opened_at in newer
    assert "Thing" in older and "Login fix" in older
    assert "Open points: the name" in older and first.opened_at in older
    assert "Old work" not in newer + older


def test_each_inbox_entry_links_to_its_project(client, root):
    seed_gates(root)

    html = client.get(web.HOME_PATH).text

    match = re.search(r'<section id="inbox">(.*?)</section>', html, re.S)
    for slug in ("dark-mode", "login-fix"):
        assert f'href="{web.PROJECT_PATH.format(slug=slug)}"' in match.group(1)


def test_the_requester_inbox_is_empty_when_nothing_waits_on_them(root):
    seed_gates(root)
    requester = signed_in(root, "requester")

    entries, empty = inbox(requester.get(web.HOME_PATH).text)

    assert entries == []
    assert empty == web.INBOX_EMPTY.format(identity="requester")


def test_a_gate_for_the_requester_reaches_only_the_requester(root):
    seed_gates(root)
    service(root).open_gate("old-work", "requester", note="Say which name you prefer")

    entries, _ = inbox(signed_in(root, "requester").get(web.HOME_PATH).text)
    assert [slug for slug, _ in entries] == ["old-work"]
    assert "Say which name you prefer" in entries[0][1]

    developer, _ = inbox(signed_in(root, "developer").get(web.HOME_PATH).text)
    assert [slug for slug, _ in developer] == ["dark-mode", "login-fix"]


def test_a_taken_gate_leaves_the_inbox(client, root):
    seed_gates(root)
    service(root).take_gate("dark-mode")

    entries, empty = inbox(client.get(web.HOME_PATH).text)

    assert [slug for slug, _ in entries] == ["login-fix"]
    assert empty is None


def test_an_inbox_entry_without_a_product_or_a_note_still_lists(client, root):
    seed_gates(root)
    svc = service(root)
    svc.create_project("Loose")
    svc.open_gate("loose", "developer")

    entries = dict(inbox(client.get(web.HOME_PATH).text)[0])

    assert set(entries) == {"dark-mode", "login-fix", "loose"}
    assert "Loose" in entries["loose"] and "No note" in entries["loose"]
    assert "No product" in entries["loose"]


def test_a_gate_on_an_inactive_project_is_not_inbox_work(client, root):
    seed_gates(root)
    service(root).shelve_project("dark-mode")

    entries, _ = inbox(client.get(web.HOME_PATH).text)

    assert [slug for slug, _ in entries] == ["login-fix"]


def test_the_landing_page_lists_every_product_with_its_counts(client, root):
    seed(root)

    response = client.get(web.HOME_PATH)

    assert response.status_code == 200
    table = rows(response.text)
    assert list(table) == ["other", "thing"]
    assert table["thing"] == ["Thing", "Ship the thing.", "3", "1"]
    assert table["other"] == ["Other", "", "0", "0"]


def test_the_counts_match_the_store(client, root):
    seed(root)
    with store_module.open_store(root) as store:
        items = WorkItems(store).list(product="thing")
    still_open = [item for item in items if item.status in ("open", "in-progress")]
    spawned = {item.project for item in items if item.project}
    active = [
        project
        for project in service(root).list_projects()
        if project.slug in spawned and project.status == "active"
    ]

    table = rows(client.get(web.HOME_PATH).text)

    assert table["thing"][2] == str(len(still_open)) == "3"
    assert table["thing"][3] == str(len(active)) == "1"


def test_a_project_made_outside_any_product_counts_for_none(client, root):
    seed(root)
    service(root).create_project("Loose", summary="no product")

    table = rows(client.get(web.HOME_PATH).text)

    assert table["thing"][3] == "1"
    assert table["other"][3] == "0"


def test_the_page_reads_the_store_on_every_request(client, root):
    seed(root)
    assert rows(client.get(web.HOME_PATH).text)["other"][2] == "0"

    with store_module.open_store(root) as store:
        WorkItems(store).add("other", "New work", today="2026-09-06")

    assert rows(client.get(web.HOME_PATH).text)["other"][2] == "1"


def test_a_daemon_without_products_says_so(client):
    response = client.get(web.HOME_PATH)

    assert response.status_code == 200
    assert rows(response.text) == {}
    assert "No products yet" in response.text


def test_each_product_links_to_its_page(client, root):
    seed(root)

    html = client.get(web.HOME_PATH).text

    for slug in ("thing", "other"):
        assert f'href="{web.PRODUCT_PATH.format(slug=slug)}"' in html


def test_the_products_page_needs_a_session(root):
    seed(root)
    browser = TestClient(create_app(root), follow_redirects=False)

    response = browser.get(web.HOME_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_a_directory_that_is_not_a_project_does_not_take_the_page_down(client, root):
    seed(root)
    broken = root / daemon.PROJECTS_DIRNAME / "broken"
    broken.mkdir(parents=True)
    (broken / "project.md").write_text("not a project\n")

    response = client.get(web.HOME_PATH)

    assert response.status_code == 200
    assert rows(response.text)["thing"] == ["Thing", "Ship the thing.", "3", "1"]
