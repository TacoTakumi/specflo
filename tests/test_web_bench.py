"""The bench pages: the model bench's results and run files, for a signed-in browser.

The bench writes under ``<daemon root>/bench``: a folder per result batch in
``results`` and a folder per run in ``runs``. The menu page lists the batches
for either identity and the runs for the developer alone. Each test plants the
files it reads under a fresh daemon root.
"""

from __future__ import annotations

import os
import re

import pytest
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.daemon import auth, web
from specflo.daemon.app import create_app

MENU = "/bench"
NO_RESULTS = "No bench results yet."


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    return TestClient(create_app(root), follow_redirects=False)


def sign_in(client, root, identity):
    token = auth.mint_token(root, identity)
    response = client.post(web.SIGNIN_PATH, data={"identity": identity, "token": token})
    assert response.status_code == 303, response.text
    return client


def plant(path, text="", mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def batch(root, name, mtime=None):
    """A result batch folder with an index page and one chart beside it."""
    folder = root / "bench" / "results" / name
    plant(folder / "index.html", f"<h1>{name}</h1>", mtime)
    plant(folder / "chart.txt", "pass rate 0.5")
    return folder


def links(html):
    return re.findall(r'href="([^"]*)"', html)


# --- the menu ------------------------------------------------------------------


@pytest.mark.parametrize("identity", ["requester", "developer"])
def test_menu_nav_link_on_every_page(client, root, identity):
    sign_in(client, root, identity)
    for path in (web.HOME_PATH, MENU):
        response = client.get(path)
        assert response.status_code == 200, response.text
        nav = re.search(r'<nav class="site-nav".*?</nav>', response.text, re.S).group(0)
        assert f'href="{MENU}"' in nav


def test_menu_lists_batches_newest_first(client, root):
    batch(root, "older", mtime=1_000_000)
    batch(root, "newer", mtime=2_000_000)
    plant(root / "bench" / "results" / "no-index" / "chart.txt", "x")
    sign_in(client, root, "requester")
    html = client.get(MENU).text
    batches = [link for link in links(html) if link.startswith(MENU + "/results/")]
    assert batches == [
        MENU + "/results/newer/index.html",
        MENU + "/results/older/index.html",
    ]
    assert "no-index" not in html
    assert NO_RESULTS not in html


def test_menu_without_bench_directory_says_no_results(client, root):
    sign_in(client, root, "developer")
    response = client.get(MENU)
    assert response.status_code == 200
    assert NO_RESULTS in response.text


def test_menu_needs_a_session(client):
    response = client.get(MENU)
    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
