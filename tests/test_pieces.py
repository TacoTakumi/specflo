"""Configured pieces: the deployable parts a product may declare, and what may target them.

A product declares zero or more pieces (web, admin, mobile, ...). A work
item may target one of the declared pieces; an undeclared one is refused,
and a product with no pieces takes items with no target. Pieces live in the
state store beside the product, are served on product routes behind the
token guard, and are managed from the CLI's ``product piece`` verbs.
"""

import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.daemon import auth, routes
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import PRODUCTS_PATH, Products, RemoteProducts
from specflo.daemon.workitems import WORK_ITEMS_PATH, RemoteWorkItems, WorkItems
from specflo.errors import SpecfloError

runner = CliRunner()


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


def with_product(root, *slugs):
    with store_module.open_store(root) as store:
        for slug in slugs:
            Products(store).add(slug.title(), slug=slug, today="2026-09-06")


def audit_records(root):
    path = root / routes.AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        (r["identity"], r["project"], r["operation"], r["id"])
        for r in (json.loads(line) for line in path.read_text().splitlines())
    ]


# --- the store ---------------------------------------------------------------


def test_pieces_are_declared_listed_and_removed_in_declaration_order(root):
    with_product(root, "thing", "other")
    with store_module.open_store(root) as store:
        products = Products(store)
        assert products.list_pieces("thing") == []
        assert products.add_piece("thing", "web") == ["web"]
        assert products.add_piece("thing", "admin") == ["web", "admin"]
        assert products.add_piece("other", "web") == ["web"]
        assert products.list_pieces("thing") == ["web", "admin"]
        assert products.remove_piece("thing", "web") == ["admin"]
        assert products.list_pieces("other") == ["web"]

    with store_module.open_store(root) as store:
        assert Products(store).list_pieces("thing") == ["admin"]


def test_piece_declarations_are_validated(root):
    with_product(root, "thing")
    with store_module.open_store(root) as store:
        products = Products(store)
        products.add_piece("thing", "web")
        with pytest.raises(SpecfloError, match="Piece 'web' is already declared on 'thing'"):
            products.add_piece("thing", "web")
        with pytest.raises(SpecfloError, match="Invalid piece name 'Web App'"):
            products.add_piece("thing", "Web App")
        with pytest.raises(SpecfloError, match="No piece 'mobile' on 'thing'"):
            products.remove_piece("thing", "mobile")
        for verb in (products.list_pieces, lambda slug: products.add_piece(slug, "web")):
            with pytest.raises(SpecfloError, match="No product 'nope'"):
                verb("nope")
        assert products.list_pieces("thing") == ["web"]


def test_a_work_item_may_target_a_declared_piece_only(root):
    with_product(root, "thing", "bare")
    with store_module.open_store(root) as store:
        products, items = Products(store), WorkItems(store)
        products.add_piece("thing", "web")
        products.add_piece("thing", "admin")

        targeted = items.add("thing", "Fix the login", piece="web")
        assert targeted.piece == "web" and items.show(targeted.id).piece == "web"
        assert items.add("thing", "Untargeted").piece is None
        with pytest.raises(SpecfloError, match="No piece 'mobile' on 'thing'; declared: web, admin"):
            items.add("thing", "Nope", piece="mobile")

        # A product with no pieces takes items with no target, and nothing else.
        assert items.add("bare", "Plain").piece is None
        with pytest.raises(SpecfloError, match="'bare' declares no pieces"):
            items.add("bare", "Nope", piece="web")
        assert [i.title for i in items.list()] == ["Fix the login", "Untargeted", "Plain"]


def test_a_targeted_piece_cannot_be_removed(root):
    with_product(root, "thing")
    with store_module.open_store(root) as store:
        products, items = Products(store), WorkItems(store)
        products.add_piece("thing", "web")
        first = items.add("thing", "A", piece="web")
        second = items.add("thing", "B", piece="web")
        with pytest.raises(SpecfloError, match=f"targeted by work item {first.id}, {second.id}"):
            products.remove_piece("thing", "web")
        assert products.list_pieces("thing") == ["web"]


