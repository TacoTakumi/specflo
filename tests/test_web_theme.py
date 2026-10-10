"""The theme switch: auto, light or dark, kept in a cookie, no script.

The user page holds a form that posts the choice to the theme path; no
other page has one. The daemon sets the theme cookie for a year and sends
the browser back to the page it came from, when that is a path on this
site, else home. The route needs no session. Every page, the sign-in page
included, follows the cookie and varies on it; a browser with no choice
gets dark.
"""

import re

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import web
from specflo.daemon.app import create_app

from conftest import web_sign_in

ONE_YEAR = 31536000


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    return TestClient(create_app(root), follow_redirects=False)


def sign_in(client, root):
    web_sign_in(client, root)


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


def named(html):
    """The theme the page's <html> names."""
    return re.search(r'<html lang="en" data-theme="([^"]+)">', html).group(1)


def switches(html):
    return re.findall(rf'<form class="theme" method="post" action="{web.THEME_PATH}"[^>]*>(.*?)</form>', html, re.S)


def switch(html):
    """The theme the page's <html> names, the pressed button of its switch and every button."""
    (form,) = switches(html)
    pressed = re.findall(r'<button name="theme" value="([^"]+)" aria-pressed="true">', form)
    buttons = re.findall(r'<button name="theme" value="([^"]+)"', form)
    return named(html), pressed, buttons


@pytest.mark.parametrize("cookie, expected", [
    (None, "dark"), ("auto", "auto"), ("light", "light"), ("dark", "dark"), ("sepia", "dark"),
])
def test_the_sign_in_page_follows_the_cookie_and_has_no_switch(client, cookie, expected):
    if cookie is not None:
        client.cookies.set(web.THEME_COOKIE, cookie)

    html = client.get(web.SIGNIN_PATH).text

    assert named(html) == expected
    assert switches(html) == []


@pytest.mark.parametrize("cookie, expected", [(None, "dark"), ("dark", "dark"), ("bogus", "dark"), ("auto", "auto")])
def test_the_user_page_switch_follows_the_cookie(client, root, cookie, expected):
    sign_in(client, root)
    if cookie is not None:
        client.cookies.set(web.THEME_COOKIE, cookie)

    named_theme, pressed, buttons = switch(client.get(web.USER_PATH).text)

    assert (named_theme, pressed) == (expected, [expected])
    assert buttons == list(web.THEMES)


@pytest.mark.parametrize("cookie, expected", [(None, "dark"), ("light", "light")])
def test_a_signed_in_page_follows_the_cookie_and_has_no_switch(client, root, cookie, expected):
    sign_in(client, root)
    if cookie is not None:
        client.cookies.set(web.THEME_COOKIE, cookie)

    html = client.get(web.HOME_PATH).text

    assert named(html) == expected
    assert switches(html) == []


def switch_next(html):
    return re.search(r'<input type="hidden" name="next" value="([^"]*)">', switches(html)[0]).group(1)


def test_the_switch_sends_the_browser_back_to_the_user_page(client, root):
    sign_in(client, root)

    assert switch_next(client.get(web.USER_PATH + "?a=1").text) == web.USER_PATH


def test_the_switch_on_the_page_a_post_answers_with_goes_back_to_the_user_page(client, root):
    sign_in(client, root)

    response = client.post(
        web.USER_PATH,
        data={"session": client.cookies[web.SESSION_COOKIE], "op": web.ADD_TOKEN_OP, "name": "laptop"},
    )

    assert response.status_code == 200
    assert switch_next(response.text) == web.USER_PATH
