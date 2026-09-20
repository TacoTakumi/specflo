"""The pool store: leases, their transitions and the pool's other records, in the daemon's store.

Pool state is rows, like products and work items: it lives in the SQLite
file under the daemon root, behind one interface, in tables of its own. The
store holds what it is given and keeps no clock; every time here is a value
the caller passes in.
"""

import dataclasses
import sqlite3

import pytest

from specflo import daemon
from specflo.daemon import poolstore
from specflo.daemon import store as store_module
from specflo.daemon.poolstore import (
    Account,
    AccountFigures,
    ConsoleAttachment,
    Lease,
    Reload,
    Resource,
    Transition,
    WaitingRequest,
)
from specflo.daemon.products import Products
from specflo.daemon.store import Conflict

T0 = "2026-09-19T10:00:00.000+00:00"
T1 = "2026-09-19T10:09:00.000+00:00"
T2 = "2026-09-19T10:20:00.000+00:00"


@pytest.fixture
def root(tmp_path):
    return daemon.prepare_root(tmp_path / "daemon")


@pytest.fixture
def store(root):
    with poolstore.open_pool_store(root) as store:
        yield store


def lease(**changes):
    base = Lease(
        id="lease-1",
        team_lease_id=None,
        holder_hash="9f86d081884c7d65",
        holder_label="requester / my-project",
        member="gpu-worker-1",
        pool="workers",
        resources=(
            Resource("pool", "workers"),
            Resource("member", "gpu-worker-1"),
            Resource("model", "qwen-coder"),
        ),
        acquired=T0,
        last_activity=T0,
        idle_limit=600,
        state="active",
    )
    return dataclasses.replace(base, **changes)


# --- where it lives ----------------------------------------------------------


def test_the_pool_store_is_the_daemon_roots_sqlite_file(root):
    with poolstore.open_pool_store(root) as store:
        assert isinstance(store, poolstore.SqlitePoolStore)
        assert store.path == root / daemon.STATE_STORE_FILENAME
        assert store.list_leases() == []


def test_opening_it_on_an_existing_root_leaves_products_and_work_items_untouched(root):
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
    schema_sql = (
        "select name, sql from sqlite_master where name in ('products', 'work_items', 'pieces')"
    )
    schema_before = sqlite3.connect(path).execute(schema_sql).fetchall()

    with poolstore.open_pool_store(root) as store:
        store.add_lease(lease())

    assert sqlite3.connect(path).execute(schema_sql).fetchall() == schema_before
    with store_module.open_store(root) as state:
        assert state.list_products() == products_before
        assert state.list_work_items() == [item]
    with poolstore.open_pool_store(root) as store:
        assert store.get_lease("lease-1") == lease()


# --- leases ------------------------------------------------------------------


def test_a_lease_row_round_trips_every_field(store):
    team_member = lease(id="lease-2", team_lease_id="team-7", member="critic-1", pool="critics")
    store.add_lease(lease())
    store.add_lease(team_member)

    assert store.get_lease("lease-1") == lease()
    assert store.get_lease("lease-2") == team_member
    assert store.get_lease("lease-9") is None


def test_a_lease_survives_closing_the_store(root):
    with poolstore.open_pool_store(root) as store:
        store.add_lease(lease())
    with poolstore.open_pool_store(root) as store:
        assert store.get_lease("lease-1") == lease()


def test_a_taken_lease_id_is_a_conflict(store):
    store.add_lease(lease())
    with pytest.raises(Conflict):
        store.add_lease(lease(member="gpu-worker-2"))
    assert store.get_lease("lease-1").member == "gpu-worker-1"


def test_leases_list_in_grant_order_and_filter(store):
    first = lease(id="b")
    second = lease(id="a", pool="critics", member="critic-1", team_lease_id="team-7")
    third = lease(id="c", state="released")
    for one in (first, second, third):
        store.add_lease(one)

    assert store.list_leases() == [first, second, third]
    assert store.list_leases(state="active") == [first, second]
    assert store.list_leases(pool="critics") == [second]
    assert store.list_leases(member="gpu-worker-1") == [first, third]
    assert store.list_leases(team_lease_id="team-7") == [second]
    assert store.list_leases(state="active", pool="workers") == [first]


def test_last_activity_is_replaced_on_the_row(store):
    store.add_lease(lease())

    assert store.set_lease_activity("lease-1", T1) == lease(last_activity=T1)
    assert store.get_lease("lease-1").last_activity == T1
    assert store.set_lease_activity("lease-9", T1) is None


# --- transitions -------------------------------------------------------------


