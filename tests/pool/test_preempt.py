"""A waiting request may take an idle lease: when its pool says so, and never in a turn.

A pool may declare ``preempt_after``. A lease of that pool whose own idle time
is longer than that, and whose member runs no turn, may be ended by a request
that waits and that the lease stands in the way of. It ends as every lease
ends, with a 'preempted' transition that names the request that took it, and
its former holder's next verb is told that id. A lease of a pool with no
``preempt_after`` is never taken: the request waits for a release or an
expiry. A team is one unit: it is taken whole, and only when every one of its
member leases may be taken by its own pool's value.

Only a request that waits takes a lease, at a look after the one that wrote
its row, so there is always an id to name. A lease is taken only when the
request fits with it gone, the fewest are taken, the longest idle first, and
a request leaves alone what would serve a request that arrived before it.
Nothing ranks one request above another: there is no priority anywhere.

The rule is a function of the lease rows, the status records, the pools and
the time, so a fake clock drives it. Where the service is asked, the hosts'
stamps are told by the fake clock too, in place of the hosts' own; one test
reads a real host with a turn that never settles.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from specflo.agent import lease as agent_lease
from specflo.agent.client import connect
from specflo.agent.statefiles import AgentPaths
from specflo.daemon.poolstore import Lease, Resource, WaitingRequest
from specflo.pool import ledger, preempt, service, teamlease, waiting
from specflo.pool import config as pool_config
from specflo.pool.config import Pool

from .test_expiry import at, called_names, prompt, text
from .test_runner import pid_alive, wait_until
from .test_team_lease import active, asks_team, team_config
from .test_waiting import asks, two_pools, waiting_rows

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"
FIVE_MINUTES = 300
EIGHT_MINUTES = 480
HOUR = 3600


def pool(name: str, preempt_after: int | None = FIVE_MINUTES) -> Pool:
    return Pool(
        name=name, definition="rebaser", members=("local-1",), size=1,
        idle_default=HOUR, idle_max=4 * HOUR, preempt_after=preempt_after,
    )


def row(lease_id: str, pool_name: str = "workers", team: str | None = None) -> Lease:
    """An active lease of an hour on that pool, granted and last touched at minute 0."""
    return Lease(
        id=lease_id, team_lease_id=team, holder_hash="0" * 64, holder_label="a",
        member=lease_id, pool=pool_name,
        resources=(Resource("pool", pool_name), Resource("member", lease_id)),
        acquired=text(at(0)), last_activity=text(at(0)), idle_limit=HOUR, state="active",
    )


def stamped(state: str, minute: float) -> dict:
    return {"state": state, "last_activity": text(at(minute))}


def taken(units) -> list[list[str]]:
    return [[lease.id for lease in unit.leases] for unit in units]


# -- the rule: which leases may be taken ---------------------------------------


def test_a_lease_idle_longer_than_its_pools_preempt_after_may_be_taken():
    lease, workers = row("a"), pool("workers")

    assert not preempt.eligible(lease, stamped("idle", 0), workers, at(4))
    assert preempt.eligible(lease, stamped("idle", 0), workers, at(6))
    # the holder's verb at minute 3 starts the five minutes again
    assert not preempt.eligible(lease, stamped("idle", 3), workers, at(6))
    assert preempt.eligible(lease, stamped("idle", 3), workers, at(9))
    # a member with no status to go by is judged by its row
    assert preempt.eligible(lease, None, workers, at(6))


def test_a_lease_idle_for_exactly_preempt_after_is_not_yet_taken():
    lease, workers = row("a"), pool("workers")

    assert not preempt.eligible(lease, None, workers, at(5))
    assert preempt.eligible(lease, None, workers, at(5) + timedelta(milliseconds=1))


def test_a_member_in_a_turn_is_never_taken_however_old_its_stamp():
    lease, workers = row("a"), pool("workers")

    assert preempt.idle_time(lease, stamped("working", 0), at(50)) == timedelta(0)
    assert not preempt.eligible(lease, stamped("working", 0), workers, at(50))


def test_the_working_state_is_checked_by_itself_and_not_only_through_the_idle_time(monkeypatch):
    # a working member reads as active now, which alone would keep it; the rule
    # asks the state too, so it holds whatever the idle time is worked out as
    monkeypatch.setattr(preempt.expiry, "last_activity", lambda lease, status, now: at(0))

    assert preempt.eligible(row("a"), stamped("idle", 0), pool("workers"), at(50))
    assert not preempt.eligible(row("a"), stamped("working", 0), pool("workers"), at(50))


def test_a_lease_of_a_pool_with_no_preempt_after_is_never_taken():
    assert not preempt.eligible(row("a"), None, pool("workers", None), at(59))
    assert preempt.units([(row("a"), None)], [pool("workers", None)], at(59)) == []
    # nor one whose pool is no longer declared
    assert preempt.units([(row("a", "gone"), None)], [pool("workers")], at(59)) == []


def test_leases_of_no_team_are_units_of_one_longest_idle_first():
    read = [
        (row("a"), stamped("idle", 3)),
        (row("b"), stamped("idle", 1)),
        (row("c"), stamped("idle", 8)),
        (row("d"), stamped("working", 0)),
    ]

    units = preempt.units(read, [pool("workers")], at(10))

    assert taken(units) == [["b"], ["a"]]
    assert [unit.team_lease_id for unit in units] == [None, None]
    assert [unit.idle for unit in units] == [timedelta(minutes=9), timedelta(minutes=7)]


def test_a_team_is_one_unit_and_only_when_every_member_may_be_taken():
    pools = [pool("workers"), pool("critics", EIGHT_MINUTES)]
    worker, critic = row("worker", "workers", "team-1"), row("critic", "critics", "team-1")
    idle = [(worker, stamped("idle", 0)), (critic, stamped("idle", 0))]

    # each member by its own pool's value: the critic's eight minutes hold the team
    assert preempt.units(idle, pools, at(6)) == []
    (unit,) = preempt.units(idle, pools, at(9))
    assert (unit.team_lease_id, taken([unit])) == ("team-1", [["worker", "critic"]])
    assert unit.idle == timedelta(minutes=9)


def test_a_team_member_is_judged_by_its_own_idle_time_and_not_the_teams():
    pools = [pool("workers"), pool("critics")]
    worker, critic = row("worker", "workers", "team-1"), row("critic", "critics", "team-1")

    # the worker was prompted at minute 7: the team is not idle as a whole
    read = [(worker, stamped("idle", 7)), (critic, stamped("idle", 0))]
    assert preempt.units(read, pools, at(9)) == []
    # and the unit is as idle as its member that was active last, which ends
    # last: the team is kept by it, so no member expires while the team is ended
    (unit,) = preempt.units(read, pools, at(13))
    assert unit.idle == timedelta(minutes=6)
    assert taken([unit]) == [["critic", "worker"]]


def test_a_team_with_a_member_in_a_turn_is_never_taken():
    pools = [pool("workers"), pool("critics")]
    read = [
        (row("worker", "workers", "team-1"), stamped("working", 0)),
        (row("critic", "critics", "team-1"), stamped("idle", 0)),
    ]

    assert preempt.units(read, pools, at(50)) == []


def test_a_team_with_one_pool_that_declares_no_preempt_after_is_never_taken():
    pools = [pool("workers"), pool("critics", None)]
    read = [
        (row("worker", "workers", "team-1"), None),
        (row("critic", "critics", "team-1"), None),
        (row("alone", "workers"), None),
    ]

    assert taken(preempt.units(read, pools, at(50))) == [["alone"]]


def test_the_fewest_units_are_chosen_and_the_longest_idle_among_equals():
    read = [(row(name), stamped("idle", minute)) for name, minute in
            [("a", 3), ("b", 1), ("c", 2)]]
    units = preempt.units(read, [pool("workers")], at(10))
    assert taken(units) == [["b"], ["c"], ["a"]]

    def fits_without(*sets: set[str]):
        return lambda gone: any(wanted <= set(gone) for wanted in sets)

    assert taken(preempt.fewest(units, fits_without({"b"}, {"a"}))) == [["b"]]
    assert taken(preempt.fewest(units, fits_without({"a"}))) == [["a"]]
    # one unit is not enough: the two longest idle that do
    assert taken(preempt.fewest(units, fits_without({"c", "a"}, {"b", "a"}))) == [["b"], ["a"]]
    # nothing makes room: nothing is chosen
    assert preempt.fewest(units, lambda gone: False) == ()
    assert preempt.fewest([], lambda gone: True) == ()


# -- the service: a waiting request takes what stands in its way ---------------


@pytest.fixture
def stamps(monkeypatch):
    """The hosts' stamps by agent name, told by the test in place of the hosts."""
    told: dict[str, dict] = {}
    monkeypatch.setattr(service.runner, "status", lambda name: told.get(name))
    return told


