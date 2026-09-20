"""A team is renewed and expires as one: one idle limit, and one last activity.

Every member lease of a team is given the same idle limit: the shortest
default among the team's pools, or what the request asks when the shortest
maximum among them allows it. A higher request is refused before anything is
granted, with that maximum and the pool that sets it named.

No verb renews a lease, so a team is renewed by how its leases are read: the
last activity of a member lease of a team is the latest last activity among
the team's active member leases. Prompts to the worker alone therefore keep
the critic's lease, and with no activity anywhere every member lease is due
at the same look. Each one is ended as any expired lease is, with an
'expired' transition of its own, and the rows share the team lease id.

The rule is a function of the lease rows, the status records and the time, so
a fake clock drives it. Where the service is asked, the hosts' stamps are
told by the fake clock too, in place of the hosts' own; one test reads a real
host, whose running turn counts as activity now.

The expiry check runs before each ending, and a member that has ended renews
no one. So a team that one member has kept is given back with that member
ended last: every member lease is then recorded, stopped and told of as
released, whoever ends it, and a team past its limit expires whole all the same.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from specflo.agent.cli import EXIT_UNREACHABLE
from specflo.agent.client import connect
from specflo.daemon import auth
from specflo.daemon.app import create_app
from specflo.daemon.poolstore import Lease, Resource
from specflo.pool import expiry, service, teamlease
from specflo.pool.config import Pool
from specflo.service.pool_remote import release_path

from .test_expiry import TEN_MINUTES, at, prompt, text
from .test_runner import pid_alive, wait_until
from .test_team_lease import active, team_config

HOUR = 3600


def pool(name: str, idle_default: int, idle_max: int) -> Pool:
    return Pool(
        name=name, definition="rebaser", members=("local-1",), size=1,
        idle_default=idle_default, idle_max=idle_max,
    )


# -- the idle limit: one for the whole team -----------------------------------


def test_the_teams_idle_limit_is_the_shortest_default_among_its_pools():
    pools = [pool("workers", 1800, 4 * HOUR), pool("critics", 600, 2 * HOUR)]

    limits = teamlease.idle_limits(pools, None, service._idle_limit)

    assert limits == {"workers": 600, "critics": 600}
    assert teamlease.idle_limits(pools[::-1], None, service._idle_limit) == limits


def test_a_requested_limit_is_every_members_up_to_the_shortest_maximum():
    pools = [pool("workers", 1800, 4 * HOUR), pool("critics", 600, 2 * HOUR)]

    assert teamlease.idle_limits(pools, 900, service._idle_limit) == {
        "workers": 900, "critics": 900,
    }
    # the shortest maximum itself is allowed
    assert teamlease.idle_limits(pools, 2 * HOUR, service._idle_limit) == {
        "workers": 2 * HOUR, "critics": 2 * HOUR,
    }


@pytest.mark.parametrize("order", [1, -1], ids=["strictest-last", "strictest-first"])
def test_a_request_above_the_shortest_maximum_is_refused_naming_it_and_its_pool(order):
    pools = [pool("workers", 1800, 4 * HOUR), pool("critics", 600, 2 * HOUR)][::order]

    with pytest.raises(service.IdleLimitError) as refused:
        teamlease.idle_limits(pools, 2 * HOUR + 1, service._idle_limit)

    assert "'critics'" in str(refused.value)
    assert "2h" in str(refused.value)
    assert "workers" not in str(refused.value)


def test_a_limit_that_is_no_time_is_refused_for_a_team_as_for_a_lease():
    with pytest.raises(service.IdleLimitError):
        teamlease.idle_limits([pool("workers", 600, HOUR)], 0, service._idle_limit)


def uneven(pool_rig):
    """The review team's pools with limits that differ: the critics' are the shorter."""
    config = team_config(pool_rig)
    workers, critics = config.pools
    return dataclasses.replace(config, pools=(
        dataclasses.replace(workers, idle_default=1800, idle_max=4 * HOUR),
        dataclasses.replace(critics, idle_default=TEN_MINUTES, idle_max=2 * HOUR),
    ))


def test_every_member_lease_of_a_granted_team_carries_the_teams_limit(pool_rig):
    svc = pool_rig.service(uneven(pool_rig))

    with pytest.raises(service.IdleLimitError, match="critics"):
        svc.grant_team("review", holder_label="lead", cwd=pool_rig.work, idle_limit=3 * HOUR)
    with pool_rig.store() as store:
        assert store.list_leases() == []

    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)

    # the worker's pool would give a lease of its own half an hour
    assert [lease.idle_limit for lease in active(pool_rig)] == [TEN_MINUTES, TEN_MINUTES]
    teamlease.release_team(svc, grant.team_lease_id)


# -- the rule: a team member is judged by the team's last activity -------------


def row(lease_id: str, team: str | None, *, last_activity: float = 0) -> Lease:
    """An active lease of ten minutes, of that team, last touched at that minute."""
    return Lease(
        id=lease_id, team_lease_id=team, holder_hash="0" * 64, holder_label="lead",
        member=lease_id, pool="workers",
        resources=(Resource("pool", "workers"), Resource("member", lease_id)),
        acquired=text(at(0)), last_activity=text(at(last_activity)),
        idle_limit=TEN_MINUTES, state="active",
    )


def stamped(state: str, minute: float) -> dict:
    return {"state": state, "last_activity": text(at(minute))}


def test_a_team_members_last_activity_is_the_latest_among_the_teams_leases():
    worker, critic = row("worker", "team-1"), row("critic", "team-1")
    alone = row("alone", None)
    read = [(worker, stamped("idle", 25)), (critic, stamped("idle", 0)), (alone, None)]

    seen = expiry.judged_activity(read, at(30))

    assert seen == {"worker": at(25), "critic": at(25), "alone": at(0)}
    # each member's own time is still its own
    assert expiry.last_activity(critic, stamped("idle", 0), at(30)) == at(0)


def test_a_working_member_makes_the_whole_team_active_now():
    worker, critic = row("worker", "team-1"), row("critic", "team-1")
    read = [(worker, stamped("working", 5)), (critic, stamped("idle", 0))]

    assert expiry.judged_activity(read, at(40)) == {"worker": at(40), "critic": at(40)}
    assert expiry.due(read, at(40)) == []


def test_one_teams_activity_renews_no_other_team_and_no_lease_of_no_team():
    read = [
        (row("worker", "team-1"), stamped("idle", 25)),
        (row("critic", "team-1"), None),
        (row("other-worker", "team-2"), stamped("idle", 3)),
        (row("other-critic", "team-2"), None),
        (row("alone", None), stamped("idle", 1)),
    ]

    seen = expiry.judged_activity(read, at(30))

    assert seen["other-worker"] == seen["other-critic"] == at(3)
    assert seen["alone"] == at(1)
    assert [lease.id for lease in expiry.due(read, at(30))] == [
        "other-worker", "other-critic", "alone",
    ]


def test_with_no_activity_every_member_of_a_team_is_due_at_minute_11_and_none_before():
    read = [(row("worker", "team-1"), stamped("idle", 0)), (row("critic", "team-1"), None)]

    assert expiry.due(read, at(9)) == []
    assert [lease.id for lease in expiry.due(read, at(11))] == ["worker", "critic"]


def test_a_member_granted_later_than_the_others_holds_the_team_no_longer_than_its_limit():
    # the members' rows are written one after the other; the team is one all the same
    read = [(row("worker", "team-1"), None), (row("critic", "team-1", last_activity=0.5), None)]

    assert expiry.due(read, at(10.25)) == []
    assert [lease.id for lease in expiry.due(read, at(10.5))] == ["worker", "critic"]


# -- the service: the team is kept, and expires, as one ------------------------


@pytest.fixture
def stamps(pool_rig, monkeypatch):
    """The hosts' stamps by agent name, told by the test in place of the hosts."""
    told: dict[str, dict] = {}
    monkeypatch.setattr(service.runner, "status", lambda name: told.get(name))
    return told


