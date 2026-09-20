"""Lease expiry: computed by whoever reads the lease, acted on when the pool is served.

A lease is renewed by what its holder does and by a turn that runs, never by
a verb of its own: the agent host stamps the time of both in its status file.
The last activity of a lease is the later of its row's time and that stamp,
and a member that is working counts as active now. A lease idle for its limit
has expired; the service ends it, through the one function that ends a lease,
at the start of whatever it is next asked to do. Nothing runs in between.

The rule itself is a function of the lease row, the status record and the
time, so a fake clock drives it. Where a member's real host is read, the fake
clock starts at the real time, because the host stamps with its own.
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from specflo.agent.cli import agent_app
from specflo.agent.client import connect
from specflo.cli import app
from specflo.daemon.poolstore import Lease, Resource
from specflo.pool import expiry, runner, service, waiting
from specflo.pool.service import Grant

from .conftest import START
from .test_runner import pid_alive, wait_until

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"
TEN_MINUTES = 600


def at(minute: float) -> datetime:
    return START + timedelta(minutes=minute)


def text(time: datetime) -> str:
    return time.isoformat(timespec="milliseconds")


def lease_row(*, last_activity: float = 0, idle_limit: int = TEN_MINUTES) -> Lease:
    """An active lease whose row was last touched at that minute."""
    return Lease(
        id="lease-1", team_lease_id=None, holder_hash="0" * 64, holder_label="a",
        member="local-1", pool="rebasers",
        resources=(Resource("pool", "rebasers"), Resource("member", "local-1")),
        acquired=text(at(0)), last_activity=text(at(last_activity)),
        idle_limit=idle_limit, state="active",
    )


def host_status(state: str, minute: float) -> dict:
    """What the agent host's status file says: its state, stamped at that minute."""
    return {"name": "local-1", "state": state, "last_activity": text(at(minute))}


# -- the rule ----------------------------------------------------------------


def test_last_activity_is_the_later_of_the_row_and_the_status_file():
    assert expiry.last_activity(lease_row(), host_status("idle", 9), at(15)) == at(9)
    late_row = lease_row(last_activity=9)
    assert expiry.last_activity(late_row, host_status("idle", 3), at(15)) == at(9)


def test_a_working_member_is_active_now():
    # the host stamps a running turn only when pi says something
    assert expiry.last_activity(lease_row(), host_status("working", 5), at(40)) == at(40)
    assert not expiry.expired(lease_row(), host_status("working", 5), at(40))


def test_with_no_activity_a_lease_has_expired_at_minute_11():
    status = host_status("idle", 0)
    assert not expiry.expired(lease_row(), status, at(9))
    assert expiry.expired(lease_row(), status, at(11))


def test_a_lease_idle_for_exactly_its_limit_has_expired():
    assert not expiry.expired(lease_row(), None, at(10) - timedelta(milliseconds=1))
    assert expiry.expired(lease_row(), None, at(10))


def test_a_holder_verb_at_minute_9_keeps_the_lease_active_at_minute_15():
    status = host_status("idle", 9)
    assert not expiry.expired(lease_row(), status, at(15))
    assert expiry.expired(lease_row(), status, at(19))


def test_a_turn_from_minute_5_to_25_keeps_the_lease_active_at_minute_30():
    lease = lease_row()
    # mid-turn, with the stamp as old as the prompt that began it
    assert not expiry.expired(lease, host_status("working", 5), at(16))
    # the end of the turn is a state change, which the host stamps
    settled = host_status("idle", 25)
    assert not expiry.expired(lease, settled, at(30))
    assert expiry.expired(lease, settled, at(35))


def test_the_idle_limit_is_the_leases_own():
    long_lease = lease_row(idle_limit=4 * 3600)
    assert not expiry.expired(long_lease, None, at(239))
    assert expiry.expired(long_lease, None, at(240))


@pytest.mark.parametrize(
    "status",
    [None, {}, {"state": "idle", "last_activity": None}, {"state": "idle", "last_activity": "soon"}],
    ids=["no-file", "empty", "no-stamp", "not-a-time"],
)
def test_a_status_that_tells_no_time_leaves_the_rows_time(status):
    assert expiry.last_activity(lease_row(last_activity=2), status, at(15)) == at(2)
    assert expiry.expired(lease_row(last_activity=2), status, at(12))


