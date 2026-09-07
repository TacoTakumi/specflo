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