def states(pool_rig, grant) -> list[str]:
    with pool_rig.store() as store:
        return [store.get_lease(member.lease_id).state for member in grant.members]


def test_prompts_to_the_worker_alone_keep_the_critics_lease_active_at_minute_30(
    pool_rig, stamps
):
    svc = pool_rig.service(uneven(pool_rig))
    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    worker, critic = grant.members
    stamps[critic.agent] = {"state": "idle", "last_activity": text(pool_rig.clock())}

    for _ in range(3):  # the worker is prompted at minutes 9, 18 and 27
        pool_rig.clock.advance(minutes=9)
        stamps[worker.agent] = {"state": "idle", "last_activity": text(pool_rig.clock())}
        assert svc.expire_due() == []
    pool_rig.clock.advance(minutes=3)

    assert svc.expire_due() == []
    assert states(pool_rig, grant) == ["active", "active"]
    assert pid_alive(pool_rig.status(critic.agent)["pi_pid"])

    # and once the worker falls silent too, the team goes as one
    pool_rig.clock.advance(minutes=7)
    assert [e.lease_id for e in svc.expire_due()] == [worker.lease_id, critic.lease_id]


def test_with_no_activity_every_member_lease_expires_together_at_minute_11(pool_rig, stamps):
    svc = pool_rig.service(uneven(pool_rig))
    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    pids = [pool_rig.status(member.agent)["pi_pid"] for member in grant.members]

    pool_rig.clock.advance(minutes=9)
    assert svc.expire_due() == []
    assert states(pool_rig, grant) == ["active", "active"]

    pool_rig.clock.advance(minutes=2)
    ended = svc.expire_due()

    assert [(e.lease_id, e.state, e.cause) for e in ended] == [
        (member.lease_id, "expired", "expired") for member in grant.members
    ]
    with pool_rig.store() as store:
        rows = store.list_leases(team_lease_id=grant.team_lease_id)
        assert [lease.state for lease in rows] == ["expired", "expired"]
        assert {lease.team_lease_id for lease in rows} == {grant.team_lease_id}
        for member in grant.members:
            kinds = [t.kind for t in store.list_transitions(lease_id=member.lease_id)]
            assert kinds == ["granted", "expired"]
    for pid in pids:
        assert wait_until(lambda pid=pid: not pid_alive(pid))
    assert svc.expire_due() == []


