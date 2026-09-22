"""The pool service: a lease granted on a free member, and ended through one function.

A grant starts a fresh pi for the member through the runner, writes the lease
row and its 'granted' transition, and hands back the lease id, the agent's
name and the lease token. However a lease ends - released, expired or
preempted - it ends in ``end_lease``: a running turn is aborted, the process
stopped, the slot freed and the transition recorded with its cause. Ending a
lease that has ended already changes nothing and reports how it ended.

The members are real processes: the stub pi under an agent host, placed by a
fake herdr, as in the runner's tests.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo.agent import lease as agent_lease
from specflo.agent.cli import agent_app
from specflo.agent.client import AgentClient, connect
from specflo.agent.statefiles import AgentPaths
from specflo.daemon import STATE_STORE_FILENAME
from specflo.errors import SpecfloError
from specflo.pool import launch, service
from specflo.pool.service import Ended, Grant

from .test_reload import STATUS_PATH, Served, write_workers
from .test_reload import console as roster_console
from .test_reload import member as roster_member
from .test_reload import pool as roster_pool
from .test_runner import pid_alive, wait_until

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"
MARKER = "heron"


def prompt(grant: Grant, message: str):
    """One turn on the leased member, as its holder."""
    return CliRunner().invoke(
        agent_app, ["prompt", grant.agent, message, "--lease-token", grant.token]
    )


def captured_types(capture: Path) -> list[str]:
    """The type of every frame the stub pi received, in order."""
    if not capture.exists():
        return []
    return [json.loads(line)["type"] for line in capture.read_text().splitlines() if line]


# -- grant ------------------------------------------------------------------


def test_grant_starts_the_member_and_returns_the_lease_the_agent_and_the_token(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    grant = svc.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)

    assert grant == Grant(lease_id="lease-1", agent="local-1", token="token-1")
    status = pool_rig.status(grant.agent)
    assert pid_alive(status["pi_pid"])
    assert pool_rig.recorded()["cwd"] == str(pool_rig.work)
    assert pool_rig.pane_names() == ["local-1"]
    # the wall is up for the token the grant returned, and for no other
    with connect(grant.agent) as client:
        assert client.request({"type": "status", "lease_token": grant.token})["success"]
        assert not client.request({"type": "status", "lease_token": "token-2"})["success"]


def test_grant_writes_the_lease_row_and_a_granted_transition(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    grant = svc.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)

    now = "2026-03-01T12:00:00.000+00:00"
    with pool_rig.store() as store:
        row = store.get_lease(grant.lease_id)
        transitions = store.list_transitions(lease_id=grant.lease_id)
    assert row.state == "active"
    assert (row.pool, row.member, row.holder_label) == ("rebasers", "local-1", "orchestrator-a")
    assert row.team_lease_id is None
    assert (row.acquired, row.last_activity) == (now, now)
    assert row.idle_limit == 600  # the pool's default
    assert {(r.kind, r.name) for r in row.resources} == {
        ("pool", "rebasers"), ("member", "local-1"),
    }
    # the hash of the token, never the token
    assert row.holder_hash == hashlib.sha256(grant.token.encode()).hexdigest()
    assert row.holder_hash == service.hash_token(grant.token)
    assert grant.token not in repr(row)
    assert [(t.kind, t.time) for t in transitions] == [("granted", now)]


def test_a_request_may_ask_for_an_idle_limit_up_to_the_pools_maximum(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    with pytest.raises(service.IdleLimitError, match="4h"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, idle_limit=5 * 3600)
    assert pool_rig.pane_names() == []

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work, idle_limit=4 * 3600)
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).idle_limit == 4 * 3600


def test_an_undeclared_pool_is_refused_by_name(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    with pytest.raises(service.UnknownPool, match="reviewers") as raised:
        svc.grant("reviewers", holder_label="a", cwd=pool_rig.work)

    assert isinstance(raised.value, SpecfloError)
    assert pool_rig.pane_names() == []


def test_a_pool_with_no_free_member_grants_nothing(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember, match="rebasers"):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert [row.id for row in store.list_leases()] == ["lease-1"]


def test_the_first_free_member_is_taken_and_the_pool_holds_no_more_than_its_size(pool_rig):
    config = pool_rig.config(pool_rig.local_member(), pool_rig.hosted_member(), size=2)
    svc = pool_rig.service(config)

    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    assert (first.agent, second.agent) == ("local-1", "hosted-1")
    assert first.token != second.token
    assert sorted(pool_rig.pane_names()) == ["hosted-1", "local-1"]

    svc.end_lease(first.lease_id, "released")
    svc.end_lease(second.lease_id, "released")
    small = pool_rig.service(
        pool_rig.config(pool_rig.local_member(), pool_rig.hosted_member(), size=1)
    )
    small.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    with pytest.raises(service.NoFreeMember):
        small.grant("rebasers", holder_label="b", cwd=pool_rig.work)


def test_a_member_that_cannot_be_started_leaves_no_active_lease(pool_rig, monkeypatch):
    monkeypatch.delenv("TEAM_A_KEY")
    svc = pool_rig.service(pool_rig.config(pool_rig.hosted_member()))

    with pytest.raises(launch.LaunchError, match="TEAM_A_KEY"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert pool_rig.pane_names() == []
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
        ended = store.list_transitions()
    assert [t.kind for t in ended] == ["released"]
    assert "TEAM_A_KEY" in ended[0].cause


def test_a_member_whose_name_names_no_agent_leaves_no_active_lease(pool_rig):
    # a roster from before such a name was refused: the agent host takes no
    # agent of that name, so the member never starts and has no status to read
    unnamed = replace(pool_rig.local_member(), name="my member")
    svc = pool_rig.service(pool_rig.config(unnamed, pool_rig.local_member()))

    with pytest.raises(service.runner.RunnerError, match="cannot name an agent"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
        (ended,) = store.list_transitions()
    assert (ended.kind, "cannot name an agent" in ended.cause) == ("released", True)
    # so nothing is left of it when the roster is put right
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    granted = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert granted.agent == "local-1"
    assert svc.end_lease(granted.lease_id, "released").state == "released"


def test_an_agent_the_pool_did_not_start_is_left_alone_by_a_refused_grant(pool_rig):
    # a developer's own agent, started by hand under the name of a declared member
    before = pool_rig.start_by_hand("local-1")
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    with pytest.raises(service.runner.RunnerError, match="agent 'local-1' already runs"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
        (ended,) = store.list_transitions()
    assert ended.kind == "released"
    assert "agent 'local-1' already runs" in ended.cause
    # the host and its pi are the ones the developer started, and nothing
    # in their state directory says a lease ended there
    time.sleep(0.5)  # a stop that was sent would have landed by now
    after = pool_rig.status("local-1")
    assert (after["host_pid"], after["pi_pid"]) == (before["host_pid"], before["pi_pid"])
    assert pid_alive(before["host_pid"]) and pid_alive(before["pi_pid"])
    with connect("local-1") as client:
        assert client.status()["status"]["state"] == "idle"
    state_dir = AgentPaths.resolve("local-1").root
    assert agent_lease.read_ended(state_dir) is None
    assert not (state_dir / agent_lease.ENDED_DIR).exists()


def test_a_member_whose_host_came_up_and_did_not_take_the_lease_is_stopped(
    pool_rig, monkeypatch
):
    real_bind, refusals = AgentClient.lease_bind, ["lease_bind failed: refused for the test"]

    def refused_once(self, *args, **kwargs):
        if refusals:
            raise RuntimeError(refusals.pop())
        real_bind(self, *args, **kwargs)

    monkeypatch.setattr(AgentClient, "lease_bind", refused_once)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    with pytest.raises(service.runner.RunnerError, match="did not take the lease"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    # the pool started this one, so it goes with the grant that failed. The
    # member runs inside a sandbox, where the pid it knows is its own in a
    # namespace of its own; the pane is what says the process is gone, since
    # the fake herdr lists one for as long as what runs in it lives.
    assert pool_rig.recorded()["pid"]
    assert wait_until(lambda: pool_rig.pane_names() == [])
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
        (ended,) = store.list_transitions()
    assert (ended.kind, "did not take the lease" in ended.cause) == ("released", True)
    # the slot and the name are free: the next request starts the member afresh
    pool_rig.forget_records()
    assert svc.grant("rebasers", holder_label="b", cwd=pool_rig.work).agent == "local-1"
    # a record written after the last one was forgotten is a process that ran
    assert pool_rig.recorded()["pid"]


# -- one lease after another on one member ----------------------------------


def test_the_next_lease_on_a_member_is_a_fresh_pi_that_recalls_nothing(pool_rig):
    pool_rig.scenario(reply="noted", recall=True)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    first_pid = pool_rig.status(first.agent)["pi_pid"]
    assert prompt(first, f"remember the word {MARKER}").exit_code == 0
    assert MARKER in prompt(first, "what was the word?").stdout  # it recalls within a lease
    svc.end_lease(first.lease_id, "released")

    second = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    assert second.agent == first.agent
    assert second.lease_id != first.lease_id and second.token != first.token
    assert pool_rig.status(second.agent)["pi_pid"] != first_pid
    asked = prompt(second, "what was the word?")
    assert asked.exit_code == 0, asked.stderr
    assert MARKER not in asked.stdout
    # the first holder's token opens nothing on the second lease
    refused = CliRunner().invoke(
        agent_app, ["prompt", first.agent, "again", "--lease-token", first.token]
    )
    assert refused.exit_code != 0


# -- the one way a lease ends -----------------------------------------------


def test_end_lease_stops_the_process_frees_the_slot_and_records_the_cause(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    status = pool_rig.status(grant.agent)
    pool_rig.clock.advance(minutes=3)

    ended = svc.end_lease(grant.lease_id, "released", cause="released by holder")

    then = "2026-03-01T12:03:00.000+00:00"
    assert ended == Ended(
        lease_id="lease-1", state="released", cause="released by holder", time=then
    )
    assert wait_until(lambda: not pid_alive(status["pi_pid"]))
    assert wait_until(lambda: not pid_alive(status["host_pid"]))
    assert pool_rig.pane_names() == []
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).state == "released"
        last = store.list_transitions(lease_id=grant.lease_id)[-1]
    assert (last.kind, last.time, last.cause) == ("released", then, "released by holder")
    # the former holder's next verb names the cause
    again = prompt(grant, "still there?")
    assert again.exit_code != 0
    assert "lease released" in again.stderr
    # and the slot is free to the next request
    assert svc.grant("rebasers", holder_label="b", cwd=pool_rig.work).agent == grant.agent


@pytest.mark.parametrize(
    ("kind", "request_id", "cause", "holder_reads"),
    [
        ("expired", None, "expired", "lease expired"),
        ("preempted", "req-7", "preempted by req-7", "lease preempted by req-7"),
    ],
)
def test_expiry_and_preemption_end_a_lease_the_same_way(
    pool_rig, kind, request_id, cause, holder_reads
):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    ended = svc.end_lease(grant.lease_id, kind, request_id=request_id)

    assert (ended.state, ended.cause) == (kind, cause)
    record = agent_lease.read_ended(AgentPaths.resolve(grant.agent).root)
    assert (record["cause"], record["request_id"]) == (kind, request_id)
    assert holder_reads in prompt(grant, "still there?").stderr
    assert pool_rig.pane_names() == []


def test_end_lease_aborts_a_running_turn_before_the_process_goes(pool_rig):
    pool_rig.scenario(mode="never_settle", capture=str(pool_rig.capture))
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    status = pool_rig.status(grant.agent)
    with connect(grant.agent, lease_token=grant.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(grant.agent)["state"] == "working")

        svc.end_lease(grant.lease_id, "expired")

    # the turn was aborted first: pi was sent the abort after the prompt
    assert captured_types(pool_rig.capture) == ["prompt", "abort"]
    events = AgentPaths.resolve(grant.agent).events.read_text(encoding="utf-8")
    types = [json.loads(line)["type"] for line in events.splitlines() if line]
    assert types.index("host_abort_sent") < types.index("process_exit")
    assert "host_escalated_kill" not in types
    assert wait_until(lambda: not pid_alive(status["pi_pid"]))


def test_ending_an_ended_lease_changes_nothing_and_reports_how_it_ended(pool_rig, monkeypatch):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    first = svc.end_lease(grant.lease_id, "released", cause="released by holder")
    # the member serves its next lease, which the late caller must not touch
    successor = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    stops = []
    monkeypatch.setattr(service.runner, "stop", lambda *a, **k: stops.append(a))
    pool_rig.clock.advance(minutes=20)

    again = svc.end_lease(grant.lease_id, "expired")

    assert again == first
    assert again.state == "released"
    assert stops == []
    with pool_rig.store() as store:
        assert [t.kind for t in store.list_transitions(lease_id=grant.lease_id)] == [
            "granted", "released",
        ]
        assert store.get_lease(successor.lease_id).state == "active"
    assert pid_alive(pool_rig.status(successor.agent)["pi_pid"])
    record = agent_lease.read_ended(AgentPaths.resolve(grant.agent).root)
    assert record is None  # cleared by the successor's start, and not written again


def test_of_two_callers_ending_one_lease_one_stops_the_member(pool_rig, monkeypatch):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    real_stop, stops = service.runner.stop, []

    def counted(name, cause, **kwargs):
        stops.append(cause)
        real_stop(name, cause, **kwargs)

    monkeypatch.setattr(service.runner, "stop", counted)
    results = {}

    def end(kind):
        results[kind] = svc.end_lease(grant.lease_id, kind)

    callers = [threading.Thread(target=end, args=(kind,)) for kind in ("released", "expired")]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(timeout=60)

    assert len(stops) == 1
    assert results["released"] == results["expired"]
    assert results["released"].state == stops[0]
    with pool_rig.store() as store:
        kinds = [t.kind for t in store.list_transitions(lease_id=grant.lease_id)]
    assert kinds == ["granted", stops[0]]


def test_a_lease_that_was_never_granted_cannot_be_ended(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    with pytest.raises(service.UnknownLease, match="lease-9"):
        svc.end_lease("lease-9", "released")


def test_a_lease_ends_in_one_of_the_three_ways_only(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(ValueError, match="granted"):
        svc.end_lease(grant.lease_id, "granted")
    with pytest.raises(ValueError, match="request"):
        svc.end_lease(grant.lease_id, "preempted")

    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).state == "active"


# -- a lease ends as it began -------------------------------------------------


def test_a_grant_writes_that_the_pool_started_the_members_process(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).pool_started is True
    svc.end_lease(grant.lease_id, "released")


def test_a_lease_row_from_before_that_was_written_ends_by_the_roster_in_force(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pi_pid = pool_rig.status(grant.agent)["pi_pid"]
    # the row as a store from before the column holds it
    rows = sqlite3.connect(pool_rig.root / STATE_STORE_FILENAME)
    rows.execute("UPDATE pool_leases SET pool_started = NULL")
    rows.commit()
    rows.close()
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).pool_started is None

    # the roster declares the member as one the pool starts, so it is stopped
    svc.end_lease(grant.lease_id, "released")

    assert wait_until(lambda: not pid_alive(pi_pid))
    assert pool_rig.pane_names() == []


def without_the_member(rig) -> None:
    """The roster with "w-1" declared no more."""
    write_workers(
        rig, members=[roster_member(rig, "w-2", model="model-c")],
        pools=[roster_pool("workers", "w-2")],
    )


def with_the_member_as_a_console(rig) -> None:
    """The roster with "w-1" declared as a developer's console."""
    write_workers(
        rig, members=[roster_console("w-1"), roster_member(rig, "w-2", model="model-c")]
    )


