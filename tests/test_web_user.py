"""The user page: the signed-in user's theme switch, personal tokens and password on one page.

The password section is there only for a session opened with a password.
The old My tokens and change password pages send the browser to the
matching section. A browser with no session is sent to sign-in, from the
page and from the old paths alike.
"""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, web
from specflo.daemon.app import create_app

from conftest import web_sign_in


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def app(root):
    return create_app(root)


def signed_in(app, root, name="rob", role="developer"):
    client = TestClient(app, follow_redirects=False)
    web_sign_in(client, root, role, name)
    return client


def proxy_signed_in(app, root, name="rob", role="developer"):
    """A client on a session opened as a proxy sign-in opens one: no password asked."""
    web_sign_in(TestClient(app, follow_redirects=False), root, role, name)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(web.SESSION_COOKIE, auth.open_session(root, name, "proxy", time.time()))
    return client


def content(html):
    """The page's own content, without the site header."""
    return re.search(r'<main class="container">(.*)</main>', html, re.S).group(1)


# The page's top-level sections, in page order.
SECTIONS = ("theme", "tokens", "password")


def section(html, name):
    """The page's section ``name``, up to the next top-level section."""
    parts = re.split(rf'(?=<section id="(?:{"|".join(SECTIONS)})">)', content(html))
    (match,) = [part for part in parts if part.startswith(f'<section id="{name}">')]
    return match


@pytest.mark.parametrize("role", ["developer", "requester"])
def test_the_page_names_the_user_and_has_the_theme_and_tokens_sections(app, root, role):
    client = signed_in(app, root, "alice", role)

    response = client.get(web.USER_PATH)

    assert response.status_code == 200
    page = content(response.text)
    assert "<h1>User settings for alice</h1>" in page
    assert '<section id="theme">' in page and '<section id="tokens">' in page
    assert web.NO_TOKENS in section(response.text, "tokens")


def test_the_theme_section_has_the_switch_and_comes_back_to_the_page(app, root):
    client = signed_in(app, root)
    client.cookies.set(web.THEME_COOKIE, "light")

    theme = section(client.get(web.USER_PATH).text, "theme")

    form = re.search(rf'<form class="theme" method="post" action="{web.THEME_PATH}"[^>]*>(.*?)</form>', theme, re.S)
    assert form, theme
    assert f'name="next" value="{web.USER_PATH}"' in form.group(1)
    assert re.findall(r'<button name="theme" value="([^"]+)"', form.group(1)) == list(web.THEMES)
    assert re.findall(r'value="([^"]+)" aria-pressed="true"', form.group(1)) == ["light"]


def test_the_switch_on_the_page_sets_the_cookie_and_returns_to_the_page(app, root):
    client = signed_in(app, root)

    response = client.post(web.THEME_PATH, data={"theme": "light", "next": web.USER_PATH})

    assert response.status_code == 303
    assert response.headers["location"] == web.USER_PATH
    assert client.cookies[web.THEME_COOKIE] == "light"


def test_the_old_tokens_page_sends_the_browser_to_the_tokens_section(app, root):
    client = signed_in(app, root)

    response = client.get(web.OLD_TOKENS_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.USER_PATH + "#tokens"


def test_a_password_session_has_the_password_section_after_the_tokens(app, root):
    client = signed_in(app, root)

    html = client.get(web.USER_PATH).text

    page = content(html)
    assert page.index('<section id="theme">') < page.index('<section id="tokens">') < page.index('<section id="password">')
    assert f'name="op" value="{web.CHANGE_PASSWORD_OP}"' in section(html, "password")


def test_a_proxy_session_has_the_theme_and_tokens_sections_and_no_password_section(app, root):
    client = proxy_signed_in(app, root)

    page = content(client.get(web.USER_PATH).text)

    assert '<section id="theme">' in page and '<section id="tokens">' in page
    assert '<section id="password">' not in page


@pytest.mark.parametrize("make", [signed_in, proxy_signed_in])
def test_the_old_password_page_sends_the_browser_to_the_password_section(app, root, make):
    client = make(app, root)

    response = client.get(web.OLD_PASSWORD_PATH)

    assert response.status_code == 303
    assert response.headers["location"] == web.USER_PATH + "#password"


@pytest.mark.parametrize("path", [web.USER_PATH, web.OLD_TOKENS_PATH, web.OLD_PASSWORD_PATH])
def test_a_browser_with_no_session_is_sent_to_sign_in(app, path):
    client = TestClient(app, follow_redirects=False)

    response = client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_the_header_names_the_user_not_the_role_and_links_to_the_page(app, root):
    client = signed_in(app, root, "alice", "developer")

    html = client.get(web.HOME_PATH).text

    header = re.search(r'<header class="site-header">(.*?)</header>', html, re.S).group(1)
    assert "Signed in as <strong>alice</strong>" in header
    assert f'<a class="settings" href="{web.USER_PATH}">Settings</a>' in header
    assert "developer" not in header


def test_every_form_on_the_page_posts_to_the_page(app, root):
    client = signed_in(app, root)
    auth.mint_user_token(root, "rob", "laptop")

    page = content(client.get(web.USER_PATH).text)

    actions = re.findall(r'<form [^>]*action="([^"]*)"', page)
    ops = re.findall(r'<input type="hidden" name="op" value="([^"]*)">', page)
    assert actions == [web.THEME_PATH] + [web.USER_PATH] * 3
    assert ops == [web.ADD_TOKEN_OP, web.REVOKE_TOKEN_OP, web.CHANGE_PASSWORD_OP]


def test_a_token_create_answers_at_the_page_and_the_page_reloads(app, root):
    client = signed_in(app, root)

    response = client.post(
        web.USER_PATH,
        data={"session": client.cookies[web.SESSION_COOKIE], "op": web.ADD_TOKEN_OP, "name": "laptop"},
    )

    assert response.status_code == 200
    assert str(response.url).endswith(web.USER_PATH)
    assert web.SHOWN_ONCE in response.text
    reload = client.get(response.url)
    assert reload.status_code == 200
    assert web.SHOWN_ONCE not in reload.text


@pytest.mark.parametrize("op", ["", "delete_user", "add_token "])
def test_an_unknown_op_is_refused_and_changes_nothing(app, root, op):
    client = signed_in(app, root)

    response = client.post(
        web.USER_PATH, data={"session": client.cookies[web.SESSION_COOKIE], "op": op, "name": "laptop"},
    )

    assert response.status_code == 400
    assert "No such action." in response.text
    assert auth.list_user_tokens(root, "rob") == []