def test_a_lease_of_no_team_beside_a_kept_team_expires_by_itself(pool_rig, stamps):
    svc = pool_rig.service(uneven(pool_rig))
    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    alone = svc.grant("workers", holder_label="b", cwd=pool_rig.work, idle_limit=TEN_MINUTES)
    worker, _critic = grant.members

    pool_rig.clock.advance(minutes=9)
    stamps[worker.agent] = {"state": "idle", "last_activity": text(pool_rig.clock())}
    pool_rig.clock.advance(minutes=2)

    assert [e.lease_id for e in svc.expire_due()] == [alone.lease_id]
    assert states(pool_rig, grant) == ["active", "active"]
    teamlease.release_team(svc, grant.team_lease_id)


def test_a_turn_that_runs_on_the_worker_keeps_the_critic_whose_host_is_idle(pool_rig):
    # the hosts stamp with their own clock, so the fake one starts at the real time
    pool_rig.clock.now = datetime.now(timezone.utc)
    pool_rig.scenario(mode="never_settle")
    svc = pool_rig.service(uneven(pool_rig))
    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    worker, critic = grant.members
    with connect(worker.agent, lease_token=worker.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(worker.agent)["state"] == "working")
        pool_rig.clock.advance(minutes=30)

        assert svc.expire_due() == []

        assert states(pool_rig, grant) == ["active", "active"]
        assert pool_rig.status(critic.agent)["state"] != "working"
    teamlease.release_team(svc, grant.team_lease_id)


# -- the release: a team that one member has kept is given back as released ----


def kept_by(pool_rig, stamps, keeper: int):
    """The review team at minute 30, kept by prompts at minutes 9, 18 and 27 to
    the member *keeper* alone; no other member was ever prompted."""
    svc = pool_rig.service(uneven(pool_rig))
    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    for _ in range(3):
        pool_rig.clock.advance(minutes=9)
        stamps[grant.members[keeper].agent] = {
            "state": "idle", "last_activity": text(pool_rig.clock()),
        }
    pool_rig.clock.advance(minutes=3)
    return svc, grant