@pytest.mark.parametrize("ending", ["released", "expired"])
@pytest.mark.parametrize("later", [without_the_member, with_the_member_as_a_console])
def test_a_daemon_started_again_on_another_roster_stops_the_process_the_pool_started(
    pool_rig, later, ending
):
    write_workers(pool_rig)
    lease = Served(pool_rig).granted(pool="workers")
    assert lease["agent"] == "w-1"
    started = pool_rig.status("w-1")
    # the admin edits the roster and starts the daemon again: a reload would
    # refuse the edit while the lease is out, and a start refuses nothing
    later(pool_rig)
    again = Served(pool_rig)
    assert again.state.pool is not None and not list(again.state.pool_errors)

    if ending == "released":
        assert again.release(lease["lease_id"], lease["token"]).status_code == 200
    else:
        again.state.pool.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=11)
        assert again.developer.get(STATUS_PATH).status_code == 200

    with pool_rig.store() as store:
        assert store.get_lease(lease["lease_id"]).state == ending
        assert store.list_leases(state="active") == []
    # the host and the pi of that lease are the pool's own, and they go with it
    assert wait_until(lambda: not pid_alive(started["pi_pid"]))
    assert wait_until(lambda: not pid_alive(started["host_pid"]))
    assert pool_rig.pane_names() == []
    # and the slot it held in the pool is free
    after = again.granted(pool="workers")
    assert after["agent"] == "w-2"
    assert again.release(after["lease_id"], after["token"]).status_code == 200


# -- structure --------------------------------------------------------------


def callers_of(attribute: str, owner: str | None = None) -> set[tuple[str, str]]:
    """The (file, function) pairs under the source tree that call ``.attribute(``,
    on the name *owner* when one is given."""
    found = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != attribute:
                    continue
                if owner is None or (
                    isinstance(node.func.value, ast.Name) and node.func.value.id == owner
                ):
                    found.add((str(path.relative_to(SRC)), function.name))
    return found


def test_one_function_ends_a_lease():
    # a member's process is stopped from one place, and a lease's state moves
    # in the grant and in that same place
    assert callers_of("stop", owner="runner") == {("pool/service.py", "end_lease")}
    assert callers_of("record_transition") == {
        ("pool/service.py", "grant"), ("pool/service.py", "end_lease"),
    }