def test_a_store_from_before_pieces_gains_the_column_on_open(root):
    # A daemon root whose work_items table predates the piece column.
    import sqlite3

    path = root / daemon.STATE_STORE_FILENAME
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE products (slug TEXT PRIMARY KEY, name TEXT NOT NULL, repo TEXT,"
        " vision TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);"
        "CREATE TABLE work_items (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " product TEXT NOT NULL REFERENCES products(slug), title TEXT NOT NULL,"
        " kind TEXT NOT NULL, issue TEXT, dev_path TEXT NOT NULL, status TEXT NOT NULL,"
        " created TEXT NOT NULL);"
        "INSERT INTO products VALUES ('thing', 'Thing', NULL, '', '2026-09-06');"
        "INSERT INTO work_items (product, title, kind, dev_path, status, created)"
        " VALUES ('thing', 'Old', 'fix', 'full', 'open', '2026-09-06');"
    )
    old.close()

    with store_module.open_store(root) as store:
        items = WorkItems(store)
        assert items.show(1).piece is None
        Products(store).add_piece("thing", "web")
        assert items.add("thing", "New", piece="web").piece == "web"


# --- the routes --------------------------------------------------------------


def test_piece_routes_round_trip_and_record_the_actor(client, root):
    with_product(root, "thing")
    pieces = f"{PRODUCTS_PATH}/thing/pieces"

    assert client.get(pieces).json()["result"] == []
    added = client.post(pieces, json={"name": "web"})
    assert added.status_code == 200, added.text
    assert added.json()["result"] == ["web"]
    assert client.post(pieces, json={"name": "admin"}).json()["result"] == ["web", "admin"]
    assert client.get(pieces).json()["result"] == ["web", "admin"]

    targeted = client.post(WORK_ITEMS_PATH, json={"product": "thing", "title": "A", "piece": "web"})
    assert targeted.status_code == 200, targeted.text
    assert targeted.json()["result"]["piece"] == "web"
    untargeted = client.post(WORK_ITEMS_PATH, json={"product": "thing", "title": "B"})
    assert untargeted.json()["result"]["piece"] is None

    removed = client.delete(f"{pieces}/admin")
    assert removed.status_code == 200, removed.text
    assert removed.json()["result"] == ["web"]

    assert audit_records(root) == [
        ("developer", None, "product_piece_add", "thing/web"),
        ("developer", None, "product_piece_add", "thing/admin"),
        ("developer", None, "workitem_add", "1"),
        ("developer", None, "workitem_add", "2"),
        ("developer", None, "product_piece_remove", "thing/admin"),
    ]


def test_piece_routes_refuse_bad_requests(client, root):
    with_product(root, "thing")
    pieces = f"{PRODUCTS_PATH}/thing/pieces"
    assert client.post(pieces, json={"name": "web"}).status_code == 200

    duplicate = client.post(pieces, json={"name": "web"})
    assert duplicate.status_code == 400 and "already declared" in duplicate.json()["detail"]
    bad_name = client.post(pieces, json={"name": "Web App"})
    assert bad_name.status_code == 400 and "Invalid piece name" in bad_name.json()["detail"]
    no_product = client.get(f"{PRODUCTS_PATH}/nope/pieces")
    assert no_product.status_code == 400 and "No product 'nope'" in no_product.json()["detail"]
    unknown = client.delete(f"{pieces}/mobile")
    assert unknown.status_code == 400 and "No piece 'mobile'" in unknown.json()["detail"]
    undeclared = client.post(
        WORK_ITEMS_PATH, json={"product": "thing", "title": "A", "piece": "mobile"}
    )
    assert undeclared.status_code == 400 and "No piece 'mobile'" in undeclared.json()["detail"]
    missing = client.post(pieces, json={})
    assert missing.status_code == 422 and "name" in missing.json()["detail"]

    assert audit_records(root) == [("developer", None, "product_piece_add", "thing/web")]
    assert client.get(pieces).json()["result"] == ["web"]
    assert client.get(WORK_ITEMS_PATH).json()["result"] == []


def test_piece_routes_need_a_token(root):
    anonymous = TestClient(create_app(root))
    pieces = f"{PRODUCTS_PATH}/thing/pieces"
    assert anonymous.get(pieces).status_code == 401
    assert anonymous.post(pieces, json={"name": "web"}).status_code == 401
    assert anonymous.delete(f"{pieces}/web").status_code == 401


# --- the remote clients ------------------------------------------------------


def test_remote_clients_mirror_the_in_process_piece_verbs(root, token):
    with_product(root, "thing")
    products = RemoteProducts("http://testserver", token, client=TestClient(create_app(root)))
    items = RemoteWorkItems("http://testserver", token, client=TestClient(create_app(root)))

    assert products.add_piece("thing", "web") == ["web"]
    assert products.add_piece("thing", "admin") == ["web", "admin"]
    assert products.list_pieces("thing") == ["web", "admin"]
    assert items.add("thing", "A", piece="web").piece == "web"
    assert products.remove_piece("thing", "admin") == ["web"]
    with pytest.raises(SpecfloError, match="No piece 'mobile' on 'thing'"):
        items.add("thing", "B", piece="mobile")
    with pytest.raises(SpecfloError, match="targeted by work item 1"):
        products.remove_piece("thing", "web")

    with store_module.open_store(root) as store:
        assert Products(store).list_pieces("thing") == ["web"]
        assert WorkItems(store).show(1).piece == "web"


# --- the CLI verbs -----------------------------------------------------------


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    """A checkout with the live daemon registered as ``home``, holding product ``thing``."""
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    assert runner.invoke(app, ["product", "add", "Thing"]).exit_code == 0
    return root


def test_piece_verbs_round_trip_and_work_items_target_them(checkout):
    empty = runner.invoke(app, ["product", "piece", "list", "thing"])
    assert empty.exit_code == 0, empty.output
    assert empty.output == "No pieces on product 'thing'; its work items carry no target.\n"

    web = runner.invoke(app, ["product", "piece", "add", "thing", "web"])
    assert web.exit_code == 0, web.output
    assert web.output == "Declared piece 'web' for product 'thing' on remote 'home'.\n"
    assert runner.invoke(app, ["product", "piece", "add", "thing", "admin"]).exit_code == 0

    listed = runner.invoke(app, ["product", "piece", "list", "thing"])
    assert listed.output == "web\nadmin\n"
    as_json = runner.invoke(app, ["product", "piece", "list", "thing", "--json"])
    assert json.loads(as_json.output) == {"product": "thing", "pieces": ["web", "admin"]}

    targeted = runner.invoke(app, ["workitem", "add", "thing", "Fix the login", "--piece", "web"])
    assert targeted.exit_code == 0, targeted.output
    shown = runner.invoke(app, ["workitem", "show", "1"])
    assert "Piece:     web\n" in shown.output
    assert json.loads(runner.invoke(app, ["workitem", "show", "1", "--json"]).output)["piece"] == "web"
    untargeted = runner.invoke(app, ["workitem", "add", "thing", "Anything"])
    assert untargeted.exit_code == 0, untargeted.output
    assert "Piece:     -\n" in runner.invoke(app, ["workitem", "show", "2"]).output

    removed = runner.invoke(app, ["product", "piece", "remove", "thing", "admin"])
    assert removed.exit_code == 0, removed.output
    assert removed.output == "Removed piece 'admin' from product 'thing' on remote 'home'.\n"
    assert runner.invoke(app, ["product", "piece", "list", "thing"]).output == "web\n"


def test_piece_verbs_refuse_what_the_daemon_refuses(checkout):
    assert runner.invoke(app, ["product", "piece", "add", "thing", "web"]).exit_code == 0

    duplicate = runner.invoke(app, ["product", "piece", "add", "thing", "web"])
    assert duplicate.exit_code == 1 and "already declared" in duplicate.stderr
    undeclared = runner.invoke(app, ["workitem", "add", "thing", "Nope", "--piece", "mobile"])
    assert undeclared.exit_code == 1
    assert "No piece 'mobile' on 'thing'; declared: web" in undeclared.stderr
    no_product = runner.invoke(app, ["product", "piece", "list", "nope"])
    assert no_product.exit_code == 1 and "No product 'nope'" in no_product.stderr
    unknown = runner.invoke(app, ["product", "piece", "remove", "thing", "mobile"])
    assert unknown.exit_code == 1 and "No piece 'mobile' on 'thing'" in unknown.stderr

    assert runner.invoke(app, ["workitem", "add", "thing", "A", "--piece", "web"]).exit_code == 0
    targeted = runner.invoke(app, ["product", "piece", "remove", "thing", "web"])
    assert targeted.exit_code == 1 and "targeted by work item 1" in targeted.stderr
    assert runner.invoke(app, ["product", "piece", "list", "thing"]).output == "web\n"