def test_a_transition_is_appended_with_its_time_and_cause(store):
    store.add_lease(lease())

    granted = store.record_transition(
        Transition(id=0, lease_id="lease-1", kind="granted", time=T0, cause="requested")
    )
    released = store.record_transition(
        Transition(id=0, lease_id="lease-1", kind="released", time=T1, cause="released by holder")
    )

    assert (granted.kind, granted.time, granted.cause) == ("granted", T0, "requested")
    assert released.id > granted.id
    assert store.list_transitions() == [granted, released]
    assert store.list_transitions(lease_id="lease-1") == [granted, released]
    assert store.list_transitions(lease_id="lease-9") == []


def test_a_transition_moves_its_lease_to_the_state_it_names(store):
    store.add_lease(lease())

    store.record_transition(
        Transition(id=0, lease_id="lease-1", kind="granted", time=T0, cause="requested")
    )
    assert store.get_lease("lease-1").state == "active"

    for kind in ("released", "expired", "preempted"):
        store.add_lease(lease(id=kind))
        store.record_transition(Transition(id=0, lease_id=kind, kind=kind, time=T1, cause=kind))
        assert store.get_lease(kind).state == kind


def test_a_transition_that_expects_another_state_writes_nothing(store):
    store.add_lease(lease())
    expired = store.record_transition(
        Transition(id=0, lease_id="lease-1", kind="expired", time=T1, cause="idle for 600 s"),
        expect="active",
    )

    late = store.record_transition(
        Transition(id=0, lease_id="lease-1", kind="released", time=T2, cause="released by holder"),
        expect="active",
    )

    assert late is None
    assert store.get_lease("lease-1").state == "expired"
    assert store.list_transitions() == [expired]


def test_a_transition_of_an_unknown_lease_or_kind_writes_nothing(store):
    store.add_lease(lease())

    assert store.record_transition(
        Transition(id=0, lease_id="lease-9", kind="released", time=T1, cause="x")
    ) is None
    with pytest.raises(ValueError, match="renewed"):
        store.record_transition(
            Transition(id=0, lease_id="lease-1", kind="renewed", time=T1, cause="x")
        )
    assert store.list_transitions() == []


def test_the_latest_transitions_come_back_oldest_first(store):
    store.add_lease(lease())
    records = [
        store.record_transition(
            Transition(id=0, lease_id="lease-1", kind="granted", time=T0, cause=f"cause {n}")
        )
        for n in range(4)
    ]

    assert store.list_transitions(limit=2) == records[2:]


# --- waiting requests --------------------------------------------------------


def test_waiting_requests_round_trip_in_arrival_order(store):
    for_pool = WaitingRequest(
        id="req-b", pool="workers", team=None, holder_label="requester / my-project", arrived=T0
    )
    for_team = WaitingRequest(
        id="req-a", pool=None, team="pair", holder_label="requester / other", arrived=T1
    )
    store.add_waiting(for_pool)
    store.add_waiting(for_team)

    assert store.list_waiting() == [for_pool, for_team]
    assert store.list_waiting(pool="workers") == [for_pool]
    with pytest.raises(Conflict):
        store.add_waiting(for_pool)


def test_a_waiting_request_that_leaves_the_queue_leaves_no_record(store):
    store.add_waiting(
        WaitingRequest(id="req-1", pool="workers", team=None, holder_label="r", arrived=T0)
    )

    assert store.remove_waiting("req-1") is True
    assert store.remove_waiting("req-1") is False
    assert store.list_waiting() == []


def test_a_waiting_request_keeps_the_time_its_wait_is_up(store):
    timed = WaitingRequest(
        id="req-1", pool="workers", team=None, holder_label="r", arrived=T0, until=T1
    )
    untimed = WaitingRequest(id="req-2", pool="workers", team=None, holder_label="r", arrived=T0)
    store.add_waiting(timed)
    store.add_waiting(untimed)

    assert [row.until for row in store.list_waiting()] == [T1, None]
    assert store.list_waiting() == [timed, untimed]


# The waiting table as it was before a row kept the time its wait is up.
WAITING_TABLE_BEFORE_UNTIL = """
CREATE TABLE pool_waiting (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    pool         TEXT,
    team         TEXT,
    holder_label TEXT NOT NULL,
    arrived      TEXT NOT NULL,
    egress       TEXT,
    pinned       TEXT
);
"""


