"""Work items: product-level entries with a kind, an issue link, a status, and a dev path.

A work item belongs to one product and lives beside it in the daemon's
state store. Kind is free text with four suggested values; the dev path is
one of three and anything else is refused; status is a fixed set. The
daemon serves them on work item routes behind the token guard, and the
CLI's ``workitem`` verbs reach those routes on a registered remote, the same
way the product verbs do.
"""

import dataclasses
import datetime
import json

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.daemon import auth, routes
from specflo.daemon import store as store_module
from specflo.daemon.products import Products
from specflo.daemon.workitems import (
    DEV_PATHS,
    KINDS,
    STATUSES,
    WORK_ITEMS_PATH,
    RemoteWorkItems,
    WorkItem,
    WorkItems,
)
from specflo.daemon.app import create_app
from specflo.errors import SpecfloError

runner = CliRunner()

ISSUE = "https://git.example/me/thing/issues/7"


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


# --- the vocabulary ------------------------------------------------------------


def test_the_fixed_vocabularies():
    assert KINDS == ("fix", "roadmap", "idea", "issue")
    assert DEV_PATHS == ("full", "one-prompt", "cyclical")
    assert STATUSES == ("open", "in-progress", "done", "dropped")


# --- the store ---------------------------------------------------------------


def test_work_items_round_trip_through_the_store(root):
    with_product(root, "thing")
    with store_module.open_store(root) as store:
        items = WorkItems(store)
        first = items.add("thing", "Fix the login", today="2026-09-06")
        assert first == WorkItem(
            id=1, product="thing", title="Fix the login", kind="fix", issue=None,
            dev_path="full", status="open", created="2026-09-06",
        )
        second = items.add(
            "thing", "Offline mode", kind="roadmap", issue=ISSUE, dev_path="cyclical",
            today="2026-09-06",
        )
        assert second.id == 2 and second.kind == "roadmap" and second.issue == ISSUE
        assert second.dev_path == "cyclical" and second.status == "open"

        done = items.set_status(1, "done")
        assert done == dataclasses.replace(first, status="done")
        assert items.show(1) == done
        assert items.list() == [done, second]

    with store_module.open_store(root) as store:
        assert WorkItems(store).show(2) == second


def test_kind_is_free_text_and_the_dev_path_and_status_are_not(root):
    with_product(root, "thing")
    with store_module.open_store(root) as store:
        items = WorkItems(store)
        assert items.add("thing", "Rename", kind="chore").kind == "chore"
        with pytest.raises(SpecfloError, match="Invalid dev path 'sprint'"):
            items.add("thing", "Nope", dev_path="sprint")
        with pytest.raises(SpecfloError, match="full, one-prompt, cyclical"):
            items.add("thing", "Nope", dev_path="")
        with pytest.raises(SpecfloError, match="Invalid status 'later'"):
            items.set_status(1, "later")
        with pytest.raises(SpecfloError, match="open, in-progress, done, dropped"):
            items.list(status="later")
        assert items.show(1).status == "open"


def test_work_items_need_a_product_and_a_title_and_an_existing_id(root):
    with_product(root, "thing")
    with store_module.open_store(root) as store:
        items = WorkItems(store)
        with pytest.raises(SpecfloError, match="No product 'nope'"):
            items.add("nope", "Orphan")
        with pytest.raises(SpecfloError, match="title"):
            items.add("thing", "   ")
        with pytest.raises(SpecfloError, match="kind"):
            items.add("thing", "Blank kind", kind="  ")
        with pytest.raises(SpecfloError, match="No work item 9"):
            items.show(9)
        with pytest.raises(SpecfloError, match="No work item 9"):
            items.set_status(9, "done")
        assert items.list() == []


def test_list_filters_by_product_status_and_kind(root):
    with_product(root, "thing", "other")
    with store_module.open_store(root) as store:
        items = WorkItems(store)
        a = items.add("thing", "A", kind="fix")
        b = items.add("thing", "B", kind="idea")
        c = items.add("other", "C", kind="fix")
        b = items.set_status(b.id, "done")

        assert items.list() == [a, b, c]
        assert items.list(product="thing") == [a, b]
        assert items.list(status="open") == [a, c]
        assert items.list(kind="fix") == [a, c]
        assert items.list(product="thing", status="open", kind="fix") == [a]
        assert items.list(product="other", kind="idea") == []
        with pytest.raises(SpecfloError, match="No product 'nope'"):
            items.list(product="nope")


# --- the routes --------------------------------------------------------------