def preemptible(pool_rig, *members, **fields):
    """The pool "rebasers" of *members*, local-1 alone without any, whose
    leases may be taken after five idle minutes."""
    members = members or (pool_rig.local_member(),)
    return pool_rig.config(*members, **{"preempt_after": FIVE_MINUTES, **fields})


def lease_state(pool_rig, lease_id: str) -> str:
    with pool_rig.store() as store:
        return store.get_lease(lease_id).state


def transitions(pool_rig, lease_id: str) -> list[tuple[str, str]]:
    with pool_rig.store() as store:
        return [(t.kind, t.cause) for t in store.list_transitions(lease_id=lease_id)]


def one_of_two(pool_rig):
    """The pool "rebasers", one lease at a time on local-1 or hosted-1, whose
    leases may be taken after five idle minutes; and "others" on local-1."""
    config = preemptible(pool_rig, pool_rig.local_member(), pool_rig.hosted_member(), size=1)
    (rebasers,) = config.pools
    others = dataclasses.replace(
        rebasers, name="others", members=("local-1",), preempt_after=None
    )
    return dataclasses.replace(config, pools=(rebasers, others))


def test_a_conflicting_request_at_idle_minute_6_is_granted_and_the_holder_is_told_who_took_it(
    pool_rig, stamps
):
    svc = pool_rig.service(one_of_two(pool_rig))
    # the old lease is on the pool's second member: a member that starts for a
    # new lease forgets how its last one ended, and this one is not started
    other = svc.grant("others", holder_label="x", cwd=pool_rig.work)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.end_lease(other.lease_id, "released")
    assert old.agent == "hosted-1"
    old_pid = pool_rig.status(old.agent)["pi_pid"]
    pool_rig.clock.advance(minutes=6)
    second = asks(pool_rig, svc, "rebasers", "b")

    # the first look writes the row: there is no id to name before it
    assert second.attempt() is None
    assert lease_state(pool_rig, old.lease_id) == "active"
    granted = second.attempt()

    assert granted is not None and granted.agent == "local-1"
    assert lease_state(pool_rig, old.lease_id) == "preempted"
    assert transitions(pool_rig, old.lease_id) == [
        ("granted", "requested"), ("preempted", "preempted by request-b"),
    ]
    record = agent_lease.read_ended(AgentPaths.resolve(old.agent).root)
    assert (record["cause"], record["request_id"]) == ("preempted", "request-b")
    assert wait_until(lambda: not pid_alive(old_pid))
    assert waiting_rows(pool_rig) == []
    assert [lease.id for lease in active(pool_rig)] == [granted.lease_id]
    # the former holder's next prompt fails and names the request
    refused = prompt(old, "still there?")
    assert refused.exit_code != 0
    assert "lease preempted by request-b" in refused.stderr
    svc.end_lease(granted.lease_id, "released")


