"""The theme switch: auto, light or dark, kept in a cookie, no script.

The header holds a form that posts the choice to the theme path. The daemon
sets the theme cookie for a year and sends the browser back to the page it
came from, when that is a path on this site, else home. The route needs no
session, so the sign-in page carries the switch too. Every page depends on
the cookie, so every page varies on it.
"""

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, web
from specflo.daemon.app import create_app

ONE_YEAR = 31536000


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    return TestClient(create_app(root), follow_redirects=False)


def sign_in(client, root):
    token = auth.mint_token(root, "developer")
    response = client.post(web.SIGNIN_PATH, data={"identity": "developer", "token": token})
    assert response.status_code == 303, response.text


@pytest.mark.parametrize("theme", web.THEMES)
def test_a_choice_sets_the_cookie_for_a_year_and_goes_back(client, theme):
    response = client.post(web.THEME_PATH, data={"theme": theme, "next": "/products/thing?x=1"})

    assert response.status_code == 303
    assert response.headers["location"] == "/products/thing?x=1"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{web.THEME_COOKIE}={theme};")
    for part in (f"Max-Age={ONE_YEAR}", "Path=/", "SameSite=lax", "HttpOnly"):
        assert part in cookie


@pytest.mark.parametrize("target", [
    "//evil.example/", "http://evil.example/", "/\\evil.example", "/a\nb", "/a\x7fb", "", "products",
])
def test_a_next_off_this_site_goes_home(client, target):
    response = client.post(web.THEME_PATH, data={"theme": "dark", "next": target})

    assert response.status_code == 303
    assert response.headers["location"] == web.HOME_PATH


def test_a_post_with_no_next_goes_home(client):
    response = client.post(web.THEME_PATH, data={"theme": "light"})

    assert response.headers["location"] == web.HOME_PATH


@pytest.mark.parametrize("theme", ["", "sepia", "Dark"])
def test_an_unknown_choice_is_refused(client, theme):
    response = client.post(web.THEME_PATH, data={"theme": theme, "next": "/"})

    assert response.status_code == 400
    assert "set-cookie" not in response.headers


def test_the_sign_in_page_varies_on_the_cookie(client):
    assert "Cookie" in client.get(web.SIGNIN_PATH).headers["vary"]


def test_a_signed_in_page_varies_on_the_cookie(client, root):
    sign_in(client, root)

    assert "Cookie" in client.get(web.HOME_PATH).headers["vary"]
