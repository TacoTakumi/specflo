"""The user page: the signed-in user's theme switch and personal tokens on one page.

The old My tokens page sends the browser to the page's tokens section. A
browser with no session is sent to sign-in, from the page and from the old
path alike.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import web
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


def content(html):
    """The page's own content, without the site header."""
    return re.search(r'<main class="container">(.*)</main>', html, re.S).group(1)


# The page's top-level sections, in page order.
SECTIONS = ("theme", "tokens")


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
    assert "<h1>alice</h1>" in page
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


@pytest.mark.parametrize("path", [web.USER_PATH, web.OLD_TOKENS_PATH])
def test_a_browser_with_no_session_is_sent_to_sign_in(app, path):
    client = TestClient(app, follow_redirects=False)

    response = client.get(path)

    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