def test_work_item_routes_round_trip_and_record_the_actor(client, root):
    with_product(root, "thing")
    today = datetime.date.today().isoformat()

    created = client.post(WORK_ITEMS_PATH, json={"product": "thing", "title": "Fix the login"})
    assert created.status_code == 200, created.text
    assert created.json()["result"] == {
        "id": 1, "product": "thing", "title": "Fix the login", "kind": "fix", "issue": None,
        "dev_path": "full", "status": "open", "created": today, "piece": None, "project": None,
    }
    second = client.post(WORK_ITEMS_PATH, json={
        "product": "thing", "title": "Offline mode", "kind": "roadmap",
        "issue": ISSUE, "dev_path": "one-prompt",
    })
    assert second.status_code == 200, second.text
    assert second.json()["result"]["dev_path"] == "one-prompt"

    status = client.put(f"{WORK_ITEMS_PATH}/1/status", json={"status": "in-progress"})
    assert status.status_code == 200, status.text
    assert status.json()["result"]["status"] == "in-progress"

    shown = client.get(f"{WORK_ITEMS_PATH}/1")
    assert shown.status_code == 200 and shown.json()["result"] == status.json()["result"]

    listed = client.get(WORK_ITEMS_PATH)
    assert [i["id"] for i in listed.json()["result"]] == [1, 2]
    filtered = client.get(WORK_ITEMS_PATH, params={"status": "open", "kind": "roadmap"})
    assert [i["id"] for i in filtered.json()["result"]] == [2]
    filtered = client.get(WORK_ITEMS_PATH, params={"product": "thing", "status": "in-progress"})
    assert [i["id"] for i in filtered.json()["result"]] == [1]

    assert audit_records(root) == [
        ("developer", None, "workitem_add", "1"),
        ("developer", None, "workitem_add", "2"),
        ("developer", None, "workitem_set_status", "1"),
    ]


def test_work_item_routes_refuse_bad_requests(client, root):
    with_product(root, "thing")
    assert client.post(WORK_ITEMS_PATH, json={"product": "thing", "title": "A"}).status_code == 200

    bad_path = client.post(
        WORK_ITEMS_PATH, json={"product": "thing", "title": "B", "dev_path": "sprint"}
    )
    assert bad_path.status_code == 400 and "Invalid dev path" in bad_path.json()["detail"]
    no_product = client.post(WORK_ITEMS_PATH, json={"product": "nope", "title": "B"})
    assert no_product.status_code == 400 and "No product 'nope'" in no_product.json()["detail"]
    unknown = client.get(f"{WORK_ITEMS_PATH}/9")
    assert unknown.status_code == 400 and "No work item 9" in unknown.json()["detail"]
    bad_status = client.put(f"{WORK_ITEMS_PATH}/1/status", json={"status": "later"})
    assert bad_status.status_code == 400 and "Invalid status" in bad_status.json()["detail"]
    bad_filter = client.get(WORK_ITEMS_PATH, params={"status": "later"})
    assert bad_filter.status_code == 400 and "Invalid status" in bad_filter.json()["detail"]

    extra = client.post(WORK_ITEMS_PATH, json={"product": "thing", "title": "B", "size": "L"})
    assert extra.status_code == 422 and "size" in extra.json()["detail"]
    missing = client.post(WORK_ITEMS_PATH, json={"product": "thing"})
    assert missing.status_code == 422 and "title" in missing.json()["detail"]
    missing = client.put(f"{WORK_ITEMS_PATH}/1/status", json={})
    assert missing.status_code == 422 and "status" in missing.json()["detail"]
    assert client.get(f"{WORK_ITEMS_PATH}/abc").status_code == 422

    assert audit_records(root) == [("developer", None, "workitem_add", "1")]
    assert [i["title"] for i in client.get(WORK_ITEMS_PATH).json()["result"]] == ["A"]


def test_work_item_routes_need_a_token(root):
    anonymous = TestClient(create_app(root))
    assert anonymous.get(WORK_ITEMS_PATH).status_code == 401
    assert anonymous.post(WORK_ITEMS_PATH, json={"product": "thing", "title": "A"}).status_code == 401
    assert anonymous.get(f"{WORK_ITEMS_PATH}/1").status_code == 401
    assert anonymous.put(f"{WORK_ITEMS_PATH}/1/status", json={"status": "done"}).status_code == 401


# --- the remote client -------------------------------------------------------


def test_remote_work_items_mirror_the_in_process_verbs(root, token):
    with_product(root, "thing")
    remote = RemoteWorkItems("http://testserver", token, client=TestClient(create_app(root)))

    added = remote.add("thing", "Fix the login", kind="fix", issue=ISSUE, dev_path="one-prompt")
    assert isinstance(added, WorkItem) and added.id == 1 and added.issue == ISSUE
    assert remote.set_status(1, "done") == dataclasses.replace(added, status="done")
    assert remote.show(1).status == "done"
    assert remote.add("thing", "Other").kind == "fix"
    assert [i.id for i in remote.list()] == [1, 2]
    assert [i.id for i in remote.list(status="open")] == [2]
    assert [i.id for i in remote.list(product="thing", kind="fix")] == [1, 2]

    with pytest.raises(SpecfloError, match="Invalid dev path 'sprint'"):
        remote.add("thing", "Nope", dev_path="sprint")
    with pytest.raises(SpecfloError, match="No work item 9"):
        remote.show(9)

    with store_module.open_store(root) as store:
        assert WorkItems(store).list() == remote.list()


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


