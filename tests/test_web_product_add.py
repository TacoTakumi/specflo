"""The new product page: a developer adds a product from the browser.

The products page links to the page for a developer only. The page holds a
form with the fields name, slug, repo and vision; the slug field says how an
empty slug is derived, and the page loads no script of its own. Any other
role gets a 403 page that names the role that may add a product.
"""

import json
import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import routes, web
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import Products

from conftest import web_sign_in

SLUG_HINT = "Leave it empty to derive the slug from the name, e.g. My Thing -&gt; my-thing."


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


def signed_in(root, role):
    client = TestClient(create_app(root), follow_redirects=False)
    web_sign_in(client, root, role)
    return client


@pytest.fixture
def developer(root):
    return signed_in(root, "developer")


@pytest.fixture
def requester(root):
    return signed_in(root, "requester")


def new_product_link(html):
    return f'href="{web.NEW_PRODUCT_PATH}"' in html


def test_the_products_page_links_to_the_new_product_page_for_a_developer_only(developer, requester):
    assert new_product_link(developer.get(web.HOME_PATH).text)
    assert not new_product_link(requester.get(web.HOME_PATH).text)


def test_a_developer_gets_the_form_with_its_four_fields(developer):
    response = developer.get(web.NEW_PRODUCT_PATH)

    assert response.status_code == 200
    html = response.text
    assert f'action="{web.NEW_PRODUCT_PATH}"' in html
    for field in ("name", "slug", "repo"):
        assert re.search(rf'<input[^>]* name="{field}"', html), field
    assert re.search(r'<textarea[^>]* name="vision"', html)
    assert re.search(r'<input type="hidden" name="session" value="[^"]+"', html)


def test_the_slug_field_says_how_an_empty_slug_is_derived(developer):
    html = developer.get(web.NEW_PRODUCT_PATH).text

    assert SLUG_HINT in html
    assert html.index('name="slug"') < html.index(SLUG_HINT)


def test_the_page_loads_no_script_of_its_own(developer):
    page = developer.get(web.NEW_PRODUCT_PATH).text
    landing = developer.get(web.HOME_PATH).text

    assert page.count("<script") == landing.count("<script")


def test_a_requester_gets_a_403_that_names_the_developer_role(requester):
    response = requester.get(web.NEW_PRODUCT_PATH)

    assert response.status_code == 403
    assert "the developer role" in unescape(response.text)
    assert 'name="vision"' not in response.text


# --- the save -------------------------------------------------------------------


REPO = "git@host:me/thing.git"


def session_of(client):
    html = client.get(web.NEW_PRODUCT_PATH).text
    return re.search(r'name="session" value="([^"]+)"', html).group(1)


def audit_records(root):
    path = root / routes.AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        (r["user"], r["role"], r["operation"], r["id"])
        for r in (json.loads(line) for line in path.read_text().splitlines())
    ]


def stored(root):
    with store_module.open_store(root) as store:
        return Products(store).list()


def test_a_save_adds_the_product_with_its_repo_and_vision(developer, root):
    response = developer.post(web.NEW_PRODUCT_PATH, data={
        "session": session_of(developer), "name": "My Thing", "slug": "",
        "repo": REPO, "vision": "Be the thing.",
    })

    assert response.status_code == 303
    assert response.headers["location"] == web.PRODUCT_PATH.format(slug="my-thing")
    [product] = stored(root)
    assert (product.name, product.slug, product.repo, product.vision) == (
        "My Thing", "my-thing", REPO, "Be the thing."
    )
    assert audit_records(root) == [
        ("developer", "developer", "product_add", "my-thing"),
        ("developer", "developer", "product_set_vision", "my-thing"),
    ]


def test_a_save_without_a_vision_or_repo_writes_one_audit_line(developer, root):
    response = developer.post(web.NEW_PRODUCT_PATH, data={
        "session": session_of(developer), "name": "Bare", "slug": "bare-one",
        "repo": "", "vision": "",
    })

    assert response.status_code == 303
    assert response.headers["location"] == web.PRODUCT_PATH.format(slug="bare-one")
    [product] = stored(root)
    assert (product.slug, product.repo, product.vision) == ("bare-one", None, "")
    assert audit_records(root) == [("developer", "developer", "product_add", "bare-one")]