def test_times_are_compared_as_times_not_as_text():
    # minute 9 written in another zone: earlier as text, later as a time
    elsewhere = at(9).astimezone(timezone(timedelta(hours=-5))).isoformat()
    status = {"state": "idle", "last_activity": elsewhere}
    assert elsewhere < lease_row().last_activity
    assert expiry.last_activity(lease_row(), status, at(15)) == at(9)
    zulu = {"state": "idle", "last_activity": "2026-03-01T12:09:00Z"}
    assert expiry.last_activity(lease_row(), zulu, at(15)) == at(9)


# -- the service acts on it when it is asked for something --------------------


def prompt(grant: Grant, message: str):
    return CliRunner().invoke(
        agent_app, ["prompt", grant.agent, message, "--lease-token", grant.token]
    )


def real_time(pool_rig) -> None:
    """Start the fake clock at the real time: a member's host stamps with its own."""
    pool_rig.clock.now = datetime.now(timezone.utc)


def lease_state(pool_rig, lease_id: str) -> str:
    with pool_rig.store() as store:
        return store.get_lease(lease_id).state


def test_an_idle_lease_is_ended_as_expired_by_the_next_check(pool_rig):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    status = pool_rig.status(grant.agent)

    pool_rig.clock.advance(minutes=9)
    assert svc.expire_due() == []
    assert lease_state(pool_rig, grant.lease_id) == "active"
    assert pid_alive(status["pi_pid"])

    pool_rig.clock.advance(minutes=2)
    ended = svc.expire_due()

    assert [(e.lease_id, e.state, e.cause) for e in ended] == [
        (grant.lease_id, "expired", "expired")
    ]
    assert ended[0].time == service._text(pool_rig.clock())
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).state == "expired"
        last = store.list_transitions(lease_id=grant.lease_id)[-1]
    assert (last.kind, last.cause) == ("expired", "expired")
    assert wait_until(lambda: not pid_alive(status["pi_pid"]))
    assert pool_rig.pane_names() == []
    refused = prompt(grant, "still there?")
    assert refused.exit_code != 0
    assert "lease expired" in refused.stderr
    # nothing is left to expire
    assert svc.expire_due() == []


def test_a_grant_checks_expiry_first_so_an_expired_leases_slot_is_free(pool_rig):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=9)
    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    pool_rig.clock.advance(minutes=2)
    second = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    assert second.agent == first.agent
    with pool_rig.store() as store:
        assert store.get_lease(first.lease_id).state == "expired"
        assert store.list_transitions(lease_id=first.lease_id)[-1].cause == "expired"
        assert store.get_lease(second.lease_id).state == "active"
    svc.end_lease(second.lease_id, "released")


def test_ending_a_lease_checks_expiry_first(pool_rig):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)

    # the release comes too late: the lease had expired, and is reported so
    ended = svc.end_lease(grant.lease_id, "released")

    assert (ended.state, ended.cause) == ("expired", "expired")
    with pool_rig.store() as store:
        kinds = [t.kind for t in store.list_transitions(lease_id=grant.lease_id)]
    assert kinds == ["granted", "expired"]
    assert pool_rig.pane_names() == []


def test_a_running_turn_keeps_its_lease_and_an_idle_neighbour_expires(pool_rig):
    real_time(pool_rig)
    pool_rig.scenario(mode="never_settle")
    config = pool_rig.config(pool_rig.local_member(), pool_rig.hosted_member())
    svc = pool_rig.service(config)
    busy = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    idle = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    with connect(busy.agent, lease_token=busy.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(busy.agent)["state"] == "working")
        pool_rig.clock.advance(minutes=30)

        ended = svc.expire_due()

        assert [e.lease_id for e in ended] == [idle.lease_id]
        assert lease_state(pool_rig, busy.lease_id) == "active"
        assert pid_alive(pool_rig.status(busy.agent)["pi_pid"])
    svc.end_lease(busy.lease_id, "released")