def test_a_store_from_before_the_time_was_kept_gains_the_column_and_its_rows_read_as_none(root):
    path = root / daemon.STATE_STORE_FILENAME
    before = sqlite3.connect(path)
    before.executescript(WAITING_TABLE_BEFORE_UNTIL)
    before.execute(
        "INSERT INTO pool_waiting (id, pool, team, holder_label, arrived, egress, pinned)"
        " VALUES ('request-old', 'workers', NULL, 'requester / my-project', ?, 'open', NULL)",
        (T0,),
    )
    before.commit()
    before.close()

    with poolstore.open_pool_store(root) as store:
        columns = [
            row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(pool_waiting)")
        ]
        assert columns[-1] == "until"
        assert store.list_waiting() == [
            WaitingRequest(
                id="request-old", pool="workers", team=None,
                holder_label="requester / my-project", arrived=T0, egress="open", until=None,
            )
        ]
        store.add_waiting(WaitingRequest(
            id="request-new", pool="workers", team=None, holder_label="later",
            arrived=T1, until=T2,
        ))

    # opened again, the column is there and is left alone
    with poolstore.open_pool_store(root) as store:
        assert [(row.id, row.until) for row in store.list_waiting()] == [
            ("request-old", None), ("request-new", T2),
        ]


def test_a_new_store_and_one_that_gained_the_time_keep_the_same_waiting_columns(tmp_path):
    fresh, grown = daemon.prepare_root(tmp_path / "fresh"), daemon.prepare_root(tmp_path / "grown")
    before = sqlite3.connect(grown / daemon.STATE_STORE_FILENAME)
    before.executescript(WAITING_TABLE_BEFORE_UNTIL)
    before.close()

    shapes = []
    for root in (fresh, grown):
        poolstore.open_pool_store(root).close()
        rows = sqlite3.connect(root / daemon.STATE_STORE_FILENAME).execute(
            "PRAGMA table_info(pool_waiting)"
        )
        shapes.append([(row[1], row[2]) for row in rows])

    assert shapes[0] == shapes[1]
    assert ("until", "TEXT") in shapes[0]


# --- accounts ----------------------------------------------------------------


def test_an_account_nobody_wrote_about_is_unknown(store):
    assert store.get_account("main") is None
    assert store.list_accounts() == []


def test_an_account_round_trips_closed_with_its_reopen_time_and_open_again(store):
    closed = store.set_account_closed("main", reopen=T2)

    assert closed == Account(name="main", closed=True, reopen=T2, figures=None)
    assert store.get_account("main") == closed

    assert store.set_account_open("main") == Account(
        name="main", closed=False, reopen=None, figures=None
    )
    assert store.get_account("main").closed is False


def test_account_figures_round_trip_and_leave_the_closed_state_alone(store):
    figures = AccountFigures(
        usage=1.25, limit=10.0, remaining=8.75, free_requests=0, read_at=T0, read_error=None
    )
    store.set_account_closed("main", reopen=T2)

    assert store.set_account_figures("main", figures) == Account(
        name="main", closed=True, reopen=T2, figures=figures
    )

    failed = AccountFigures(
        usage=None, limit=None, remaining=None, free_requests=None,
        read_at=T1, read_error="provider unreachable",
    )
    store.set_account_figures("spare", failed)
    assert store.get_account("spare") == Account(
        name="spare", closed=False, reopen=None, figures=failed
    )
    store.set_account_closed("spare", reopen=T2)
    assert store.get_account("spare").figures == failed
    assert [account.name for account in store.list_accounts()] == ["main", "spare"]


# --- reloads -----------------------------------------------------------------


def test_member_reload_records_round_trip(store):
    first = store.add_reload(Reload(id=0, member="gpu-worker-1", model="qwen-coder", time=T1))
    second = store.add_reload(Reload(id=0, member="critic-1", model="glm-air", time=T2))

    assert (first.member, first.model, first.time) == ("gpu-worker-1", "qwen-coder", T1)
    assert store.list_reloads() == [first, second]
    assert store.list_reloads(member="critic-1") == [second]


def test_reload_data_reads_unavailable_until_the_store_is_told_otherwise(store):
    assert store.reload_data_available() is False

    store.set_reload_data_available(True)
    assert store.reload_data_available() is True

    store.set_reload_data_available(False)
    assert store.reload_data_available() is False


# --- console attachments -----------------------------------------------------


def test_a_console_attachment_round_trips_and_drains(store):
    attachment = ConsoleAttachment(slot="robs-console", agent="console-1", attached=T0)
    store.attach_console(attachment)

    assert store.get_console("robs-console") == attachment
    assert attachment.draining is False
    assert store.list_consoles() == [attachment]
    with pytest.raises(Conflict):
        store.attach_console(dataclasses.replace(attachment, agent="console-2"))

    draining = store.set_console_draining("robs-console")
    assert draining == dataclasses.replace(attachment, draining=True)
    assert store.get_console("robs-console") == draining
    assert store.set_console_draining("other-console") is None


def test_a_detached_console_leaves_no_attachment(store):
    store.attach_console(ConsoleAttachment(slot="robs-console", agent="console-1", attached=T0))

    assert store.detach_console("robs-console") is True
    assert store.detach_console("robs-console") is False
    assert store.get_console("robs-console") is None
    assert store.list_consoles() == []
