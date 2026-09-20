"""A request that waits is judged by the egress class it named, not by the default.

The requests that wait are served in the order they came: a grant gives way
to every earlier request that fits now. Whether an earlier request fits
depends on its egress ceiling, as it does for a request that is asked now, so
the waiting row keeps the class the request named, or that it named none. A
request that named open is then not overtaken at the open member it waits
for, and one that named local holds up no one at a member it would not take.

The row keeps what was asked, not the ceiling: the ceiling also stands under
the pool's definition, and is worked out again at every look.

A waiting table from before the class was kept gains the column when the
store is opened, and a row with no class reads as a request that named none.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from specflo import daemon
from specflo.daemon import poolstore
from specflo.daemon import store as store_module
from specflo.daemon.poolstore import WaitingRequest
from specflo.daemon.products import Products
from specflo.pool import service, waiting
from specflo.pool.config import Member, PoolConfig

from .test_runner import DEFINITION

T0 = "2026-09-19T10:00:00.000+00:00"

ACCEPTS_OPEN = dataclasses.replace(DEFINITION, egress="open")

# The waiting table as it was before a row kept its request's class.
WAITING_TABLE_BEFORE = """
CREATE TABLE pool_waiting (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    pool         TEXT,
    team         TEXT,
    holder_label TEXT NOT NULL,
    arrived      TEXT NOT NULL
);
"""


def open_member(pool_rig) -> Member:
    return dataclasses.replace(pool_rig.hosted_member(), name="open-1", egress="open")


def pool_of(pool_rig, *members: Member, definition=ACCEPTS_OPEN) -> PoolConfig:
    """The pool "rebasers" of *members*, under a definition that accepts class open."""
    return dataclasses.replace(pool_rig.config(*members), definitions=(definition,))


def asks(pool_rig, svc, label: str, egress: str | None = None) -> waiting.Waiting:
    ids = iter([f"request-{label}"])
    return waiting.Waiting(
        svc, "rebasers", holder_label=label, cwd=pool_rig.work, wait=3600,
        egress=egress, mint_id=lambda: next(ids),
    )


def waiting_rows(pool_rig) -> list[WaitingRequest]:
    with pool_rig.store() as store:
        return store.list_waiting()


# -- the row ------------------------------------------------------------------


def test_a_waiting_row_round_trips_the_class_its_request_named(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    named = WaitingRequest(
        id="req-a", pool="workers", team=None, holder_label="a", arrived=T0, egress="open"
    )
    unnamed = WaitingRequest(id="req-b", pool="workers", team=None, holder_label="b", arrived=T0)

    with poolstore.open_pool_store(root) as store:
        store.add_waiting(named)
        store.add_waiting(unnamed)

    with poolstore.open_pool_store(root) as store:
        assert store.list_waiting() == [named, unnamed]
    assert unnamed.egress is None


def test_a_request_that_waits_writes_down_the_class_it_named(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member(), open_member(pool_rig)))
    held = [
        svc.grant("rebasers", holder_label=label, cwd=pool_rig.work, egress="open")
        for label in ("a", "b")
    ]

    assert asks(pool_rig, svc, "c", "open").attempt() is None
    assert asks(pool_rig, svc, "d", "local").attempt() is None

    assert [(row.id, row.egress) for row in waiting_rows(pool_rig)] == [
        ("request-c", "open"), ("request-d", "local"),
    ]
    for grant in held:
        svc.end_lease(grant.lease_id, "released")


def test_a_request_that_named_no_class_writes_down_none(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member()))
    held = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert asks(pool_rig, svc, "b").attempt() is None

    (row,) = waiting_rows(pool_rig)
    assert row.egress is None
    svc.end_lease(held.lease_id, "released")


# -- the order among those that wait -------------------------------------------


def test_an_earlier_request_for_the_open_member_is_not_overtaken_by_a_later_one(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, open_member(pool_rig)))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="open")
    earlier = asks(pool_rig, svc, "b", "open")
    assert earlier.attempt() is None

    svc.end_lease(first.lease_id, "released")

    # it came later, for the member the earlier one waits for
    with pytest.raises(service.NoFreeMember, match="came earlier"):
        svc.grant("rebasers", holder_label="c", cwd=pool_rig.work, egress="open")
    granted = earlier.attempt()
    assert granted.agent == "open-1"
    assert waiting_rows(pool_rig) == []
    svc.end_lease(granted.lease_id, "released")


def test_an_earlier_request_for_a_local_member_holds_up_no_one_at_a_no_train_member(pool_rig):
    svc = pool_rig.service(
        pool_of(pool_rig, pool_rig.local_member(), pool_rig.hosted_member(), definition=DEFINITION)
    )
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, egress="local")
    assert first.agent == "local-1"
    earlier = asks(pool_rig, svc, "b", "local")
    assert earlier.attempt() is None

    # only the no-train member is free, and the earlier request would not take it
    later = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)

    assert later.agent == "hosted-1"
    assert earlier.attempt() is None
    assert [row.id for row in waiting_rows(pool_rig)] == ["request-b"]
    earlier.leave()
    for grant in (first, later):
        svc.end_lease(grant.lease_id, "released")


def test_a_row_with_no_class_is_judged_as_a_request_that_named_none(pool_rig):
    svc = pool_rig.service(pool_of(pool_rig, pool_rig.local_member(), open_member(pool_rig)))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert first.agent == "local-1"
    with pool_rig.store() as store:
        # written as a store from before the class was kept wrote it
        store.connection.execute(
            "INSERT INTO pool_waiting (id, pool, team, holder_label, arrived)"
            " VALUES ('request-old', 'rebasers', NULL, 'b', ?)", (T0,),
        )
        store.connection.commit()
    (row,) = waiting_rows(pool_rig)
    assert row.egress is None

    # the open member is not one a default request takes, so it holds up no one there
    later = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work, egress="open")
    assert later.agent == "open-1"

    svc.end_lease(first.lease_id, "released")
    # and at the local member it does fit, so it goes first
    with pytest.raises(service.NoFreeMember, match="came earlier"):
        svc.grant("rebasers", holder_label="d", cwd=pool_rig.work)
    svc.end_lease(later.lease_id, "released")


# -- a store from before the class was kept ------------------------------------


def test_opening_the_store_on_a_root_with_the_earlier_waiting_table_adds_the_column(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    with store_module.open_store(root) as state:
        Products(state).add("My Thing", repo=None, today="2026-09-06")
        item = state.add_work_item(
            store_module.WorkItem(
                id=0, product="my-thing", title="First", kind="feature", issue=None,
                dev_path="full", status="open", created="2026-09-06",
            )
        )
        products_before = state.list_products()
    path = root / daemon.STATE_STORE_FILENAME
    before = sqlite3.connect(path)
    before.executescript(WAITING_TABLE_BEFORE)
    before.execute(
        "INSERT INTO pool_waiting (id, pool, team, holder_label, arrived)"
        " VALUES ('request-old', 'workers', NULL, 'requester / my-project', ?)", (T0,),
    )
    before.commit()
    before.close()

    with poolstore.open_pool_store(root) as store:
        assert store.list_waiting() == [
            WaitingRequest(
                id="request-old", pool="workers", team=None,
                holder_label="requester / my-project", arrived=T0, egress=None,
            )
        ]
        store.add_waiting(WaitingRequest(
            id="request-new", pool="workers", team=None, holder_label="later",
            arrived=T0, egress="open",
        ))

    columns = [row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(pool_waiting)")]
    assert columns == [
        "seq", "id", "pool", "team", "holder_label", "arrived", "egress", "pinned", "until",
    ]
    # opened again, the column is there and is left alone
    with poolstore.open_pool_store(root) as store:
        assert [(row.id, row.egress) for row in store.list_waiting()] == [
            ("request-old", None), ("request-new", "open"),
        ]
    with store_module.open_store(root) as state:
        assert state.list_products() == products_before
        assert state.list_work_items() == [item]


def test_a_new_store_and_one_that_gained_the_column_keep_the_same_waiting_columns(tmp_path):
    fresh, grown = daemon.prepare_root(tmp_path / "fresh"), daemon.prepare_root(tmp_path / "grown")
    before = sqlite3.connect(grown / daemon.STATE_STORE_FILENAME)
    before.executescript(WAITING_TABLE_BEFORE)
    before.close()

    shapes = []
    for root in (fresh, grown):
        poolstore.open_pool_store(root).close()
        rows = sqlite3.connect(root / daemon.STATE_STORE_FILENAME).execute(
            "PRAGMA table_info(pool_waiting)"
        )
        shapes.append([(row[1], row[2]) for row in rows])

    assert shapes[0] == shapes[1]