def endings(pool_rig, grant) -> list[list[tuple[str, str]]]:
    """The kind and the cause of every transition of each member lease, in grant order."""
    with pool_rig.store() as store:
        return [
            [(t.kind, t.cause) for t in store.list_transitions(lease_id=member.lease_id)]
            for member in grant.members
        ]


@pytest.mark.parametrize("keeper", [0, 1], ids=["kept-by-the-worker", "kept-by-the-critic"])
def test_a_team_one_member_has_kept_is_released_at_minute_30_and_no_member_expires(
    pool_rig, stamps, monkeypatch, keeper
):
    svc, grant = kept_by(pool_rig, stamps, keeper)
    stopped, stop = [], service.runner.stop
    monkeypatch.setattr(
        service.runner, "stop",
        lambda name, kind, **told: (stopped.append((name, kind)), stop(name, kind, **told))[1],
    )

    ended = teamlease.release_team(svc, grant.team_lease_id)

    # reported in grant order, whatever order the members were ended in
    assert [(e.lease_id, e.state, e.cause) for e in ended] == [
        (member.lease_id, "released", "released") for member in grant.members
    ]
    assert states(pool_rig, grant) == ["released", "released"]
    assert endings(pool_rig, grant) == [[("granted", "requested"), ("released", "released")]] * 2
    # the member that kept the team is the last to end, and each is stopped as released
    assert stopped == [
        (member.agent, "released")
        for member in sorted(grant.members, key=lambda m: m is grant.members[keeper])
    ]
    for member in grant.members:
        told = prompt(member, "still there?")
        assert told.exit_code == EXIT_UNREACHABLE
        assert "lease released" in told.stderr
        assert "expired" not in told.stderr


def test_the_release_route_answers_released_and_records_no_member_as_expired(pool_rig, stamps):
    svc, grant = kept_by(pool_rig, stamps, 0)
    app = create_app(pool_rig.root)
    app.state.pool = svc
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {auth.mint_token(pool_rig.root, 'developer')}"

    # what the release verb prints is this answer's state
    response = client.post(
        release_path(grant.team_lease_id), json={"token": grant.members[0].token}
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {
        "lease_id": grant.team_lease_id, "state": "released", "held": True,
    }
    assert [kinds[-1] for kinds in endings(pool_rig, grant)] == [("released", "released")] * 2


def test_a_member_that_ended_before_the_release_is_reported_as_it_ended(pool_rig, stamps):
    svc, grant = kept_by(pool_rig, stamps, 0)
    worker, critic = grant.members
    svc.end_lease(critic.lease_id, "released", cause="its host was lost")

    ended = teamlease.release_team(svc, grant.team_lease_id)

    assert [(e.lease_id, e.state, e.cause) for e in ended] == [
        (worker.lease_id, "released", "released"),
        (critic.lease_id, "released", "its host was lost"),
    ]


def test_a_team_past_its_limit_expires_as_one_though_a_release_is_what_finds_it(
    pool_rig, stamps
):
    svc, grant = kept_by(pool_rig, stamps, 0)
    pool_rig.clock.advance(minutes=7)  # the worker's last prompt is ten minutes old

    ended = teamlease.release_team(svc, grant.team_lease_id)

    assert [(e.lease_id, e.state) for e in ended] == [
        (member.lease_id, "expired") for member in grant.members
    ]
    assert [kinds[-1] for kinds in endings(pool_rig, grant)] == [("expired", "expired")] * 2


def test_each_teams_keeper_is_put_last_in_the_places_its_team_holds_and_no_other_id_moves():
    read = [
        (row("alone", None), stamped("idle", 1)),
        (row("worker", "team-1"), stamped("idle", 27)),
        (row("other-worker", "team-2"), stamped("working", 0)),
        (row("critic", "team-1"), None),
        (row("other-critic", "team-2"), stamped("idle", 29)),
        (row("second-critic", "team-1"), None),
    ]
    given = [
        "alone", "worker", "other-worker", "ended", "critic", "other-critic", "second-critic",
    ]

    ordered = teamlease.keeper_last(given, read, at(30))

    # members idle alike keep their order; an id that was not read stays
    assert ordered == [
        "alone", "critic", "other-critic", "ended", "second-critic", "other-worker", "worker",
    ]