def test_the_request_is_granted_the_very_member_it_took(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    old_pid = pool_rig.status(old.agent)["pi_pid"]
    pool_rig.clock.advance(minutes=6)
    second = asks(pool_rig, svc, "rebasers", "b")
    assert second.attempt() is None

    granted = second.attempt()

    assert granted is not None and granted.agent == old.agent
    assert pool_rig.status(granted.agent)["pi_pid"] != old_pid
    assert transitions(pool_rig, old.lease_id) == [
        ("granted", "requested"), ("preempted", "preempted by request-b"),
    ]
    assert transitions(pool_rig, granted.lease_id) == [("granted", "requested")]
    # the former holder's token opens nothing on the new lease
    assert prompt(old, "still there?").exit_code != 0
    assert prompt(granted, "hello").exit_code == 0


def test_at_idle_minute_4_the_request_waits(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=4)
    second = asks(pool_rig, svc, "rebasers", "b")

    assert second.attempt() is None
    assert second.attempt() is None

    assert lease_state(pool_rig, old.lease_id) == "active"
    assert [row.id for row in waiting_rows(pool_rig)] == ["request-b"]
    # and at minute 6 the same request takes it
    pool_rig.clock.advance(minutes=2)
    assert second.attempt() is not None
    assert lease_state(pool_rig, old.lease_id) == "preempted"


def test_a_holders_verb_starts_the_idle_time_again(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b")
    assert second.attempt() is None
    pool_rig.clock.advance(minutes=4)
    stamps[old.agent] = {"state": "idle", "last_activity": text(pool_rig.clock())}
    pool_rig.clock.advance(minutes=4)

    assert second.attempt() is None

    assert lease_state(pool_rig, old.lease_id) == "active"


def test_with_the_member_in_a_turn_the_request_waits(pool_rig):
    # the host stamps with its own clock, so the fake one starts at the real time
    pool_rig.clock.now = datetime.now(timezone.utc)
    pool_rig.scenario(mode="never_settle")
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b")
    with connect(old.agent, lease_token=old.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(old.agent)["state"] == "working")
        pool_rig.clock.advance(minutes=30)

        assert second.attempt() is None
        assert second.attempt() is None

        assert lease_state(pool_rig, old.lease_id) == "active"
        assert pid_alive(pool_rig.status(old.agent)["pi_pid"])
    svc.end_lease(old.lease_id, "released")


def test_a_stamp_that_says_working_keeps_the_lease(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    stamps[old.agent] = {"state": "working", "last_activity": text(pool_rig.clock())}
    second = asks(pool_rig, svc, "rebasers", "b")
    pool_rig.clock.advance(minutes=30)

    assert second.attempt() is None
    assert second.attempt() is None

    assert lease_state(pool_rig, old.lease_id) == "active"


def test_against_a_pool_with_no_preempt_after_the_request_waits_until_expiry(pool_rig, stamps):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b")

    for _ in range(3):  # looks at minutes 3, 6 and 9
        pool_rig.clock.advance(minutes=3)
        assert second.attempt() is None
        assert lease_state(pool_rig, old.lease_id) == "active"
    pool_rig.clock.advance(minutes=2)
    granted = second.attempt()

    assert granted is not None
    assert transitions(pool_rig, old.lease_id) == [("granted", "requested"), ("expired", "expired")]


def test_against_a_pool_with_no_preempt_after_the_request_waits_until_release(pool_rig, stamps):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = asks(pool_rig, svc, "rebasers", "b")
    pool_rig.clock.advance(minutes=9)
    assert second.attempt() is None
    assert second.attempt() is None
    assert lease_state(pool_rig, old.lease_id) == "active"

    svc.end_lease(old.lease_id, "released")

    assert second.attempt() is not None
    assert transitions(pool_rig, old.lease_id)[-1] == ("released", "released")


def test_a_request_that_does_not_wait_takes_nothing(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)

    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    with pytest.raises(service.NoFreeMember):
        asks(pool_rig, svc, "rebasers", "c", wait=0).attempt()
    # nor does an id that is no waiting row's
    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="d", cwd=pool_rig.work, waiting_id="request-d")

    assert lease_state(pool_rig, old.lease_id) == "active"


def test_an_idle_lease_that_is_not_in_the_way_is_left_alone(pool_rig, stamps):
    config = two_pools(pool_rig)
    rebasers, critics = config.pools
    config = dataclasses.replace(config, pools=(
        dataclasses.replace(rebasers, preempt_after=FIVE_MINUTES), critics,
    ))
    svc = pool_rig.service(config)
    idle = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.grant("critics", holder_label="b", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)
    third = asks(pool_rig, svc, "critics", "c")

    assert third.attempt() is None
    assert third.attempt() is None

    # the critics' lease is never taken, and taking the rebasers' makes no room
    assert lease_state(pool_rig, idle.lease_id) == "active"
    assert len(active(pool_rig)) == 2


def test_one_lease_is_taken_when_one_makes_room_and_it_is_the_longest_idle(pool_rig, stamps):
    members = (pool_rig.local_member(), pool_rig.hosted_member())
    svc = pool_rig.service(preemptible(pool_rig, *members))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=2)
    stamps[first.agent] = {"state": "idle", "last_activity": text(pool_rig.clock())}
    pool_rig.clock.advance(minutes=7)
    third = asks(pool_rig, svc, "rebasers", "c")
    assert third.attempt() is None

    granted = third.attempt()

    # both were idle past five minutes: the second for nine, the first for seven
    assert granted is not None and granted.agent == second.agent
    assert lease_state(pool_rig, second.lease_id) == "preempted"
    assert lease_state(pool_rig, first.lease_id) == "active"


def test_a_request_leaves_an_idle_lease_to_the_request_that_arrived_before_it(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    earlier, later = asks(pool_rig, svc, "rebasers", "b"), asks(pool_rig, svc, "rebasers", "c")
    assert earlier.attempt() is None
    assert later.attempt() is None
    pool_rig.clock.advance(minutes=6)

    # the later one looks first: the ending would serve the earlier one
    assert later.attempt() is None
    assert lease_state(pool_rig, old.lease_id) == "active"
    granted = earlier.attempt()

    assert granted is not None
    # so the transition names the request that got the member
    assert transitions(pool_rig, old.lease_id)[-1] == ("preempted", "preempted by request-b")
    assert [row.id for row in waiting_rows(pool_rig)] == ["request-c"]


# -- a team is taken, and takes, as one ----------------------------------------


def review(pool_rig, *, workers: int | None = FIVE_MINUTES, critics: int | None = FIVE_MINUTES):
    """The review team's pools, "workers" on two members and "critics" on one,
    each with that ``preempt_after``."""
    config = team_config(pool_rig)
    worker_pool, critic_pool = config.pools
    return dataclasses.replace(config, pools=(
        dataclasses.replace(worker_pool, preempt_after=workers),
        dataclasses.replace(critic_pool, preempt_after=critics),
    ))


def states(pool_rig, grant) -> list[str]:
    return [lease_state(pool_rig, member.lease_id) for member in grant.members]


def test_a_team_in_the_way_is_taken_whole_with_one_transition_for_each_member(pool_rig, stamps):
    svc = pool_rig.service(review(pool_rig))
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    worker, critic = team.members
    pool_rig.clock.advance(minutes=6)
    alone = asks(pool_rig, svc, "critics", "b")
    assert alone.attempt() is None

    granted = alone.attempt()

    # only the critic stood in the way, and the worker goes with it
    assert granted is not None and granted.agent == critic.agent
    assert states(pool_rig, team) == ["preempted", "preempted"]
    for member in team.members:
        assert transitions(pool_rig, member.lease_id) == [
            ("granted", "requested"), ("preempted", "preempted by request-b"),
        ]
    # the worker's member was not started again, so it still says how it ended
    record = agent_lease.read_ended(AgentPaths.resolve(worker.agent).root)
    assert (record["cause"], record["request_id"]) == ("preempted", "request-b")
    refused = prompt(worker, "still there?")
    assert refused.exit_code != 0
    assert "lease preempted by request-b" in refused.stderr


def test_a_team_is_not_taken_while_one_member_was_active_inside_its_pools_value(
    pool_rig, stamps
):
    svc = pool_rig.service(review(pool_rig))
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    worker, critic = team.members
    alone = asks(pool_rig, svc, "critics", "b")
    assert alone.attempt() is None
    pool_rig.clock.advance(minutes=4)
    stamps[worker.agent] = {"state": "idle", "last_activity": text(pool_rig.clock())}
    pool_rig.clock.advance(minutes=4)

    # the critic has been idle for eight minutes, the worker for four
    assert alone.attempt() is None
    assert states(pool_rig, team) == ["active", "active"]

    pool_rig.clock.advance(minutes=2)
    assert alone.attempt() is not None
    assert states(pool_rig, team) == ["preempted", "preempted"]


def test_a_team_with_a_member_in_a_turn_is_not_taken(pool_rig, stamps):
    svc = pool_rig.service(review(pool_rig))
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    worker, _critic = team.members
    stamps[worker.agent] = {"state": "working", "last_activity": text(pool_rig.clock())}
    alone = asks(pool_rig, svc, "critics", "b")
    assert alone.attempt() is None
    pool_rig.clock.advance(minutes=9)

    assert alone.attempt() is None

    assert states(pool_rig, team) == ["active", "active"]


def test_a_team_with_one_pool_lacking_preempt_after_is_never_taken(pool_rig, stamps):
    svc = pool_rig.service(review(pool_rig, workers=None))
    team = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    alone = asks(pool_rig, svc, "critics", "b")
    assert alone.attempt() is None
    pool_rig.clock.advance(minutes=9)

    # the critics' pool declares it, the workers' does not
    assert alone.attempt() is None
    assert states(pool_rig, team) == ["active", "active"]

    # it waits for the team's expiry, as for any lease
    pool_rig.clock.advance(minutes=2)
    assert alone.attempt() is not None
    assert {kind for m in team.members for kind, _ in transitions(pool_rig, m.lease_id)} == {
        "granted", "expired",
    }


def test_a_waiting_team_takes_the_idle_lease_that_holds_its_role(pool_rig, stamps):
    svc = pool_rig.service(review(pool_rig))
    critic = svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    worker = svc.grant("workers", holder_label="b", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)
    team = asks_team(pool_rig, svc, "review", "lead")
    assert team.attempt() is None

    granted = team.attempt()

    assert isinstance(granted, teamlease.TeamGrant)
    assert transitions(pool_rig, critic.lease_id)[-1] == (
        "preempted", "preempted by request-lead",
    )
    # the workers' pool had a member free: its idle lease was in no one's way
    assert lease_state(pool_rig, worker.lease_id) == "active"
    assert waiting_rows(pool_rig) == []


def test_a_waiting_team_takes_nothing_when_what_may_be_taken_does_not_make_it_fit(
    pool_rig, stamps
):
    svc = pool_rig.service(review(pool_rig, critics=None))
    svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    first = svc.grant("workers", holder_label="b", cwd=pool_rig.work)
    second = svc.grant("workers", holder_label="c", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)
    team = asks_team(pool_rig, svc, "review", "lead")

    assert team.attempt() is None
    assert team.attempt() is None

    # the workers' leases may be taken, and the team would still lack its critic
    assert [lease_state(pool_rig, g.lease_id) for g in (first, second)] == ["active", "active"]
    assert len(active(pool_rig)) == 3


# -- structure ----------------------------------------------------------------

RANKS = ("priority", "rank", "weight", "urgen", "importan", "preced")


def route_fields() -> set[str]:
    """The fields the lease request route reads from its body."""
    tree = ast.parse((SRC / "daemon" / "pool_routes.py").read_text(encoding="utf-8"))
    route = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lease_request"
    )
    return {
        constant.value
        for call in ast.walk(route)
        if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_body"
        for keyword in call.keywords
        for constant in ast.walk(keyword.value)
        if isinstance(constant, ast.Constant) and isinstance(constant.value, str)
    }


def test_no_request_ranks_above_another():
    names = {*pool_config.POOL_FIELDS, *route_fields()}
    for record in (WaitingRequest, waiting.Waiting, ledger.Request, Pool, preempt.Unit):
        names.update(f.name for f in dataclasses.fields(record))
    for function in (
        service.PoolService.grant, service.PoolService.grant_team, ledger.place,
        preempt.eligible, preempt.units, preempt.fewest,
    ):
        names.update(inspect.signature(function).parameters)

    # the walk reaches the fields
    assert {"pool", "wait", "egress", "waiting_id", "preempt_after", "arrived"} <= names
    assert [name for name in names if any(word in name.lower() for word in RANKS)] == []


def test_the_rule_reads_no_clock_no_file_and_no_service():
    tree = ast.parse((SRC / "pool" / "preempt.py").read_text(encoding="utf-8"))
    assert not called_names(tree) & {
        "now", "utcnow", "time", "monotonic", "open", "read_text", "open_store",
        "list_leases", "get_lease", "end_lease", "status",
    }
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not [
        m for m in modules if any(word in m for word in ("agent", "runner", "service", "store"))
        and m != "..daemon.poolstore"
    ]


def test_a_lease_is_taken_only_through_the_one_function_that_ends_a_lease():
    """'preempted' is written where a lease ends and where a team's leases are
    ended one by one, both under the service's one function."""
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
                if any(isinstance(v, ast.Constant) and v.value == "preempted" for v in values):
                    found.add((str(path.relative_to(SRC)), function.name, node.func.attr))
    assert found == {
        ("pool/service.py", "_take_idle", "end_lease"),
        ("pool/service.py", "_take_idle", "end_members"),
    }
