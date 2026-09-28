"""A request that does not fit waits: in arrival order, up to a time, and only while asked for.

A request for a pool with nothing free is not refused when its asker gave it
a time to wait. It is written down as waiting and looked at again at a short
interval, and each look begins with the expiry check, so what frees a slot
may be a release or a lease that ran out of idle time: the one who waits is
the only client there is. Among those that wait the earliest that fits is
served, and one that does not fit holds up no one behind it. When the time is
up the verb fails and names the full pool, and a client that goes away stops
waiting; either way the record of the wait goes.

The order and the time are the service's, so a fake clock and one look at a
time drive them. The wait itself is the daemon's route: a real daemon on a
loopback port, or the application in process where the test must hold its
clock. The members are real processes, as in the request verb's tests.
"""

from __future__ import annotations

import ast
import dataclasses
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from specflo.cli import app
from specflo.daemon.app import create_app
from specflo.daemon.poolstore import WaitingRequest
from specflo.errors import SpecfloError
from specflo.pool import service, waiting
from specflo.service.pool_remote import LEASES_PATH, WAITING_MEDIA_TYPE, RemotePool
from waits import settle, wait_until

from .conftest import FakeClock
from .test_expiry import BACKGROUND, called_names
from .test_lease_request import (  # noqa: F401  (fixtures)
    GONE,
    SlowStart,
    ask_and_read_nothing,
    checkout,
    endings,
    pool_daemon,
    runner,
    write_pool,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"


@pytest.fixture(autouse=True)
def short_interval(monkeypatch):
    """A waiting request is looked at again every few hundredths of a second."""
    monkeypatch.setattr(waiting, "POLL_INTERVAL", 0.05)


def waiting_rows(pool_rig) -> list[WaitingRequest]:
    with pool_rig.store() as store:
        return store.list_waiting()


def two_pools(pool_rig):
    """Two pools of one member each: "rebasers" on local-1, "critics" on hosted-1."""
    local, hosted = pool_rig.local_member(), pool_rig.hosted_member()
    config = pool_rig.config(local)
    (rebasers,) = config.pools
    critics = dataclasses.replace(rebasers, name="critics", members=(hosted.name,))
    return dataclasses.replace(config, members=(local, hosted), pools=(rebasers, critics))


def asks(pool_rig, svc, pool: str, label: str, *, wait: float = 3600) -> waiting.Waiting:
    ids = iter([f"request-{label}"])
    return waiting.Waiting(
        svc, pool, holder_label=label, cwd=pool_rig.work, wait=wait, mint_id=lambda: next(ids)
    )


# -- the service: one look at a time, on a fake clock -------------------------


def test_a_request_that_does_not_fit_is_written_down_as_waiting(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b")

    assert second.attempt() is None

    (row,) = waiting_rows(pool_rig)
    assert (row.id, row.pool, row.team, row.holder_label) == ("request-b", "rebasers", None, "b")
    assert row.arrived == service._text(pool_rig.clock())
    # a second look writes nothing more
    assert second.attempt() is None
    assert len(waiting_rows(pool_rig)) == 1


def test_a_request_that_fits_is_granted_at_once_and_never_waits(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    grant = asks(pool_rig, svc, "rebasers", "a").attempt()

    assert grant.agent == "local-1"
    assert waiting_rows(pool_rig) == []


def test_without_a_time_to_wait_a_full_pool_is_refused_as_before(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember, match="rebasers"):
        asks(pool_rig, svc, "rebasers", "b", wait=0).attempt()

    assert waiting_rows(pool_rig) == []


def test_the_earliest_waiting_request_that_fits_is_granted(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    earlier, later = asks(pool_rig, svc, "rebasers", "b"), asks(pool_rig, svc, "rebasers", "c")
    assert earlier.attempt() is None
    assert later.attempt() is None

    svc.end_lease(first.lease_id, "released")

    # the later one looks first, and leaves the member to the earlier one
    assert later.attempt() is None
    # so does a request that has only now arrived
    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="d", cwd=pool_rig.work)
    granted = earlier.attempt()
    assert granted.agent == "local-1"
    assert [row.id for row in waiting_rows(pool_rig)] == ["request-c"]

    svc.end_lease(granted.lease_id, "released")
    assert later.attempt().agent == "local-1"
    assert waiting_rows(pool_rig) == []


def test_a_request_that_does_not_fit_does_not_block_a_later_one_that_does(pool_rig):
    svc = pool_rig.service(two_pools(pool_rig))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    held = svc.grant("critics", holder_label="b", cwd=pool_rig.work)
    earlier, later = asks(pool_rig, svc, "rebasers", "c"), asks(pool_rig, svc, "critics", "d")
    assert earlier.attempt() is None
    assert later.attempt() is None

    svc.end_lease(held.lease_id, "released")

    assert earlier.attempt() is None
    assert later.attempt().agent == "hosted-1"
    assert [row.id for row in waiting_rows(pool_rig)] == ["request-c"]


def test_when_the_time_is_up_the_request_fails_naming_the_full_pool_and_leaves(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b", wait=2)
    assert second.attempt() is None

    pool_rig.clock.advance(seconds=1)
    assert second.attempt() is None
    pool_rig.clock.advance(seconds=1)
    with pytest.raises(waiting.WaitTimeout) as refused:
        second.attempt()

    assert "rebasers" in str(refused.value)
    assert "full" in str(refused.value)
    assert waiting_rows(pool_rig) == []


def test_a_request_that_leaves_is_no_longer_waiting(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b")
    assert second.attempt() is None

    second.leave()
    second.leave()

    assert waiting_rows(pool_rig) == []


# -- a row and the time its wait is up ----------------------------------------


def orphan(pool_rig, *, until: datetime | None, pool: str = "rebasers") -> None:
    """A waiting row no one waits on: its asker went away and did not take it out."""
    with pool_rig.store() as store:
        store.add_waiting(WaitingRequest(
            id="request-gone", pool=pool, team=None, holder_label="gone",
            arrived=service._text(pool_rig.clock()),
            until=None if until is None else service._text(until),
        ))


def test_a_waiting_row_keeps_the_time_its_wait_is_up(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert asks(pool_rig, svc, "rebasers", "b", wait=waiting.WAIT_MAX).attempt() is None

    (row,) = waiting_rows(pool_rig)
    assert row.until == service._text(pool_rig.clock() + timedelta(seconds=waiting.WAIT_MAX))


def test_a_wait_too_long_to_count_writes_no_row(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b", wait=10**12)

    with pytest.raises(OverflowError):
        second.attempt()

    # the time is worked out before the row is written, so nothing stays behind
    assert waiting_rows(pool_rig) == []


def test_a_row_long_past_its_time_holds_no_pool_and_is_taken_out(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    orphan(pool_rig, until=pool_rig.clock() + timedelta(seconds=2))
    svc.end_lease(first.lease_id, "released")

    # while its time runs the row is served first, as any row is
    with pytest.raises(service.NoFreeMember, match="came earlier"):
        svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)
    pool_rig.clock.advance(seconds=2 + service.WAITING_GRACE + 1)

    assert svc.grant("rebasers", holder_label="c", cwd=pool_rig.work).agent == "local-1"
    assert waiting_rows(pool_rig) == []


def test_a_row_only_just_past_its_time_is_still_served_first(pool_rig):
    # its asker takes it out at its next look, which comes a little after the time
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    earlier = asks(pool_rig, svc, "rebasers", "b", wait=2)
    assert earlier.attempt() is None
    svc.end_lease(first.lease_id, "released")
    pool_rig.clock.advance(seconds=2 + service.WAITING_GRACE)

    with pytest.raises(service.NoFreeMember, match="came earlier"):
        svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)

    assert [row.id for row in waiting_rows(pool_rig)] == ["request-b"]
    assert earlier.attempt().agent == "local-1"


def test_a_row_with_no_time_is_served_first_however_old(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    orphan(pool_rig, until=None)
    pool_rig.clock.advance(days=30)

    with pytest.raises(service.NoFreeMember, match="came earlier"):
        svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)

    assert [row.id for row in waiting_rows(pool_rig)] == ["request-gone"]


def test_a_request_that_looks_is_not_given_up_at_its_own_look(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    earlier = asks(pool_rig, svc, "rebasers", "b", wait=2)
    later = asks(pool_rig, svc, "rebasers", "c")
    assert earlier.attempt() is None
    assert later.attempt() is None
    svc.end_lease(first.lease_id, "released")
    pool_rig.clock.advance(minutes=30)

    # it asks, so it is there: the member is its own and not the later one's
    assert earlier.attempt().agent == "local-1"
    assert [row.id for row in waiting_rows(pool_rig)] == ["request-c"]


def test_a_row_long_past_its_time_keeps_no_idle_lease_from_a_later_request(
    pool_rig, monkeypatch
):
    monkeypatch.setattr(service.runner, "status", lambda name: None)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member(), preempt_after=300))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    orphan(pool_rig, until=pool_rig.clock() + timedelta(seconds=2))
    later = asks(pool_rig, svc, "rebasers", "c")
    assert later.attempt() is None
    pool_rig.clock.advance(minutes=6)

    granted = later.attempt()

    assert granted is not None
    with pool_rig.store() as store:
        assert store.get_lease(old.lease_id).state == "preempted"
    assert waiting_rows(pool_rig) == []


# -- the daemon: a request that blocks ----------------------------------------


@pytest.fixture
def holder(pool_daemon):
    """A client of the daemon, for the one that holds the pool's only member."""
    client = RemotePool(pool_daemon["url"], pool_daemon["token"], timeout=60)
    yield client
    client.client.close()


def ask_in_background(pool_daemon, pool_rig, *, wait: int, timeout: float = 60.0):
    """A request made by a client of its own, in a thread; what came of it is
    in the box when the thread ends."""
    box: dict = {}

    def ask() -> None:
        client = RemotePool(pool_daemon["url"], pool_daemon["token"], timeout=timeout)
        try:
            box["grant"] = client.request("rebasers", cwd=str(pool_rig.work), wait=wait)
        except SpecfloError as exc:
            box["error"] = exc
        finally:
            client.client.close()

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    return thread, box


def test_a_second_request_blocks_as_waiting_and_a_release_grants_it_within_5_s(
    pool_daemon, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))

    thread, box = ask_in_background(pool_daemon, pool_rig, wait=60)

    assert wait_until(lambda: len(waiting_rows(pool_rig)) == 1)
    (row,) = waiting_rows(pool_rig)
    assert row.pool == "rebasers"
    assert "developer" in row.holder_label
    settle(0.3)
    assert thread.is_alive() and not box

    released = time.monotonic()
    holder.release(first.lease_id, token=first.token)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert time.monotonic() - released < 5
    assert box["grant"].agent == "local-1"
    assert box["grant"].lease_id != first.lease_id
    assert waiting_rows(pool_rig) == []
    with pool_rig.store() as store:
        assert [lease.id for lease in store.list_leases(state="active")] == [box["grant"].lease_id]


def test_a_lease_passing_its_idle_limit_grants_the_waiting_request_with_no_other_client(
    pool_rig,
):
    write_pool(pool_rig)
    pool_rig.serve_bridge()
    application = create_app(pool_rig.root)
    # a member's host stamps with its own clock, so the fake one starts at the real time
    clock = FakeClock(datetime.now(timezone.utc))
    application.state.pool.clock = clock
    headers = {"Authorization": f"Bearer {_token(pool_rig.root)}"}
    body = {"pool": "rebasers", "cwd": str(pool_rig.work)}
    first = TestClient(application).post(LEASES_PATH, json=body, headers=headers)
    assert first.status_code == 200, first.text
    box: dict = {}

    def ask() -> None:
        box["response"] = TestClient(application).post(
            LEASES_PATH, json={**body, "wait": 3600}, headers=headers
        )

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    assert wait_until(lambda: len(waiting_rows(pool_rig)) == 1)

    clock.advance(minutes=9)
    settle(0.3)
    assert thread.is_alive()

    # nothing but the time moves: the one who waits is the only client
    clock.advance(minutes=2)
    passed = time.monotonic()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert time.monotonic() - passed < 5
    assert box["response"].status_code == 200, box["response"].text
    assert waiting_rows(pool_rig) == []
    with pool_rig.store() as store:
        assert store.get_lease(first.json()["result"]["lease_id"]).state == "expired"
        (active,) = store.list_leases(state="active")
    assert active.id == box["response"].json()["result"]["lease_id"]


def _token(root: Path) -> str:
    from specflo.daemon import auth

    return auth.mint_token(root, "developer")


def test_a_client_that_goes_away_while_it_waits_leaves_no_waiting_record(
    pool_daemon, pool_rig, holder
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))

    # the client gives up after a second and closes its connection
    thread, box = ask_in_background(pool_daemon, pool_rig, wait=600, timeout=1.0)
    assert wait_until(lambda: len(waiting_rows(pool_rig)) == 1)
    thread.join(timeout=10)
    assert "error" in box

    assert wait_until(lambda: waiting_rows(pool_rig) == [], timeout=5)
    # and what frees later is not granted to it
    holder.release(first.lease_id, token=first.token)
    settle(0.3)
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []


@pytest.mark.parametrize("accept", [None, WAITING_MEDIA_TYPE], ids=["plain", "told"])
def test_a_client_that_goes_away_while_its_member_starts_leaves_no_lease_out(
    pool_daemon, pool_rig, holder, monkeypatch, accept
):
    first = holder.request("rebasers", cwd=str(pool_rig.work))
    # the one that waits is granted a member that starts slowly
    slow = SlowStart(monkeypatch)
    body = {"pool": "rebasers", "cwd": str(pool_rig.work), "wait": 600}
    connection = ask_and_read_nothing(pool_daemon, body, accept=accept)
    assert wait_until(lambda: len(waiting_rows(pool_rig)) == 1)

    holder.release(first.lease_id, token=first.token)
    slow.requester_goes_away(connection)

    def second_ended() -> bool:
        with pool_rig.store() as store:
            leases = store.list_leases()
        return len(leases) == 2 and leases[-1].state != "active"

    assert wait_until(second_ended, timeout=10, message="a lease no one holds is still out")
    with pool_rig.store() as store:
        second = store.list_leases()[-1]
    kind, cause = endings(pool_rig, second.id)[-1]
    assert (second.state, kind) == ("released", "released") and GONE in cause
    assert waiting_rows(pool_rig) == []
    # the slot is free at once: the next request does not wait for it
    again = holder.request("rebasers", cwd=str(pool_rig.work))
    assert again.agent == "local-1"


def test_the_route_refuses_a_full_pool_at_once_when_no_wait_is_asked(pool_rig, holder):
    holder.request("rebasers", cwd=str(pool_rig.work))

    with pytest.raises(SpecfloError, match="rebasers"):
        holder.request("rebasers", cwd=str(pool_rig.work))

    assert waiting_rows(pool_rig) == []


def test_the_route_refuses_a_wait_that_is_no_number_of_seconds(pool_daemon, pool_rig):
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    good = {"pool": "rebasers", "cwd": str(pool_rig.work)}

    for wait in (-1, True, "2", 1.5):
        response = client.post(LEASES_PATH, json={**good, "wait": wait})
        assert response.status_code == 422, (wait, response.text)
    assert pool_rig.pane_names() == []
    assert waiting_rows(pool_rig) == []


def test_the_route_refuses_a_wait_above_the_maximum_and_writes_no_row(pool_rig):
    write_pool(pool_rig)
    pool_rig.serve_bridge()
    client = TestClient(create_app(pool_rig.root))
    client.headers["Authorization"] = f"Bearer {_token(pool_rig.root)}"
    good = {"pool": "rebasers", "cwd": str(pool_rig.work)}
    # the pool is full, so a request that may wait would be written down
    assert client.post(LEASES_PATH, json=good).status_code == 200

    for wait in (10**12, waiting.WAIT_MAX + 1):
        response = client.post(LEASES_PATH, json={**good, "wait": wait})
        assert response.status_code == 400, (wait, response.text)
        assert f"{waiting.WAIT_MAX} s" in response.json()["detail"]
        assert waiting_rows(pool_rig) == []
    # and so is a team's, which comes in by the same route
    response = client.post(
        LEASES_PATH, json={"team": "pair", "cwd": str(pool_rig.work), "wait": 10**12}
    )
    assert response.status_code == 400, response.text
    assert f"{waiting.WAIT_MAX} s" in response.json()["detail"]
    assert waiting_rows(pool_rig) == []


def test_waiting_records_a_stopped_daemon_left_are_cleared_when_the_pool_opens(pool_rig):
    write_pool(pool_rig)
    with pool_rig.store() as store:
        store.add_waiting(WaitingRequest(
            id="request-old", pool="rebasers", team=None, holder_label="gone",
            arrived="2026-03-01T12:00:00.000+00:00",
        ))

    create_app(pool_rig.root)

    assert waiting_rows(pool_rig) == []


# -- the verb -----------------------------------------------------------------


def test_wait_2_exits_non_zero_naming_the_full_pool_and_removes_the_waiting_record(
    checkout, pool_rig
):
    held = runner.invoke(app, ["lease", "request", "rebasers"])
    assert held.exit_code == 0, held.output
    token_file = checkout / ".specflo" / "leases" / "local-1.token"
    kept = token_file.read_text()

    asked = time.monotonic()
    result = runner.invoke(app, ["lease", "request", "rebasers", "--wait", "2"])

    assert result.exit_code != 0
    assert 2 <= time.monotonic() - asked < 10
    output = " ".join(result.output.split())
    assert "pool 'rebasers' is full" in output
    assert "2 s" in output
    assert "Traceback" not in result.output
    assert waiting_rows(pool_rig) == []
    assert token_file.read_text() == kept


def test_a_wait_below_zero_is_refused_by_the_verb(checkout, pool_rig):
    result = runner.invoke(app, ["lease", "request", "rebasers", "--wait", "-1"])

    assert result.exit_code != 0
    assert pool_rig.pane_names() == []


def test_a_wait_above_the_maximum_is_refused_by_the_verb_and_writes_no_row(checkout, pool_rig):
    held = runner.invoke(app, ["lease", "request", "rebasers"])
    assert held.exit_code == 0, held.output

    asked = time.monotonic()
    result = runner.invoke(
        app, ["lease", "request", "rebasers", "--wait", str(waiting.WAIT_MAX + 1)]
    )

    # refused at once, by the verb itself: nothing is taken in to wait
    assert time.monotonic() - asked < 10

    assert result.exit_code != 0
    output = " ".join(result.output.split())
    assert str(waiting.WAIT_MAX) in output
    assert "Traceback" not in result.output
    assert waiting_rows(pool_rig) == []
    assert pool_rig.pane_names() == ["local-1"]


# -- structure ----------------------------------------------------------------


def test_the_look_is_short_enough_to_grant_within_5_s(monkeypatch):
    monkeypatch.undo()
    assert 0 < waiting.POLL_INTERVAL <= 2


def test_a_row_is_given_up_only_after_its_asker_has_had_its_look():
    # the asker's last look comes a poll interval after the one before, 2 s at
    # the most, and may wait its turn behind a member that starts and one that stops
    slowest_turn = service.runner.START_TIMEOUT + service.runner.STOP_TIMEOUT
    assert service.WAITING_GRACE >= 2 + slowest_turn


def test_nothing_in_the_waiting_module_runs_by_itself():
    # the rule of the lease modules holds here too: a waiting request is
    # looked at by the request that waits, and nothing else looks
    tree = ast.parse((SRC / "pool" / "waiting.py").read_text(encoding="utf-8"))
    assert not called_names(tree) & (BACKGROUND | {"sleep"})
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            imported.add((node.module or "").split(".")[0])
    assert not imported & {
        "asyncio", "sched", "concurrent", "multiprocessing", "signal", "threading", "time",
    }
