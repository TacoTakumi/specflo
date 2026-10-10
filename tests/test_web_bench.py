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


# --- the batch files -----------------------------------------------------------


def batch_file(batch_name, name):
    return f"{MENU}/results/{batch_name}/{name}"


@pytest.mark.parametrize("identity", ["requester", "developer"])
def test_batch_files_for_each_identity(client, root, identity):
    folder = batch(root, "pilot")
    (folder / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    sign_in(client, root, identity)
    for name, kind, body in (
        ("index.html", "text/html", b"<h1>pilot</h1>"),
        ("chart.png", "image/png", b"\x89PNG\r\n\x1a\nfake"),
        ("chart.txt", "text/plain", b"pass rate 0.5"),
    ):
        response = client.get(batch_file("pilot", name))
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith(kind)
        assert response.content == body


SECRET = "planted-secret-5521"


def test_batch_refuses_what_is_not_a_chart(client, root):
    folder = batch(root, "pilot")
    plant(folder / "data.json", SECRET)
    plant(folder / "nested" / "chart.txt", SECRET)
    plant(root / "bench" / "results" / "secret.txt", SECRET)
    plant(root / "bench" / "state" / "loaded-by-bench.json", SECRET)
    sign_in(client, root, "developer")
    for path in (
        batch_file("pilot", "data.json"),
        f"{MENU}/results/pilot/nested/chart.txt",
        batch_file("pilot", "nested%2Fchart.txt"),
        batch_file("pilot", "..%2Fsecret.txt"),
        batch_file("pilot", "%2e%2e%2fsecret.txt"),
        batch_file("..", "secret.txt"),
        batch_file("%2e%2e", "secret.txt"),
        batch_file("..%2F..%2Fstate", "loaded-by-bench.json"),
        batch_file("pilot", "missing.txt"),
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert SECRET not in response.text, path


def test_batch_refuses_a_link_out_of_the_bench(client, root, tmp_path):
    outside = plant(tmp_path / "outside" / "secret.txt", SECRET)
    folder = batch(root, "pilot")
    (folder / "linked.txt").symlink_to(outside)
    (root / "bench" / "results" / "linked-batch").symlink_to(outside.parent)
    sign_in(client, root, "developer")
    for path in (batch_file("pilot", "linked.txt"), batch_file("linked-batch", "secret.txt")):
        response = client.get(path)
        assert response.status_code == 404, path
        assert SECRET not in response.text, path


def test_batch_file_needs_a_session(client, root):
    batch(root, "pilot")
    response = client.get(batch_file("pilot", "index.html"))
    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH
