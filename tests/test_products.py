"""Products: a name, a slug, a repo location, and a vision, held in the daemon's store.

A product is not a project artifact: it lives in the SQLite state store under
the daemon root, behind one store interface. The daemon serves it on product
routes behind the token guard, and the CLI's ``product`` verbs reach those
routes on a registered remote: the one named by ``--remote``, or the only one
registered. Nothing about a product is written under a client checkout.
"""

import dataclasses
import datetime
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.daemon import auth, routes
from specflo.daemon import store as store_module
from specflo.daemon.app import create_app
from specflo.daemon.products import PRODUCTS_PATH, Product, Products, RemoteProducts
from specflo.errors import SpecfloError

runner = CliRunner()

REPO = "git@host:me/thing.git"


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


def audit_records(root):
    path = root / routes.AUDIT_FILENAME
    if not path.is_file():
        return []
    return [
        (r["identity"], r["project"], r["operation"], r["id"])
        for r in (json.loads(line) for line in path.read_text().splitlines())
    ]


# --- the store ---------------------------------------------------------------


def test_the_daemon_root_holds_a_sqlite_store_with_a_products_table(root):
    with store_module.open_store(root) as store:
        assert isinstance(store, store_module.SqliteStore)
        assert store.list_products() == []

    path = root / daemon.STATE_STORE_FILENAME
    assert path.is_file()
    tables = sqlite3.connect(path).execute(
        "select name from sqlite_master where type = 'table'"
    ).fetchall()
    assert ("products",) in tables


def test_products_round_trip_through_the_store(root):
    with store_module.open_store(root) as store:
        products = Products(store)
        added = products.add("My Thing", repo=REPO, today="2026-09-06")
        assert added == Product(
            name="My Thing", slug="my-thing", repo=REPO, vision="", created="2026-09-06"
        )
        with_vision = products.set_vision("my-thing", "Be the thing.\n")
        assert with_vision == dataclasses.replace(added, vision="Be the thing.\n")
        assert products.show("my-thing") == with_vision
        bare = products.add("Zed", today="2026-09-06")
        assert bare.repo is None
        assert products.list() == [with_vision, bare]

    # A fresh store on the same root sees what the first one wrote.
    with store_module.open_store(root) as store:
        assert Products(store).show("my-thing") == with_vision


def test_a_duplicate_slug_is_refused(root):
    with store_module.open_store(root) as store:
        products = Products(store)
        products.add("Thing")
        with pytest.raises(SpecfloError, match="Product 'thing' already exists"):
            products.add("Thing")
        with pytest.raises(SpecfloError, match="Product 'thing' already exists"):
            products.add("Other", slug="thing")
        assert [p.slug for p in products.list()] == ["thing"]


def test_names_slugs_and_unknown_products_are_refused(root):
    with store_module.open_store(root) as store:
        products = Products(store)
        with pytest.raises(SpecfloError, match="name"):
            products.add("   ")
        with pytest.raises(SpecfloError, match="Invalid product slug 'Not A Slug'"):
            products.add("Thing", slug="Not A Slug")
        assert products.add("Thing", slug="thing-2").slug == "thing-2"
        with pytest.raises(SpecfloError, match="No product 'nope'"):
            products.show("nope")
        with pytest.raises(SpecfloError, match="No product 'nope'"):
            products.set_vision("nope", "x")


# --- the routes --------------------------------------------------------------


def test_product_routes_round_trip_and_record_the_actor(client, root):
    today = datetime.date.today().isoformat()

    created = client.post(PRODUCTS_PATH, json={"name": "My Thing", "repo": REPO})
    assert created.status_code == 200, created.text
    assert created.json()["result"] == {
        "name": "My Thing", "slug": "my-thing", "repo": REPO, "vision": "", "created": today,
    }

    vision = client.put(f"{PRODUCTS_PATH}/my-thing/vision", json={"vision": "Be the thing."})
    assert vision.status_code == 200, vision.text
    assert vision.json()["result"]["vision"] == "Be the thing."

    shown = client.get(f"{PRODUCTS_PATH}/my-thing")
    assert shown.status_code == 200 and shown.json()["result"] == vision.json()["result"]

    assert client.post(PRODUCTS_PATH, json={"name": "Zed"}).status_code == 200
    listed = client.get(PRODUCTS_PATH)
    assert [p["slug"] for p in listed.json()["result"]] == ["my-thing", "zed"]

    assert audit_records(root) == [
        ("developer", None, "product_add", "my-thing"),
        ("developer", None, "product_set_vision", "my-thing"),
        ("developer", None, "product_add", "zed"),
    ]


def test_product_routes_refuse_bad_requests(client, root):
    assert client.post(PRODUCTS_PATH, json={"name": "Thing"}).status_code == 200

    duplicate = client.post(PRODUCTS_PATH, json={"name": "Thing"})
    assert duplicate.status_code == 400 and "already exists" in duplicate.json()["detail"]

    unknown = client.get(f"{PRODUCTS_PATH}/nope")
    assert unknown.status_code == 400 and "No product 'nope'" in unknown.json()["detail"]
    unknown = client.put(f"{PRODUCTS_PATH}/nope/vision", json={"vision": "x"})
    assert unknown.status_code == 400 and "No product 'nope'" in unknown.json()["detail"]

    extra = client.post(PRODUCTS_PATH, json={"name": "Other", "colour": "red"})
    assert extra.status_code == 422 and "colour" in extra.json()["detail"]
    missing = client.post(PRODUCTS_PATH, json={"repo": REPO})
    assert missing.status_code == 422 and "name" in missing.json()["detail"]
    missing = client.put(f"{PRODUCTS_PATH}/thing/vision", json={})
    assert missing.status_code == 422 and "vision" in missing.json()["detail"]

    # A refused request leaves no audit record and changes nothing.
    assert audit_records(root) == [("developer", None, "product_add", "thing")]
    assert [p["name"] for p in client.get(PRODUCTS_PATH).json()["result"]] == ["Thing"]


def test_product_routes_need_a_token(root):
    anonymous = TestClient(create_app(root))
    assert anonymous.get(PRODUCTS_PATH).status_code == 401
    assert anonymous.post(PRODUCTS_PATH, json={"name": "Thing"}).status_code == 401
    assert anonymous.get(f"{PRODUCTS_PATH}/thing").status_code == 401
    assert anonymous.put(f"{PRODUCTS_PATH}/thing/vision", json={"vision": "x"}).status_code == 401


# --- the remote client -------------------------------------------------------


def test_remote_products_mirror_the_in_process_verbs(root, token):
    remote = RemoteProducts("http://testserver", token, client=TestClient(create_app(root)))

    added = remote.add("My Thing", repo=REPO)
    assert isinstance(added, Product) and added.slug == "my-thing" and added.repo == REPO
    assert remote.set_vision("my-thing", "Be the thing.") == dataclasses.replace(
        added, vision="Be the thing."
    )
    assert remote.show("my-thing").vision == "Be the thing."
    assert remote.list() == [remote.show("my-thing")]

    with pytest.raises(SpecfloError, match="Product 'my-thing' already exists"):
        remote.add("My Thing")
    with pytest.raises(SpecfloError, match="No product 'nope'"):
        remote.show("nope")

    with store_module.open_store(root) as store:
        assert Products(store).list() == remote.list()


def test_remote_products_report_a_refused_token_and_an_unreachable_daemon(root):
    refused = RemoteProducts("http://testserver", "wrong", client=TestClient(create_app(root)))
    with pytest.raises(SpecfloError, match="refused the token"):
        refused.list()

    unreachable = RemoteProducts("http://127.0.0.1:9", "token", timeout=0.5)
    with pytest.raises(SpecfloError, match="Cannot reach the remote"):
        unreachable.list()


# --- the CLI verbs -----------------------------------------------------------


@pytest.fixture
def checkout(tmp_path, monkeypatch, live_daemon):
    """A checkout with the live daemon registered as its one remote, ``home``."""
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)
    done = runner.invoke(
        app, ["remote", "add", "home", live_daemon["url"], "--token", live_daemon["token"]]
    )
    assert done.exit_code == 0, done.output
    return root


