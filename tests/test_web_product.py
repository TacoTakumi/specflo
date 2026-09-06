"""The product page: vision, pieces, backlog, and projects, with the archive folded away.

One product's page shows its vision text, the pieces it declares, its
backlog in order, and the projects spawned from its work items. Complete
projects are the archive: hidden by default and shown when the archived
filter is on, through a plain link that changes nothing on the daemon.
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
