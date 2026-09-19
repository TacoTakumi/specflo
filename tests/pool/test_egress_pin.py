"""A project's egress pin stands over every request made from it.

A hosted project can pin an egress class on its project record. A lease
request made from a checkout whose active project is hosted carries that
project's slug and nothing more of it: the daemon reads the pin from its own
record of the project, and the pin is one more limit the request's ceiling is
the strictest of. No option of the request widens it, since the request never
says what the pin is. A request that names a project the daemon does not hold
is refused, never served as one with no pin; a request from a directory with
no active project names none, and no pin applies.

A request that waits keeps the pin it arrived under beside the class it
named, so the requests behind it judge whether it fits as the pool would.

The service's tests run real processes: the stub pi under an agent host. The
verb's tests ask a real daemon that holds a hosted project pinned with
``specflo egress``.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from specflo import daemon
from specflo.cli import app
from specflo.daemon import poolstore
from specflo.daemon.app import create_app
from specflo.daemon.poolstore import WaitingRequest
from specflo.pool import cli_admin, service, waiting
from specflo.pool import config as pool_config
from specflo.pool.config import Member, PoolConfig
from specflo.service.pool_remote import LEASES_PATH

from . import test_lease_request
from .test_lease_request import checkout, pool_daemon, runner  # noqa: F401 - fixtures
from .test_runner import DEFINITION

T0 = "2026-09-19T10:00:00.000+00:00"

ACCEPTS_OPEN = dataclasses.replace(DEFINITION, egress="open")

# The waiting table as it was when a row kept its request's class and no pin.
WAITING_TABLE_BEFORE = """
CREATE TABLE pool_waiting (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    pool         TEXT,
    team         TEXT,
    holder_label TEXT NOT NULL,
    arrived      TEXT NOT NULL,
    egress       TEXT
);
"""


def open_member(pool_rig) -> Member:
    return dataclasses.replace(pool_rig.hosted_member(), name="open-1", egress="open")


def pool_of(pool_rig, *members: Member) -> PoolConfig:
    """The pool "rebasers" of *members*, under a definition that accepts class open."""
    return dataclasses.replace(pool_rig.config(*members), definitions=(ACCEPTS_OPEN,))


def waiting_rows(pool_rig) -> list[WaitingRequest]:
    with pool_rig.store() as store:
        return store.list_waiting()


# -- the service: the pin is one more limit -------------------------------------


def test_from_a_project_pinned_local_an_open_request_to_a_hosted_pool_is_refused_naming_the_pin(
    pool_rig,
):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.hosted_member(), open_member(pool_rig)))

    with pytest.raises(service.EgressRefused, match="rebasers") as refused:
        svc.grant(
            "rebasers", holder_label="a", cwd=pool_rig.work, egress="open",
            pinned="local", project="my-project",
        )

    message = str(refused.value)
    assert "project 'my-project' pins 'local'" in message
    assert "ceiling is 'local'" in message
    assert not isinstance(refused.value, service.NoFreeMember)
    with pool_rig.store() as store:
        assert store.list_leases() == []
    assert pool_rig.pane_names() == []


def test_from_a_project_pinned_local_a_request_to_a_local_pool_is_granted(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member()))

    granted = svc.grant(
        "rebasers", holder_label="a", cwd=pool_rig.work, egress="open",
        pinned="local", project="my-project",
    )

    assert granted.agent == "local-1"
    svc.end_lease(granted.lease_id, "released")


def test_a_pinned_request_is_served_by_a_member_under_the_pin_and_no_other(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig), pool_rig.local_member()))

    first = svc.grant(
        "rebasers", holder_label="a", cwd=pool_rig.work, egress="open",
        pinned="local", project="my-project",
    )
    assert first.agent == "local-1"

    # the open member stands free, and the pin keeps the request from it
    with pytest.raises(service.NoFreeMember):
        svc.grant(
            "rebasers", holder_label="b", cwd=pool_rig.work, egress="open",
            pinned="local", project="my-project",
        )
    svc.end_lease(first.lease_id, "released")


def test_a_pin_more_open_than_the_request_widens_nothing(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))

    # names no class, so no-train stands, whatever the project would allow
    with pytest.raises(service.EgressRefused) as refused:
        svc.grant(
            "rebasers", holder_label="a", cwd=pool_rig.work,
            pinned="open", project="my-project",
        )

    assert "ceiling is 'no-train'" in str(refused.value)


def test_without_a_pin_the_same_open_request_is_granted(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))

    granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="open")

    assert granted.agent == "open-1"
    svc.end_lease(granted.lease_id, "released")


def test_a_pin_that_is_not_a_class_is_refused_by_the_service(pool_rig):
    from specflo.pool import egress

    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member()))

    with pytest.raises(egress.UnknownClass, match="private"):
        svc.grant(
            "rebasers", holder_label="a", cwd=pool_rig.work,
            pinned="private", project="my-project",
        )


# -- a pinned request that waits -------------------------------------------------


def asks(pool_rig, svc, label: str, egress=None, pinned=None) -> waiting.Waiting:
    ids = iter([f"request-{label}"])
    return waiting.Waiting(
        svc, "rebasers", holder_label=label, cwd=pool_rig.work, wait=3600, egress=egress,
        pinned=pinned, project=None if pinned is None else "my-project",
        mint_id=lambda: next(ids),
    )


def test_a_waiting_row_round_trips_the_pin_its_request_arrived_under(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    pinned = WaitingRequest(
        id="req-a", pool="workers", team=None, holder_label="a", arrived=T0,
        egress="open", pinned="local",
    )
    unpinned = WaitingRequest(id="req-b", pool="workers", team=None, holder_label="b", arrived=T0)

    with poolstore.open_pool_store(root) as store:
        store.add_waiting(pinned)
        store.add_waiting(unpinned)

    with poolstore.open_pool_store(root) as store:
        assert store.list_waiting() == [pinned, unpinned]
    assert unpinned.pinned is None


def test_a_pinned_request_that_waits_writes_down_its_pin_beside_the_class_it_named(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member(), open_member(pool_rig)))
    held = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="local")

    assert asks(pool_rig, svc, "b", "open", pinned="local").attempt() is None
    assert asks(pool_rig, svc, "c", "local").attempt() is None

    assert [(row.id, row.egress, row.pinned) for row in waiting_rows(pool_rig)] == [
        ("request-b", "open", "local"), ("request-c", "local", None),
    ]
    svc.end_lease(held.lease_id, "released")


def test_an_earlier_pinned_request_holds_up_no_one_at_a_member_its_pin_keeps_it_from(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member(), open_member(pool_rig)))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="local")
    earlier = asks(pool_rig, svc, "b", "open", pinned="local")
    assert earlier.attempt() is None

    # the open member is free; by the class it named the earlier request would
    # take it, and by its pin it may not
    later = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work, egress="open")

    assert later.agent == "open-1"
    assert earlier.attempt() is None
    svc.end_lease(first.lease_id, "released")
    # at the local member it fits, so it goes before a later request
    with pytest.raises(service.NoFreeMember, match="came earlier"):
        svc.grant("rebasers", holder_label="d", cwd=pool_rig.work)
    granted = earlier.attempt()
    assert granted.agent == "local-1"
    assert waiting_rows(pool_rig) == []
    for grant in (later, granted):
        svc.end_lease(grant.lease_id, "released")


def test_a_pinned_request_no_member_can_serve_never_waits(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))
    asked = asks(pool_rig, svc, "a", "open", pinned="local")

    with pytest.raises(service.EgressRefused, match="my-project"):
        asked.attempt()

    assert waiting_rows(pool_rig) == []
    asked.leave()


def test_opening_the_store_on_a_waiting_table_from_before_the_pin_adds_the_column(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    path = root / daemon.STATE_STORE_FILENAME
    before = sqlite3.connect(path)
    before.executescript(WAITING_TABLE_BEFORE)
    before.execute(
        "INSERT INTO pool_waiting (id, pool, team, holder_label, arrived, egress)"
        " VALUES ('request-old', 'workers', NULL, 'b', ?, 'open')", (T0,),
    )
    before.commit()
    before.close()

    with poolstore.open_pool_store(root) as store:
        (old,) = store.list_waiting()
        store.add_waiting(WaitingRequest(
            id="request-new", pool="workers", team=None, holder_label="later",
            arrived=T0, egress="open", pinned="local",
        ))

    assert (old.egress, old.pinned) == ("open", None)
    with poolstore.open_pool_store(root) as store:
        assert [(row.id, row.pinned) for row in store.list_waiting()] == [
            ("request-old", None), ("request-new", "local"),
        ]


# -- the verb, asked of a real daemon that holds the project ----------------------

WRITE_POOL = test_lease_request.write_pool
REVIEWER = test_lease_request.REBASER.replace("egress: local", "egress: no-train")


def write_two_pools(rig) -> None:
    """The lease verb's local pool, and beside it a pool "reviewers" of one
    hosted member of class no-train."""
    WRITE_POOL(rig)
    directory = cli_admin.pool_dir(rig.root)
    (directory / pool_config.DEFINITIONS_DIR / "reviewer.md").write_text(
        REVIEWER, encoding="utf-8"
    )
    path = directory / pool_config.POOL_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["accounts"] = [{"name": "team-a", "cap": 2, "key_env": "TEAM_A_KEY"}]
    data["members"].append({
        "name": "hosted-1", "command": rig.command, "backing": "hosted",
        "model": "some-vendor/some-model", "account": "team-a", "labels": [],
        "capacity": 1, "egress": "no-train",
    })
    data["pools"].append({
        "name": "reviewers", "definition": "reviewer", "members": ["hosted-1"],
        "size": 1, "idle_default": "10m", "idle_max": "4h",
    })
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def two_pools(monkeypatch):
    """Asked for before the daemon: the daemon's fixture writes these pools instead."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_two_pools)


