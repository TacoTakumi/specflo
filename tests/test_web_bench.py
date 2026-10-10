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
from specflo.daemon import web
from specflo.daemon.app import create_app

from conftest import web_sign_in

MENU = "/bench"
NO_RESULTS = "No bench results yet."


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def client(root):
    return TestClient(create_app(root), follow_redirects=False)


def sign_in(client, root, identity):
    web_sign_in(client, root, identity)
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
    page = client.get(batch_file("pilot", "index.html"))
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "<h1>Model bench: pilot</h1>" in page.text
    for name, kind, body in (
        ("chart.png", "image/png", b"\x89PNG\r\n\x1a\nfake"),
        ("chart.txt", "text/plain", b"pass rate 0.5"),
    ):
        response = client.get(batch_file("pilot", name))
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith(kind)
        assert response.content == body


SECRET_BEFORE = "planted-secret-7730"


DISCLOSURE = (
    "level: quick; compared variable: harness; excluded runs: 0\n"
    "entry-a / pi: arm.entry=entry-a; arm.harness=pi; runs=2\n"
    "entry-a / claude-code: arm.entry=entry-a; arm.harness=claude-code; runs=2\n"
)


def graphed_batch(root, name):
    """A result batch folder with one comparison: its two graphs and the disclosure beside each."""
    folder = batch(root, name)
    for kind in ("pass-rate", "metrics"):
        (folder / f"quick-by-harness-{kind}.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        plant(folder / f"quick-by-harness-{kind}.txt", DISCLOSURE)
    return folder


def test_batch_page_has_the_daemon_top_bar(client, root):
    graphed_batch(root, "pilot")
    sign_in(client, root, "developer")
    response = client.get(batch_file("pilot", "index.html"))
    assert response.status_code == 200
    assert f'<a href="{web.BENCH_PATH}">Bench</a>' in response.text
    assert re.search(
        rf'<span class="identity">Signed in as <strong>developer</strong></span>\s*'
        rf'<a class="settings" href="{web.USER_PATH}">Settings</a>\s*<form class="signout"',
        response.text,
    )


def test_batch_page_takes_the_daemon_theme(client, root):
    graphed_batch(root, "pilot")
    sign_in(client, root, "developer")
    page = client.get(batch_file("pilot", "index.html")).text
    assert '<html lang="en" data-theme="dark">' in page
    assert 'name="theme"' not in page
    client.cookies.set(web.THEME_COOKIE, "light")
    page = client.get(batch_file("pilot", "index.html")).text
    assert '<html lang="en" data-theme="light">' in page
    assert 'name="theme"' not in page


def test_batch_page_shows_the_results_and_its_graphs_load(client, root):
    graphed_batch(root, "pilot")
    sign_in(client, root, "developer")
    page = client.get(batch_file("pilot", "index.html")).text
    for text in ("<h1>Model bench: pilot</h1>", "How to read a graph", 'id="level-quick"',
                 '<tr class="differs"><th>arm.harness</th>', "What the metrics mean"):
        assert text in page, text
    sources = re.findall(r'<img src="([^"]+)"', page)
    assert sources == ["quick-by-harness-pass-rate.png", "quick-by-harness-metrics.png"]
    for src in sources:
        image = client.get(batch_file("pilot", src))
        assert image.status_code == 200, src
        assert image.headers["content-type"].startswith("image/png")


def test_batch_page_links_the_bench_stylesheet(client, root):
    graphed_batch(root, "pilot")
    sign_in(client, root, "developer")
    page = client.get(batch_file("pilot", "index.html")).text
    href = f"{web.ASSETS_PATH}/bench.css"
    assert f'<link rel="stylesheet" href="{href}">' in page
    sheet = client.get(href)
    assert sheet.status_code == 200
    assert sheet.headers["content-type"].startswith("text/css")


def test_a_folder_without_an_index_page_is_no_batch(client, root):
    folder = root / "bench" / "results" / "half"
    plant(folder / "quick-by-harness-pass-rate.png", "png")
    sign_in(client, root, "developer")
    assert "half" not in client.get(MENU).text
    response = client.get(batch_file("half", "index.html"))
    assert response.status_code == 404
    assert "No such bench file." in response.text


def test_batch_page_leaves_out_a_linked_disclosure(client, root, tmp_path):
    outside = plant(tmp_path / "outside" / "auth.json", SECRET_BEFORE)
    folder = graphed_batch(root, "pilot")
    for kind in ("pass-rate", "metrics"):
        (folder / f"quick-by-harness-{kind}.txt").unlink()
        (folder / f"quick-by-harness-{kind}.txt").symlink_to(outside)
    sign_in(client, root, "requester")
    assert client.get(batch_file("pilot", "quick-by-harness-metrics.txt")).status_code == 404
    page = client.get(batch_file("pilot", "index.html"))
    assert page.status_code == 200
    assert SECRET_BEFORE not in page.text


def test_batch_page_of_a_linked_or_missing_batch_is_a_404(client, root, tmp_path):
    outside = graphed_batch(tmp_path / "elsewhere", "pilot")
    (root / "bench" / "results").mkdir(parents=True, exist_ok=True)
    (root / "bench" / "results" / "linked").symlink_to(outside)
    sign_in(client, root, "developer")
    for path in (batch_file("linked", "index.html"), batch_file("missing", "index.html"),
                 batch_file("%2e%2e", "index.html")):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "No such bench file." in response.text, path
    # An encoded slash makes a path no route matches, so it never reaches the batch route.
    assert client.get(batch_file("..%2Fresults", "index.html")).status_code == 404


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


# --- the runs ------------------------------------------------------------------


def run_file(run_name, name):
    return f"{MENU}/runs/{run_name}/{name}"


def run(root, name, session=True):
    """A run folder with its record, its normalised session when asked, and its raw output."""
    folder = root / "bench" / "runs" / name
    plant(folder / "record.json", f'{{"run": "{name}"}}')
    if session:
        plant(folder / "session.norm.json", '{"calls": []}')
    plant(folder / "harness.out", SECRET)
    plant(folder / "pi-agent" / "auth.json", SECRET)
    return folder


def test_run_list_on_the_developer_menu_only(client, root):
    run(root, "run-a")
    run(root, "run-b", session=False)
    plant(root / "bench" / "runs" / "probes" / "probe.json", "{}")
    sign_in(client, root, "developer")
    found = [link for link in links(client.get(MENU).text) if link.startswith(MENU + "/runs/")]
    assert found == [
        run_file("run-a", "record.json"),
        run_file("run-a", "session.norm.json"),
        run_file("run-b", "record.json"),
    ]
    requester = TestClient(client.app, follow_redirects=False)
    html = sign_in(requester, root, "requester").get(MENU).text
    assert "run-a" not in html and "run-b" not in html
    assert MENU + "/runs/" not in html


def test_run_files_for_the_developer(client, root):
    run(root, "run-a")
    sign_in(client, root, "developer")
    for name, body in (("record.json", b'{"run": "run-a"}'), ("session.norm.json", b'{"calls": []}')):
        response = client.get(run_file("run-a", name))
        assert response.status_code == 200, name
        assert response.headers["content-type"].startswith("application/json")
        assert response.content == body


def test_run_files_refused_to_the_requester(client, root):
    run(root, "run-a")
    sign_in(client, root, "requester")
    for name in ("record.json", "session.norm.json"):
        response = client.get(run_file("run-a", name))
        assert response.status_code == 403, name
        assert '"calls"' not in response.text and '"run": ' not in response.text


def test_run_refuses_what_is_not_a_record(client, root, tmp_path):
    folder = run(root, "run-a")
    plant(root / "bench" / "state" / "loaded-by-bench.json", SECRET)
    outside = plant(tmp_path / "outside" / "record.json", SECRET)
    (root / "bench" / "runs" / "linked-run").symlink_to(outside.parent)
    (folder / "pi-agent" / "record.json").write_text(SECRET)
    sign_in(client, root, "developer")
    for path in (
        run_file("run-a", "harness.out"),
        f"{MENU}/runs/run-a/pi-agent/auth.json",
        run_file("run-a", "pi-agent%2Fauth.json"),
        run_file("run-a", "pi-agent%2Frecord.json"),
        run_file("..%2Fstate", "loaded-by-bench.json"),
        run_file("%2e%2e", "record.json"),
        run_file("linked-run", "record.json"),
        run_file("missing", "record.json"),
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert SECRET not in response.text, path


def test_run_refuses_a_link_to_a_file_beside_it(client, root):
    folder = run(root, "run-a", session=False)
    (folder / "session.norm.json").symlink_to(folder / "harness.out")
    other = run(root, "run-b")
    (other / "record.json").unlink()
    (other / "record.json").symlink_to(other / "harness.out")
    sign_in(client, root, "developer")
    for path in (run_file("run-a", "session.norm.json"), run_file("run-b", "record.json")):
        response = client.get(path)
        assert response.status_code == 404, path
        assert SECRET not in response.text, path


def test_batch_refuses_a_link_to_a_file_beside_it(client, root):
    folder = batch(root, "pilot")
    plant(folder / "data.json", SECRET)
    (folder / "chart.txt").unlink()
    (folder / "chart.txt").symlink_to(folder / "data.json")
    sign_in(client, root, "developer")
    response = client.get(batch_file("pilot", "chart.txt"))
    assert response.status_code == 404
    assert SECRET not in response.text


def test_lookup_failure_is_a_404(client, root):
    run(root, "run-a")
    batch(root, "pilot")
    (root / "bench" / "runs" / "loop").symlink_to(root / "bench" / "runs" / "loop")
    long = "x" * 300
    sign_in(client, root, "developer")
    for path in (
        batch_file("pilot", long + ".txt"),
        batch_file(long, "index.html"),
        run_file(long, "record.json"),
        run_file("loop", "record.json"),
    ):
        assert client.get(path).status_code == 404, path


def test_run_file_needs_a_session(client, root):
    run(root, "run-a")
    response = client.get(run_file("run-a", "record.json"))
    assert response.status_code == 303
    assert response.headers["location"] == web.SIGNIN_PATH


def test_run_routes_are_no_static_mount(client):
    from starlette.routing import Mount

    mounts = [route.path for route in client.app.routes if isinstance(route, Mount)]
    assert not [path for path in mounts if path.startswith(MENU)]
