"""The theme switch: auto, light or dark, kept in a cookie, no script.

The header holds a form that posts the choice to the theme path. The daemon
sets the theme cookie for a year and sends the browser back to the page it
came from, when that is a path on this site, else home. The route needs no
session, so the sign-in page carries the switch too. Every page depends on
the cookie, so every page varies on it.
"""

import re

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


# --- what the pages show -----------------------------------------------------------


def switch(html):
    """The theme the page's <html> names and the pressed button of its switch."""
    named = re.search(r'<html lang="en" data-theme="([^"]+)">', html).group(1)
    form = re.search(rf'<form class="theme" method="post" action="{web.THEME_PATH}"[^>]*>(.*?)</form>', html, re.S).group(1)
    pressed = re.findall(r'<button name="theme" value="([^"]+)" aria-pressed="true">', form)
    buttons = re.findall(r'<button name="theme" value="([^"]+)"', form)
    return named, pressed, buttons


@pytest.mark.parametrize("cookie, expected", [
    (None, "auto"), ("auto", "auto"), ("light", "light"), ("dark", "dark"), ("sepia", "auto"),
])
def test_the_sign_in_page_follows_the_cookie(client, cookie, expected):
    if cookie is not None:
        client.cookies.set(web.THEME_COOKIE, cookie)

    named, pressed, buttons = switch(client.get(web.SIGNIN_PATH).text)

    assert named == expected
    assert pressed == [expected]
    assert buttons == list(web.THEMES)


@pytest.mark.parametrize("cookie, expected", [(None, "auto"), ("dark", "dark"), ("bogus", "auto")])
def test_a_signed_in_page_follows_the_cookie(client, root, cookie, expected):
    sign_in(client, root)
    if cookie is not None:
        client.cookies.set(web.THEME_COOKIE, cookie)

    named, pressed, _ = switch(client.get(web.HOME_PATH).text)

    assert (named, pressed) == (expected, [expected])


def test_the_switch_sends_the_browser_back_to_the_page_it_is_on(client, root):
    sign_in(client, root)

    html = client.get(web.HOME_PATH + "?a=1&b=2").text

    assert '<input type="hidden" name="next" value="/?a=1&amp;b=2">' in html


def switch_next(html):
    return re.search(r'<input type="hidden" name="next" value="([^"]*)">', html).group(1)


@pytest.mark.parametrize("path", [
    web.TAKE_PATH.format(slug="nothing"),
    web.CHAT_PATH.format(slug="nothing"),
    web.START_AGENT_PATH.format(slug="nothing"),
    web.START_PROJECT_PATH.format(slug="nothing", item_id=1),
])
def test_a_page_that_answers_a_post_sends_the_switch_home(client, root, path):
    # A post without the session secret is refused with a page; a GET of the
    # posted path would not be that page, or no page at all.
    sign_in(client, root)

    response = client.post(path, data={})

    assert response.status_code == 403
    assert switch_next(response.text) == web.HOME_PATH


def test_a_refused_sign_in_post_sends_the_switch_home(client):
    response = client.post(web.SIGNIN_PATH, data={"identity": "developer", "token": "wrong"})

    assert response.status_code == 400
    assert switch_next(response.text) == web.HOME_PATH