def test_a_holder_verb_at_minute_9_keeps_a_served_lease_at_minute_15(pool_rig, monkeypatch):
    # the host's stamp, told by the fake clock in place of the host's own
    stamp = {"state": "idle", "last_activity": text(at(0))}
    monkeypatch.setattr(service.runner, "status", lambda name: dict(stamp))
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    pool_rig.clock.advance(minutes=9)
    stamp["last_activity"] = text(pool_rig.clock())
    pool_rig.clock.advance(minutes=6)
    assert svc.expire_due() == []
    assert lease_state(pool_rig, grant.lease_id) == "active"

    pool_rig.clock.advance(minutes=4)
    assert [e.lease_id for e in svc.expire_due()] == [grant.lease_id]


def test_a_member_whose_host_died_mid_turn_is_not_working_for_ever(pool_rig):
    real_time(pool_rig)
    pool_rig.scenario(mode="never_settle")
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    with connect(grant.agent, lease_token=grant.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(grant.agent)["state"] == "working")
    status = pool_rig.status(grant.agent)
    assert runner.status(grant.agent)["state"] == "working"

    for pid in (status["host_pid"], status["pi_pid"]):
        os.kill(pid, signal.SIGKILL)
    assert wait_until(lambda: not pid_alive(status["host_pid"]))

    # the file still says working, and no one is there to have written it
    assert pool_rig.status(grant.agent)["state"] == "working"
    assert runner.status(grant.agent) is None
    pool_rig.clock.advance(minutes=11)
    assert [e.lease_id for e in svc.expire_due()] == [grant.lease_id]


def test_the_runner_reads_no_status_for_a_member_that_never_ran(pool_rig):
    assert runner.status("local-1") is None


# -- a member that does not stop -----------------------------------------------


def two_pools(pool_rig):
    """Two pools of one member each: "rebasers" on local-1, "critics" on hosted-1."""
    local, hosted = pool_rig.local_member(), pool_rig.hosted_member()
    config = pool_rig.config(local)
    (rebasers,) = config.pools
    critics = dataclasses.replace(rebasers, name="critics", members=(hosted.name,))
    return dataclasses.replace(config, members=(local, hosted), pools=(rebasers, critics))


def asks(pool_rig, svc, pool: str, label: str) -> waiting.Waiting:
    ids = iter([f"request-{label}"])
    return waiting.Waiting(
        svc, pool, holder_label=label, cwd=pool_rig.work, wait=3600, mint_id=lambda: next(ids)
    )


def does_not_stop(monkeypatch, agent: str) -> list[str]:
    """From now on the stop of *agent* fails, as a host that gives no answer
    makes it fail; the agents a stop was asked of. The process is stopped all
    the same, so the test leaves none behind."""
    real_stop, asked = runner.stop, []

    def stop(name, cause, **tokens):
        asked.append(name)
        real_stop(name, cause, **tokens)
        if name == agent:
            raise runner.RunnerError(f"member '{name}' did not stop: no answer in 20 s")

    monkeypatch.setattr(service.runner, "stop", stop)
    return asked


def test_an_expired_member_that_does_not_stop_fails_no_request_for_another_pool(
    pool_rig, monkeypatch, caplog
):
    real_time(pool_rig)
    svc = pool_rig.service(two_pools(pool_rig))
    stuck = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)
    real_stop = runner.stop
    does_not_stop(monkeypatch, stuck.agent)

    with caplog.at_level(logging.WARNING, logger="specflo.pool.service"):
        granted = svc.grant("critics", holder_label="b", cwd=pool_rig.work)

    assert granted.agent == "hosted-1"
    # the lease of the member that did not stop has ended, and the log says why
    assert lease_state(pool_rig, stuck.lease_id) == "expired"
    (logged,) = [r.getMessage() for r in caplog.records if r.name == "specflo.pool.service"]
    assert stuck.lease_id in logged and "member 'local-1' did not stop" in logged
    # so its slot is free, and nothing is left for the next check to fail on
    again = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)
    assert again.agent == stuck.agent
    monkeypatch.setattr(service.runner, "stop", real_stop)
    for grant in (granted, again):
        svc.end_lease(grant.lease_id, "released")