def hosted_project(name: str, *pin: str) -> None:
    """A project on the daemon, active in the checkout, pinned to *pin* when given."""
    created = runner.invoke(app, ["new", name, "--remote", "home"])
    assert created.exit_code == 0, created.output
    for egress_class in pin:
        pinned = runner.invoke(app, ["egress", egress_class])
        assert pinned.exit_code == 0, pinned.output


def lease_bodies(monkeypatch) -> list[dict]:
    """The body of every lease request the verb sends, as it goes out."""
    bodies = []
    real_send = httpx.Client.send

    def recording_send(self, request, **kwargs):
        if request.url.path == LEASES_PATH:
            bodies.append(json.loads(request.content))
        return real_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", recording_send)
    return bodies


def test_from_a_project_pinned_local_the_verb_is_refused_a_hosted_pool_and_granted_a_local_one(
    two_pools, checkout, pool_rig, monkeypatch  # noqa: F811
):
    hosted_project("Pinned Thing", "local")
    bodies = lease_bodies(monkeypatch)

    refused = runner.invoke(app, ["lease", "request", "reviewers", "--egress", "open"])

    assert refused.exit_code != 0
    assert "project 'pinned-thing' pins 'local'" in refused.output
    with pool_rig.store() as store:
        assert store.list_leases() == []
        assert store.list_waiting() == []

    granted = runner.invoke(app, ["lease", "request", "rebasers", "--egress", "open", "--json"])

    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.stdout)["agent"] == "local-1"
    # the request says which project asks, and nothing of what that project pins
    assert [body["project"] for body in bodies] == ["pinned-thing", "pinned-thing"]
    assert all("local" not in json.dumps(body) for body in bodies)


