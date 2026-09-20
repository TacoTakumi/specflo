"""A former holder learns how its lease ended, even when the member is leased again.

When a lease ends the pool stops the member's host, and the former holder's
next verb finds the host gone and is told the cause. A member that is leased
again at once - always so in a pool of one member, and the usual case after a
preemption, because the request is granted the member it took - has a new host
with a new wall, and the wall alone would say only that someone else holds the
lease. So the pool keeps the ended record of a lease for its former holder,
under the hash of the token that holder presents, and starting the member for
the next lease leaves it there. A verb the wall turns away looks for the
record of its own token: the former holder is told 'preempted', 'expired' or
'released', and a token that never held a lease on the member gets the wall's
refusal and nothing else. What is kept for one member is bounded.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from specflo.agent import lease as agent_lease
from specflo.agent.cli import EXIT_GENERIC, EXIT_UNREACHABLE, agent_app
from specflo.agent.statefiles import AgentPaths
from specflo.pool import service

from .test_expiry import prompt, real_time
from .test_preempt import preemptible
from .test_waiting import asks


@pytest.fixture
def stamps(monkeypatch):
    """The hosts' stamps by agent name, told by the test in place of the hosts."""
    told: dict[str, dict] = {}
    monkeypatch.setattr(service.runner, "status", lambda name: told.get(name))
    return told


def verb(agent: str, *args: str):
    return CliRunner().invoke(agent_app, ["prompt", agent, "anyone there?", *args])


def released_and_leased_again(pool_rig):
    """The one member's lease is released and the member is leased again:
    the former holder's grant and the new holder's."""
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.end_lease(old.lease_id, "released")
    new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert new.agent == old.agent
    return svc, old, new


# -- the three endings, each with the member leased again ----------------------


def test_a_preempted_holder_is_told_who_took_its_lease(pool_rig, stamps):
    svc = pool_rig.service(preemptible(pool_rig))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)
    second = asks(pool_rig, svc, "rebasers", "b")
    assert second.attempt() is None
    granted = second.attempt()
    assert granted is not None and granted.agent == old.agent

    refused = prompt(old, "still there?")

    assert refused.exit_code == EXIT_UNREACHABLE
    assert "lease preempted by request-b" in refused.stderr
    assert "leased to another holder" not in refused.stderr
    assert prompt(granted, "hello").exit_code == 0
    svc.end_lease(granted.lease_id, "released")


def test_an_expired_holder_is_told_so_after_the_member_is_leased_again(pool_rig):
    real_time(pool_rig)
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)
    new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert new.agent == old.agent

    refused = prompt(old, "still there?")

    assert refused.exit_code == EXIT_UNREACHABLE
    assert "lease expired" in refused.stderr
    assert prompt(new, "hello").exit_code == 0
    svc.end_lease(new.lease_id, "released")


def test_a_released_holder_is_told_so_after_the_member_is_leased_again(pool_rig):
    svc, old, new = released_and_leased_again(pool_rig)

    refused = prompt(old, "still there?")

    assert refused.exit_code == EXIT_UNREACHABLE
    assert "lease released" in refused.stderr
    assert prompt(new, "hello").exit_code == 0
    svc.end_lease(new.lease_id, "released")


def test_each_former_holder_reads_its_own_ending(pool_rig):
    svc, first, second = released_and_leased_again(pool_rig)
    real_time(pool_rig)
    pool_rig.clock.advance(minutes=11)
    third = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)

    assert "lease released" in prompt(first, "still there?").stderr
    assert "lease expired" in prompt(second, "still there?").stderr
    # and with the host gone too, not the last ending but its own
    svc.end_lease(third.lease_id, "released")
    gone = prompt(second, "still there?")
    assert gone.exit_code == EXIT_UNREACHABLE
    assert "lease expired" in gone.stderr


# -- a token that never held a lease on the member -----------------------------


def test_a_strangers_token_gets_the_wall_and_learns_of_no_ending(pool_rig):
    svc, old, new = released_and_leased_again(pool_rig)

    for refused in (verb(old.agent, "--lease-token", "never-held"), verb(old.agent)):
        assert refused.exit_code == EXIT_GENERIC
        assert "leased to another holder" in refused.stderr
        assert "released" not in refused.output
        assert "ended" not in refused.output
    svc.end_lease(new.lease_id, "released")


# -- the records themselves ----------------------------------------------------


def test_the_agent_side_hashes_a_token_as_the_pool_does():
    for token in ("", "a", "lease-token-0123456789abcdef", "caf\u00e9"):
        assert agent_lease.token_hash(token) == service.hash_token(token)


def test_a_record_is_found_by_its_holders_token_alone(tmp_path):
    agent_lease.write_ended(
        tmp_path, "preempted", "request-b", holder=agent_lease.token_hash("mine")
    )

    record = agent_lease.read_ended(tmp_path, "mine")
    assert (record["cause"], record["request_id"]) == ("preempted", "request-b")
    assert agent_lease.read_ended(tmp_path, "not-mine") is None
    assert agent_lease.read_ended(tmp_path, "") is None
    # the token is not written down, and a new lease leaves the record there
    assert all(b"mine" not in p.read_bytes() for p in tmp_path.rglob("*") if p.is_file())
    agent_lease.clear_ended(tmp_path)
    assert agent_lease.read_ended(tmp_path, "mine")["cause"] == "preempted"


def test_a_record_for_the_holder_alone_leaves_the_agent_no_last_ending(tmp_path):
    # how a lease on a developer's console ends: the host runs on, and a last
    # ending beside it would answer the developer's own verbs, which carry
    # no token
    written = agent_lease.write_ended(
        tmp_path, "released", holder=agent_lease.token_hash("mine"), last=False
    )

    assert written is None
    assert agent_lease.read_ended(tmp_path) is None
    assert agent_lease.read_ended(tmp_path, "mine")["cause"] == "released"


def test_a_holder_that_is_no_token_hash_is_refused(tmp_path):
    with pytest.raises(ValueError):
        agent_lease.write_ended(tmp_path, "released", holder="../status")
    assert list(tmp_path.iterdir()) == []


def test_the_records_kept_for_one_member_are_bounded(tmp_path):
    root = AgentPaths.resolve("local-1", tmp_path).ensure().root
    count = agent_lease.ENDED_KEPT + 2
    for i in range(count):
        agent_lease.write_ended(
            root, "released", holder=agent_lease.token_hash(f"token-{i}"),
            ended_at=f"2026-03-01T12:{i:02d}:00.000+00:00",
        )

    kept = [i for i in range(count) if agent_lease.read_ended(root, f"token-{i}")]
    # the oldest go, the newest stay
    assert kept == list(range(2, count))