def test_the_leases_due_after_one_whose_member_does_not_stop_are_ended_in_the_same_pass(
    pool_rig, monkeypatch, caplog
):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member(), pool_rig.hosted_member()))
    stuck = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    after = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    pi_after = pool_rig.status(after.agent)["pi_pid"]
    pool_rig.clock.advance(minutes=11)
    asked = does_not_stop(monkeypatch, stuck.agent)

    with caplog.at_level(logging.WARNING, logger="specflo.pool.service"):
        ended = svc.expire_due()

    assert asked == [stuck.agent, after.agent]
    assert [(e.lease_id, e.state) for e in ended] == [
        (stuck.lease_id, "expired"), (after.lease_id, "expired"),
    ]
    assert lease_state(pool_rig, after.lease_id) == "expired"
    assert wait_until(lambda: not pid_alive(pi_after))
    assert ["did not stop" in r.getMessage() for r in caplog.records] == [True]
    assert svc.expire_due() == []


def test_a_request_that_waits_keeps_its_row_and_its_place_when_an_expired_member_does_not_stop(
    pool_rig, monkeypatch
):
    real_time(pool_rig)
    svc = pool_rig.service(two_pools(pool_rig))
    stuck = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    held = svc.grant("critics", holder_label="b", cwd=pool_rig.work, idle_limit=3600)
    first, second = asks(pool_rig, svc, "critics", "c"), asks(pool_rig, svc, "critics", "d")
    assert first.attempt() is None
    assert second.attempt() is None
    pool_rig.clock.advance(minutes=11)
    does_not_stop(monkeypatch, stuck.agent)

    # the look finds the other pool's lease expired, and goes on as a look does
    assert first.attempt() is None

    assert lease_state(pool_rig, stuck.lease_id) == "expired"
    with pool_rig.store() as store:
        assert [row.id for row in store.list_waiting()] == ["request-c", "request-d"]
    assert (first.place(), second.place()) == (1, 2)
    svc.end_lease(held.lease_id, "released")
    served = first.attempt()
    assert served.agent == held.agent
    second.leave()
    svc.end_lease(served.lease_id, "released")


def test_a_read_goes_on_when_an_expired_member_does_not_stop(pool_rig, monkeypatch):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    stuck = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)
    does_not_stop(monkeypatch, stuck.agent)

    assert svc.read([]) == []

    assert lease_state(pool_rig, stuck.lease_id) == "expired"


def test_a_release_is_told_when_its_own_member_does_not_stop(pool_rig, monkeypatch):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    does_not_stop(monkeypatch, grant.agent)

    with pytest.raises(runner.RunnerError, match="member 'local-1' did not stop"):
        svc.end_lease(grant.lease_id, "released")

    assert lease_state(pool_rig, grant.lease_id) == "released"


def test_a_release_that_finds_its_own_lease_expired_is_told_when_the_member_does_not_stop(
    pool_rig, monkeypatch
):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member(), pool_rig.hosted_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    after = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)
    does_not_stop(monkeypatch, grant.agent)

    # the check on the way in is what ends it; the failure is this caller's own
    with pytest.raises(runner.RunnerError, match="member 'local-1' did not stop"):
        svc.end_lease(grant.lease_id, "released")

    assert lease_state(pool_rig, grant.lease_id) == "expired"
    # and it is raised when the pass is done, not in place of the rest of it
    assert lease_state(pool_rig, after.lease_id) == "expired"


# -- a lease whose agent's status cannot be read ---------------------------------

# A name the agent host refuses: a lease under it has no status to read at all.
NO_AGENT = "my member"


def unreadable_lease(pool_rig) -> Lease:
    """An active lease in the store under a name that names no agent, as a
    store from before such a member name was refused may hold one."""
    now = service._text(pool_rig.clock())
    lease = Lease(
        id="lease-unreadable", team_lease_id=None, holder_hash="0" * 64, holder_label="gone",
        member=NO_AGENT, pool="rebasers",
        resources=(Resource("pool", "rebasers"), Resource("member", NO_AGENT)),
        acquired=now, last_activity=now, idle_limit=TEN_MINUTES, state="active",
    )
    with pool_rig.store() as store:
        store.add_lease(lease)
    return lease


def test_a_status_that_cannot_be_read_is_no_status():
    def status(name):
        if name == NO_AGENT:
            raise runner.RunnerError(f"member '{name}' cannot name an agent")
        return host_status("idle", 9)

    unread = dataclasses.replace(lease_row(), id="lease-2", member=NO_AGENT)

    assert expiry.read([lease_row(), unread], status) == [
        (lease_row(), host_status("idle", 9)), (unread, None),
    ]


