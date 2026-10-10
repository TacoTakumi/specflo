"""The web UI's look: the frame and markdown stylesheets the pages share.

The daemon serves two stylesheets from its assets directory with no session:
specflo.css, the page frame, whose every colour is a light-dark() token on
:root with a teal accent, and a vendored github-markdown.css for rendered
markdown, whose MIT notice sits in LICENSES.md beside it.
"""

import re

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import web
from specflo.daemon.app import create_app

from conftest import web_sign_in

STYLESHEETS = ("specflo.css", "github-markdown.css")
TEAL_ACCENT = "--accent:       light-dark(#0e7c7b, #4cc3bd);"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(daemon.prepare_root(tmp_path / "daemon")), follow_redirects=False)


@pytest.mark.parametrize("name", STYLESHEETS)
def test_each_stylesheet_is_served_without_a_session(client, name):
    response = client.get(f"{web.ASSETS_PATH}/{name}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")


def test_the_frame_accent_is_teal_in_both_themes():
    assert TEAL_ACCENT in (web.ASSETS_DIR / "specflo.css").read_text()


def test_the_markdown_stylesheet_keeps_its_mit_notice():
    first_line = (web.ASSETS_DIR / "github-markdown.css").read_text().splitlines()[0]
    notice = (web.ASSETS_DIR / "LICENSES.md").read_text()

    assert "github-markdown-css v5.9.0 | MIT License" in first_line
    assert "| github-markdown.css | github-markdown-css | 5.9.0 |" in notice
    assert "Permission is hereby granted, free of charge" in notice


# --- the frame every page shares -------------------------------------------------


def sign_in(client, root):
    web_sign_in(client, root)


def header(html):
    return re.search(r'<header class="site-header">(.*?)</header>', html, re.S).group(1)


def test_the_sign_in_page_links_the_stylesheets_and_shows_the_brand(client):
    html = client.get(web.SIGNIN_PATH).text

    for name in ("github-markdown.css", "specflo.css", "daemon.css"):
        assert f'<link rel="stylesheet" href="{web.ASSETS_PATH}/{name}">' in html
    assert re.search(rf'<a class="brand" href="{web.HOME_PATH}">\s*<svg[^>]*>.*?</svg>\s*specflo\s*</a>', header(html), re.S)
    assert 'class="identity"' not in header(html) and 'class="settings"' not in header(html)
    assert f'href="{web.POOL_PATH}"' not in header(html)
    assert 'class="theme"' not in header(html) and 'class="signout"' not in header(html)


def test_a_signed_in_page_shows_the_links_the_user_and_sign_out_in_the_header(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    client = TestClient(create_app(root), follow_redirects=False)
    sign_in(client, root)

    top = header(client.get(web.HOME_PATH).text)

    assert f'<a class="brand" href="{web.HOME_PATH}">' in top
    assert f'<a href="{web.POOL_PATH}">Pool</a>' in top
    # the user's name, then a link to their page, then sign-out
    assert re.search(
        rf'<span class="identity">Signed in as <strong>developer</strong></span>\s*'
        rf'<a class="settings" href="{web.USER_PATH}">Settings</a>\s*<form class="signout"',
        top,
    )
    for gone in ('class="theme"', 'class="tokens"', 'class="password"'):
        assert gone not in top


def test_every_stylesheet_a_page_links_is_served(client):
    html = client.get(web.SIGNIN_PATH).text
    for href in re.findall(r'<link rel="stylesheet" href="([^"]+)">', html):
        assert client.get(href).status_code == 200, href


# --- colours come from the tokens --------------------------------------------------

COLOUR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\(")


def test_no_template_holds_a_colour():
    for path in sorted(web.TEMPLATES_DIR.glob("*.html")):
        assert not COLOUR.findall(path.read_text()), path.name


def test_the_daemon_stylesheet_holds_colours_only_in_its_token_block():
    sheet = (web.ASSETS_DIR / "daemon.css").read_text()
    tokens = re.search(r"/\* -+ Colour tokens -+ \*/\s*:root \{[^}]*\}", sheet)
    assert tokens, "daemon.css has no token block"
    for line in tokens.group(0).splitlines()[2:-1]:
        assert re.fullmatch(r"\s*--[a-z-]+:\s*light-dark\(#[0-9a-f]{6}, #[0-9a-f]{6}\);", line), line
    rest = sheet.replace(tokens.group(0), "")
    assert not COLOUR.findall(rest)


def test_a_number_cell_is_not_styled_as_the_frames_count_pill():
    for path in sorted(web.TEMPLATES_DIR.glob("*.html")):
        assert '<td class="count"' not in path.read_text(), path.name