def test_product_verbs_round_trip_through_the_one_registered_remote(checkout, live_daemon):
    today = datetime.date.today().isoformat()

    add = runner.invoke(app, ["product", "add", "My Thing", "--repo", REPO])
    assert add.exit_code == 0, add.output
    assert add.output == "Added product 'my-thing' (My Thing) on remote 'home'.\n"

    vision = runner.invoke(app, ["product", "set-vision", "my-thing", "Be the thing."])
    assert vision.exit_code == 0, vision.output
    assert vision.output == "Set the vision of 'my-thing' on remote 'home'.\n"

    shown = runner.invoke(app, ["product", "show", "my-thing"])
    assert shown.exit_code == 0, shown.output
    assert shown.output == (
        "Product: My Thing (my-thing)\n"
        f"Repo:    {REPO}\n"
        f"Created: {today}\n"
        "Vision:\n"
        "Be the thing.\n"
    )

    assert runner.invoke(app, ["product", "add", "Zed"]).exit_code == 0
    listed = runner.invoke(app, ["product", "list"])
    assert listed.exit_code == 0, listed.output
    assert listed.output == f"my-thing  My Thing  {REPO}\nzed  Zed\n"

    as_json = runner.invoke(app, ["product", "show", "my-thing", "--json"])
    assert json.loads(as_json.output) == {
        "name": "My Thing", "slug": "my-thing", "repo": REPO,
        "vision": "Be the thing.", "created": today,
    }
    as_json = runner.invoke(app, ["product", "list", "--json"])
    assert [p["slug"] for p in json.loads(as_json.output)["products"]] == ["my-thing", "zed"]

    # A multi-line vision comes in on stdin and prints back verbatim.
    stdin = runner.invoke(
        app, ["product", "set-vision", "my-thing", "--stdin"], input="One.\nTwo.\n"
    )
    assert stdin.exit_code == 0, stdin.output
    shown = runner.invoke(app, ["product", "show", "my-thing"])
    assert shown.output.endswith("Vision:\nOne.\nTwo.\n")

    # The daemon root holds the store; the checkout holds nothing of it.
    assert (live_daemon["root"] / daemon.STATE_STORE_FILENAME).is_file()
    assert not (checkout / daemon.STATE_STORE_FILENAME).exists()
    with store_module.open_store(live_daemon["root"]) as store:
        assert [p.slug for p in Products(store).list()] == ["my-thing", "zed"]


def test_product_verbs_show_an_empty_list_and_a_product_without_vision_or_repo(checkout):
    empty = runner.invoke(app, ["product", "list"])
    assert empty.exit_code == 0
    assert empty.output == (
        "No products on remote 'home'. Add one with `specflo product add <name>`.\n"
    )

    assert runner.invoke(app, ["product", "add", "Bare"]).exit_code == 0
    shown = runner.invoke(app, ["product", "show", "bare"])
    assert shown.exit_code == 0, shown.output
    lines = shown.output.splitlines()
    assert lines[0] == "Product: Bare (bare)"
    assert lines[1] == "Repo:    -"
    assert lines[3] == "Vision:  (none)"


def test_product_verbs_refuse_what_the_daemon_refuses(checkout):
    assert runner.invoke(app, ["product", "add", "Thing"]).exit_code == 0

    duplicate = runner.invoke(app, ["product", "add", "Thing"])
    assert duplicate.exit_code == 1 and "Product 'thing' already exists" in duplicate.stderr

    unknown = runner.invoke(app, ["product", "show", "nope"])
    assert unknown.exit_code == 1 and "No product 'nope'" in unknown.stderr

    bad_slug = runner.invoke(app, ["product", "add", "Other", "--slug", "Not A Slug"])
    assert bad_slug.exit_code == 1 and "Invalid product slug" in bad_slug.stderr

    both = runner.invoke(app, ["product", "set-vision", "thing", "text", "--stdin"])
    assert both.exit_code == 1 and "exactly one" in both.stderr
    neither = runner.invoke(app, ["product", "set-vision", "thing"])
    assert neither.exit_code == 1 and "exactly one" in neither.stderr

    listed = runner.invoke(app, ["product", "list"])
    assert listed.output == "thing  Thing\n"


def test_product_verbs_need_exactly_one_remote_unless_one_is_named(tmp_path, monkeypatch, live_daemon):
    root = tmp_path / "checkout"
    root.mkdir()
    config.init_config(root)
    monkeypatch.chdir(root)

    none = runner.invoke(app, ["product", "list"])
    assert none.exit_code == 1 and "No remotes" in none.stderr

    for name in ("home", "office"):
        added = runner.invoke(
            app, ["remote", "add", name, live_daemon["url"], "--token", live_daemon["token"]]
        )
        assert added.exit_code == 0, added.output

    several = runner.invoke(app, ["product", "list"])
    assert several.exit_code == 1
    assert "home, office" in several.stderr and "--remote" in several.stderr

    named = runner.invoke(app, ["product", "add", "Thing", "--remote", "office"])
    assert named.exit_code == 0, named.output
    assert named.output == "Added product 'thing' (Thing) on remote 'office'.\n"
    assert runner.invoke(app, ["product", "list", "--remote", "home"]).output == "thing  Thing\n"

    unknown = runner.invoke(app, ["product", "list", "--remote", "nowhere"])
    assert unknown.exit_code == 1 and "No remote 'nowhere'" in unknown.stderr