def test_a_lease_whose_status_cannot_be_read_fails_no_caller(pool_rig):
    real_time(pool_rig)
    config = pool_rig.config(pool_rig.local_member(), pool_rig.hosted_member())
    svc = pool_rig.service(config)
    unread = unreadable_lease(pool_rig)

    assert svc.expire_due() == []
    assert svc.read([unread]) == [(unread, None)]
    assert svc.swap(config, lambda old, new, store: ()) == ()
    granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert granted.agent == "local-1"
    assert svc.end_lease(granted.lease_id, "released").state == "released"

    # it stands until its limit, as any lease whose member has no status
    assert lease_state(pool_rig, unread.id) == "active"


def test_a_lease_whose_status_cannot_be_read_ends_at_its_idle_limit(pool_rig, caplog):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    unread = unreadable_lease(pool_rig)
    pool_rig.clock.advance(minutes=9)
    # it holds the pool's one slot until then
    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    pool_rig.clock.advance(minutes=2)
    with caplog.at_level(logging.WARNING, logger="specflo.pool.service"):
        granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert lease_state(pool_rig, unread.id) == "expired"
    with pool_rig.store() as store:
        (ended,) = store.list_transitions(lease_id=unread.id)
    assert (ended.kind, ended.cause) == ("expired", "expired")
    # there was no process to stop under that name, and the log says so
    (logged,) = [r.getMessage() for r in caplog.records if r.name == "specflo.pool.service"]
    assert unread.id in logged and "cannot name an agent" in logged
    svc.end_lease(granted.lease_id, "released")


# -- structure ----------------------------------------------------------------

# What would run by itself: a thread, a timer, a scheduled or background task.
BACKGROUND = {
    "Thread", "Timer", "start_new_thread", "create_task", "ensure_future",
    "call_later", "call_at", "run_in_executor", "submit", "add_task", "scheduler",
}
LEASE_MODULES = (
    SRC / "pool" / "service.py",
    SRC / "pool" / "expiry.py",
    SRC / "pool" / "waiting.py",
)


def called_names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    return names


def test_nothing_in_the_lease_modules_runs_by_itself():
    for path in LEASE_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not called_names(tree) & BACKGROUND, path.name
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imported.add((node.module or "").split(".")[0])
        assert not imported & {"asyncio", "sched", "concurrent", "multiprocessing", "signal"}
        # of threading, the lock that makes callers take turns and no more
        used = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name) and node.value.id == "threading"
        }
        assert used <= {"RLock"}, path.name


def test_the_rule_reads_no_clock_and_no_file():
    tree = ast.parse((SRC / "pool" / "expiry.py").read_text(encoding="utf-8"))
    assert not called_names(tree) & {"now", "utcnow", "time", "monotonic", "open", "read_text"}
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not [m for m in modules if "agent" in m or "runner" in m]


def expiring_functions() -> set[tuple[str, str]]:
    """The (file, function) pairs under the source tree that end a lease as expired."""
    found = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                values = [*node.args, *(keyword.value for keyword in node.keywords)]
                if node.func.attr == "end_lease" and any(
                    isinstance(v, ast.Constant) and v.value == "expired" for v in values
                ):
                    found.add((str(path.relative_to(SRC)), function.name))
    return found


def test_expired_leases_are_ended_by_the_check_and_through_end_lease():
    assert expiring_functions() == {("pool/service.py", "expire_due")}


def test_every_entry_point_of_the_service_checks_expiry_first():
    tree = ast.parse((SRC / "pool" / "service.py").read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PoolService"
    )
    entry_points = [
        node for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and not node.name.startswith("_") and node.name != "expire_due"
    ]
    assert {"grant", "end_lease"} <= {node.name for node in entry_points}
    for function in entry_points:
        calls = [
            node.func for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"
        ]
        assert calls, function.name
        first = min(calls, key=lambda func: (func.lineno, func.col_offset))
        assert first.attr == "expire_due", function.name


def test_the_cli_has_no_renew_verb():
    def verbs(command: click.Command, path: tuple[str, ...] = ()):
        for name, sub in getattr(command, "commands", {}).items():
            yield (*path, name)
            yield from verbs(sub, (*path, name))

    every = list(verbs(get_command(app)))
    assert ("agent", "prompt") in every  # the walk reaches the verbs
    assert not [path for path in every if "renew" in path[-1]]
