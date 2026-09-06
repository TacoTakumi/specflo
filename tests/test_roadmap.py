"""The roadmap: a read view over a product, its vision then its backlog in order.

Nothing is written for a roadmap. It is the product's vision text followed
by its work items in backlog order, read from the store on every call, so
adding an item changes the view on its own. The daemon serves it on the
product's route behind the token guard and the CLI prints it.
"""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import PRODUCTS_PATH, Products, RemoteProducts, Roadmap
from specflo.daemon.workitems import WorkItems
from specflo.errors import SpecfloError

runner = CliRunner()

VISION = "Ship the thing.\nThen keep it shipping.\n"


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def token(root):
    return auth.mint_token(root, "developer")


@pytest.fixture
def client(root, token):
    client = TestClient(create_app(root))
    client.headers["Authorization"] = f"Bearer {token}"
    return client


def seed(root):
    """Product ``thing`` with a vision, piece ``web``, and three items in order."""
    with store_module.open_store(root) as store:
        products = Products(store)
        products.add("Thing", slug="thing", today="2026-09-06")
        products.set_vision("thing", VISION)
        products.add_piece("thing", "web")
        products.add("Other", slug="other", today="2026-09-06")
        items = WorkItems(store)
        items.add("thing", "Fix the login", piece="web", today="2026-09-06")
        items.add("thing", "Tidy the readme", kind="idea", dev_path="one-prompt", today="2026-09-06")
        items.add("thing", "Offline mode", kind="roadmap", today="2026-09-06")
        items.add("other", "Elsewhere", today="2026-09-06")
        items.set_status(2, "done")


# --- the view in-process -------------------------------------------------------


def test_the_roadmap_is_the_vision_then_the_backlog_in_order(root):
    seed(root)
    with store_module.open_store(root) as store:
        roadmap = Products(store).roadmap("thing")
        assert isinstance(roadmap, Roadmap)
        assert roadmap.product == Products(store).show("thing")
        assert roadmap.product.vision == VISION
        assert [i.title for i in roadmap.items] == ["Fix the login", "Tidy the readme", "Offline mode"]
        assert [i.status for i in roadmap.items] == ["open", "done", "open"]
        assert roadmap.items == WorkItems(store).list(product="thing")

        # Adding an item changes the view; nothing else is written.
        WorkItems(store).add("thing", "Fourth", today="2026-09-06")
        assert [i.title for i in Products(store).roadmap("thing").items][-1] == "Fourth"

        assert Products(store).roadmap("other").items[0].title == "Elsewhere"
        with pytest.raises(SpecfloError, match="No product 'nope'"):
            Products(store).roadmap("nope")


# --- the route and the remote client -------------------------------------------


def test_the_roadmap_route_returns_the_same_data(client, root):
    seed(root)

    response = client.get(f"{PRODUCTS_PATH}/thing/roadmap")
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["product"]["slug"] == "thing" and result["product"]["vision"] == VISION
    assert [i["id"] for i in result["items"]] == [1, 2, 3]

    assert client.post("/api/workitems", json={"product": "thing", "title": "Fourth"}).status_code == 200
    assert [i["id"] for i in client.get(f"{PRODUCTS_PATH}/thing/roadmap").json()["result"]["items"]] == [1, 2, 3, 5]

    unknown = client.get(f"{PRODUCTS_PATH}/nope/roadmap")
    assert unknown.status_code == 400 and "No product 'nope'" in unknown.json()["detail"]
    assert TestClient(create_app(root)).get(f"{PRODUCTS_PATH}/thing/roadmap").status_code == 401


def test_the_remote_client_rebuilds_the_roadmap(root, token):
    seed(root)
    remote = RemoteProducts("http://testserver", token, client=TestClient(create_app(root)))

    roadmap = remote.roadmap("thing")
    assert isinstance(roadmap, Roadmap)
    with store_module.open_store(root) as store:
        assert roadmap == Products(store).roadmap("thing")
    with pytest.raises(SpecfloError, match="No product 'nope'"):
        remote.roadmap("nope")


# --- the CLI verb ------------------------------------------------------------


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    seed(live_daemon["root"])
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    return root


def test_product_roadmap_prints_the_vision_then_the_items(checkout):
    result = runner.invoke(app, ["product", "roadmap", "thing"])
    assert result.exit_code == 0, result.output
    assert result.output == (
        "Roadmap: Thing (thing)\n"
        "Vision:\n"
        "Ship the thing.\n"
        "Then keep it shipping.\n"
        "Backlog:\n"
        "1  open  fix  full  Fix the login [web]\n"
        "2  done  idea  one-prompt  Tidy the readme\n"
        "3  open  roadmap  full  Offline mode\n"
    )

    assert runner.invoke(app, ["workitem", "add", "thing", "Fourth"]).exit_code == 0
    assert runner.invoke(app, ["workitem", "spawn", "3"]).exit_code == 0
    result = runner.invoke(app, ["product", "roadmap", "thing"])
    assert result.output.endswith(
        "3  open  roadmap  full  Offline mode -> offline-mode\n"
        "5  open  fix  full  Fourth\n"
    )

    as_json = json.loads(runner.invoke(app, ["product", "roadmap", "thing", "--json"]).output)
    assert as_json["product"]["vision"] == VISION
    assert [i["id"] for i in as_json["items"]] == [1, 2, 3, 5]


def test_product_roadmap_shows_an_empty_product_and_refuses_an_unknown_one(checkout):
    assert runner.invoke(app, ["product", "add", "Bare"]).exit_code == 0
    result = runner.invoke(app, ["product", "roadmap", "bare"])
    assert result.exit_code == 0, result.output
    assert result.output == "Roadmap: Bare (bare)\nVision:  (none)\nBacklog:  (empty)\n"

    unknown = runner.invoke(app, ["product", "roadmap", "nope"])
    assert unknown.exit_code == 1 and "No product 'nope'" in unknown.stderr