def test_workitem_verbs_round_trip_through_the_registered_remote(checkout, live_daemon):
    today = datetime.date.today().isoformat()

    add = runner.invoke(app, ["workitem", "add", "thing", "Fix the login"])
    assert add.exit_code == 0, add.output
    assert add.output == "Added work item 1 (Fix the login) to product 'thing' on remote 'home'.\n"

    full = runner.invoke(app, [
        "workitem", "add", "thing", "Offline mode", "--kind", "roadmap",
        "--issue", ISSUE, "--dev-path", "cyclical",
    ])
    assert full.exit_code == 0, full.output

    status = runner.invoke(app, ["workitem", "set-status", "1", "in-progress"])
    assert status.exit_code == 0, status.output
    assert status.output == "Set work item 1 to 'in-progress' on remote 'home'.\n"

    shown = runner.invoke(app, ["workitem", "show", "2"])
    assert shown.exit_code == 0, shown.output
    assert shown.output == (
        "Work item: 2 - Offline mode\n"
        "Product:   thing\n"
        "Kind:      roadmap\n"
        "Dev path:  cyclical\n"
        "Piece:     -\n"
        "Project:   -\n"
        "Status:    open\n"
        f"Issue:     {ISSUE}\n"
        f"Created:   {today}\n"
    )
    shown = runner.invoke(app, ["workitem", "show", "1"])
    assert "Issue:     -\n" in shown.output and "Status:    in-progress\n" in shown.output

    listed = runner.invoke(app, ["workitem", "list"])
    assert listed.exit_code == 0, listed.output
    assert listed.output == (
        "1  thing  in-progress  fix  full  Fix the login\n"
        "2  thing  open  roadmap  cyclical  Offline mode\n"
    )
    assert runner.invoke(app, ["workitem", "list", "--status", "open"]).output == (
        "2  thing  open  roadmap  cyclical  Offline mode\n"
    )
    assert runner.invoke(app, ["workitem", "list", "--kind", "fix", "--product", "thing"]).output == (
        "1  thing  in-progress  fix  full  Fix the login\n"
    )
    assert runner.invoke(app, ["workitem", "list", "--kind", "idea"]).output == (
        "No work items match on remote 'home'.\n"
    )

    as_json = runner.invoke(app, ["workitem", "show", "2", "--json"])
    assert json.loads(as_json.output) == {
        "id": 2, "product": "thing", "title": "Offline mode", "kind": "roadmap",
        "issue": ISSUE, "dev_path": "cyclical", "status": "open", "created": today, "piece": None, "project": None,
    }
    as_json = runner.invoke(app, ["workitem", "list", "--json", "--status", "open"])
    assert [i["id"] for i in json.loads(as_json.output)["work_items"]] == [2]

    with store_module.open_store(live_daemon["root"]) as store:
        assert [i.id for i in WorkItems(store).list()] == [1, 2]
    assert not (checkout / daemon.STATE_STORE_FILENAME).exists()


def test_workitem_verbs_show_an_empty_backlog_and_refuse_what_the_daemon_refuses(checkout):
    empty = runner.invoke(app, ["workitem", "list"])
    assert empty.exit_code == 0
    assert empty.output == (
        "No work items on remote 'home'. Add one with"
        " `specflo workitem add <product> <title>`.\n"
    )

    bad_path = runner.invoke(app, ["workitem", "add", "thing", "Nope", "--dev-path", "sprint"])
    assert bad_path.exit_code == 1 and "Invalid dev path 'sprint'" in bad_path.stderr
    no_product = runner.invoke(app, ["workitem", "add", "nope", "Orphan"])
    assert no_product.exit_code == 1 and "No product 'nope'" in no_product.stderr
    unknown = runner.invoke(app, ["workitem", "show", "9"])
    assert unknown.exit_code == 1 and "No work item 9" in unknown.stderr
    assert runner.invoke(app, ["workitem", "add", "thing", "A"]).exit_code == 0
    bad_status = runner.invoke(app, ["workitem", "set-status", "1", "later"])
    assert bad_status.exit_code == 1 and "Invalid status 'later'" in bad_status.stderr
    bad_filter = runner.invoke(app, ["workitem", "list", "--status", "later"])
    assert bad_filter.exit_code == 1 and "Invalid status 'later'" in bad_filter.stderr
    assert runner.invoke(app, ["workitem", "show", "abc"]).exit_code == 2

    no_remote = runner.invoke(app, ["workitem", "list", "--remote", "nowhere"])
    assert no_remote.exit_code == 1 and "No remote 'nowhere'" in no_remote.stderr


# --- the issue link -------------------------------------------------------------


def test_an_issue_link_must_be_http_or_https(root):
    with store_module.open_store(root) as store:
        Products(store).add("Thing", slug="thing", today="2026-09-06")
        items = WorkItems(store)
        kept = items.add("thing", "Linked", issue="  https://issues.example/1 ", today="2026-09-06")
        blank = items.add("thing", "Blank", issue="   ", today="2026-09-06")
        assert kept.issue == "https://issues.example/1"
        assert blank.issue is None
        for bad in ("javascript:alert(1)", "ftp://host/x", "issues/1", "https:"):
            with pytest.raises(SpecfloError, match="Invalid issue link"):
                items.add("thing", "Bad", issue=bad, today="2026-09-06")
        assert [item.title for item in items.list()] == ["Linked", "Blank"]