def test_a_request_from_a_directory_with_no_active_project_uses_no_pin(
    two_pools, checkout, pool_rig, monkeypatch  # noqa: F811
):
    bodies = lease_bodies(monkeypatch)

    granted = runner.invoke(app, ["lease", "request", "reviewers", "--json"])

    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.stdout)["agent"] == "hosted-1"
    assert ["project" in body for body in bodies] == [False]


def test_a_hosted_project_with_no_pin_is_held_to_none(two_pools, checkout, pool_rig):  # noqa: F811
    hosted_project("Free Thing")

    granted = runner.invoke(app, ["lease", "request", "reviewers", "--json"])

    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.stdout)["agent"] == "hosted-1"


def test_the_daemon_reads_the_pin_from_its_own_record_at_every_request(
    two_pools, checkout, pool_rig, pool_daemon  # noqa: F811
):
    hosted_project("Pinned Thing", "local")
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    body = {
        "pool": "reviewers", "cwd": str(pool_rig.work), "egress": "open",
        "project": "pinned-thing",
    }

    refused = client.post(LEASES_PATH, json=body)

    assert refused.status_code == 400
    assert "project 'pinned-thing' pins 'local'" in refused.json()["detail"]
    # a request has no field to say the pin with
    assert client.post(LEASES_PATH, json={**body, "pinned": "open"}).status_code == 422
    assert pool_rig.pane_names() == []

    # the record changes on the daemon, and the same request is judged by it
    assert runner.invoke(app, ["egress", "no-train"]).exit_code == 0
    granted = client.post(LEASES_PATH, json=body)

    assert granted.status_code == 200, granted.text
    assert granted.json()["result"]["agent"] == "hosted-1"


def test_a_request_that_names_a_project_the_daemon_does_not_hold_is_refused(
    two_pools, checkout, pool_rig, pool_daemon  # noqa: F811
):
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    good = {"pool": "reviewers", "cwd": str(pool_rig.work)}

    unknown = client.post(LEASES_PATH, json={**good, "project": "no-such-thing"})

    assert unknown.status_code == 400
    assert "no-such-thing" in unknown.json()["detail"]
    for project in ("../projects", "", 7):
        response = client.post(LEASES_PATH, json={**good, "project": project})
        assert response.status_code in (400, 422), (project, response.text)
    with pool_rig.store() as store:
        assert store.list_leases() == []
    assert pool_rig.pane_names() == []
