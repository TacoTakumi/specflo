"""A lease on a console: the pool binds it and ends it, and the process stays the developer's.

An attached console is leased like any member, by the same ledger and the
same rows, with one difference: its pi is the developer's own. A grant binds
the lease's token on the host that runs already; it starts no process,
generates no pi configuration and applies nothing of the pool's definition.
Behind the wall it has that pi clear its conversation in place, so a holder
gets nothing of a former holder's turns or of the developer's own, and a pi
that does not clear is not leased.
However the lease ends - released, expired or preempted - the pool lowers the
wall, aborts a turn the holder left running and records the ending for the
former holder. The host and its pi are left as they were, under the same
process ids.

A detach while a lease is out leaves that lease alone: the slot drains, and
is offline once the lease has ended, until the developer attaches again -
the same host, if the developer likes. A console host that dies while it is
attached reads offline from then on, so a request to a pool that only it
serves waits, as for a console no one attached.

The console's agent host is a real one, the stub pi under ``specflo agent
start``, which no pool started; the helpers are the attach tests' own.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal

import pytest
from typer.testing import CliRunner

from specflo.agent import lease as agent_lease
from specflo.agent.cli import EXIT_UNREACHABLE, agent_app
from specflo.agent.client import connect
from specflo.agent.statefiles import AgentPaths
from specflo.cli import app
from specflo.daemon import pool_routes
from specflo.daemon.poolstore import ConsoleAttachment, Lease, Resource
from specflo.pool import console, ledger, runner, service, waiting
from specflo.pool.config import Member

from .test_console_attach import AGENT, SLOT, asks, console_member, console_rows, start_host
from .test_console_attach import console_daemon, short_interval  # noqa: F401  (fixtures)
from .test_expiry import prompt, real_time
from .test_lease_request import checkout, pool_daemon  # noqa: F401  (fixtures)
from .test_preempt import stamped
from .test_preempt import stamps  # noqa: F401  (fixture)
from .test_runner import POOL_TOKEN, pid_alive, wait_until

cli = CliRunner()


def hosted_console() -> Member:
    """A console whose model is a hosted one: a member the pool starts for
    that model gets a pi configuration directory generated for its lease."""
    return Member(
        name=SLOT, command="", backing="hosted", labels=(), capacity=1, egress="no-train",
        model="some-vendor/some-model", account="team-a", kind="console",
    )


def attached(pool_rig, *members: Member, **pool):
    """A service on a pool of *members*, the one local console without any,
    with the developer's host running and attached; the service and the
    host's status before any lease."""
    svc = pool_rig.service(pool_rig.config(*(members or (console_member(),)), **pool))
    before = start_host(pool_rig)
    console.attach(svc, SLOT, AGENT)
    return svc, before


def same_process(pool_rig, before: dict) -> bool:
    """Does the developer's host still run, with the pi it ran before?"""
    now = pool_rig.status(AGENT)
    return (
        (now["host_pid"], now["pi_pid"]) == (before["host_pid"], before["pi_pid"])
        and pid_alive(now["host_pid"]) and pid_alive(now["pi_pid"])
    )


def statuses(pool_rig) -> dict:
    """What the service reads of the attached hosts, by agent name."""
    return {row.agent: runner.status(row.agent) for row in console_rows(pool_rig)}


def slot_state(pool_rig) -> str:
    with pool_rig.store() as store:
        out = store.list_leases(state="active")
    return console.state(SLOT, console_rows(pool_rig), out, statuses(pool_rig))


def last(grant):
    """What `specflo agent last` prints for the holder of *grant*."""
    return cli.invoke(agent_app, ["last", grant.agent, "--lease-token", grant.token])


def kill_host(before: dict) -> None:
    for pid in (before["host_pid"], before["pi_pid"]):
        os.kill(pid, signal.SIGKILL)
    assert wait_until(lambda: not pid_alive(before["host_pid"]))


def kill_pi(rig, before: dict) -> None:
    """Kill the console's pi and leave the developer's host running: the host
    writes exited as soon as pi's output ends."""
    os.kill(before["pi_pid"], signal.SIGKILL)
    assert wait_until(lambda: rig.status(AGENT)["state"] == "exited")
    assert pid_alive(before["host_pid"])


def working(rig, name: str = AGENT) -> None:
    """Start a turn of the developer's own on the console's pi; the stub pi
    is in never_settle mode, so the turn runs until it is aborted."""
    with connect(name) as developer:
        developer.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: rig.status(name)["state"] == "working")


# -- the grant ----------------------------------------------------------------


def test_a_lease_on_a_console_starts_no_process_and_applies_no_definition(pool_rig):
    svc, before = attached(pool_rig, hosted_console())
    started = pool_rig.recorded()

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert grant.agent == AGENT
    assert same_process(pool_rig, before)
    # the one pi that was ever started is the developer's, with the developer's
    # own command line: no prompt, tool list or deny list of the definition
    assert pool_rig.recorded() == started
    assert started["pid"] == before["pi_pid"]
    assert not {"--append-system-prompt", "--tools", "--no-tools"} & set(started["argv"])
    # nothing to launch from and no generated pi configuration, under either name
    for name in (AGENT, SLOT):
        root = AgentPaths.resolve(name).root
        assert not (root / runner.LAUNCH_FILE).exists()
        assert not (root / runner.CONFIG_DIR_FILE).exists()
    assert not pool_rig.config_root.exists() or not list(pool_rig.config_root.iterdir())
    assert pool_rig.pane_names() == []
    assert prompt(grant, "hello").exit_code == 0


def test_a_live_host_that_does_not_take_the_lease_leaves_no_active_lease_and_runs_on(pool_rig):
    svc, before = attached(pool_rig)
    with connect(AGENT) as host:
        # a lease the pool's rows know nothing of is bound on the host
        host.lease_bind(POOL_TOKEN, "token-of-no-row")

    with pytest.raises(runner.RunnerError, match=AGENT):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
    assert same_process(pool_rig, before)


# -- a console that takes no lease now -----------------------------------------


def test_a_console_whose_developer_is_in_a_turn_is_passed_over_for_a_free_member(pool_rig):
    pool_rig.scenario(mode="never_settle", recall=True)
    started = pool_rig.local_member()
    svc, before = attached(pool_rig, console_member(), started, size=2)
    working(pool_rig)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    # the member the pool starts serves the request; the console is passed over
    assert grant.agent == started.name
    with pool_rig.store() as store:
        assert [out.member for out in store.list_leases()] == [started.name]
    assert same_process(pool_rig, before)
    assert pool_rig.status(AGENT)["state"] == "working"


def test_a_console_whose_pi_exited_under_a_live_host_takes_no_lease(pool_rig):
    svc, before = attached(pool_rig)
    kill_pi(pool_rig, before)

    with pytest.raises(service.NoFreeMember, match=f"'{SLOT}'.*console"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases() == []


def test_a_console_that_takes_no_lease_now_is_attached_still(pool_rig):
    pool_rig.scenario(mode="never_settle", recall=True)
    svc, _ = attached(pool_rig)
    working(pool_rig)

    # the slot is the developer's and stays attached on the page; it is only
    # the placement that passes it over while the turn runs
    assert slot_state(pool_rig) == console.ATTACHED
    unmatched = console.unmatched(svc.config, console_rows(pool_rig), statuses(pool_rig))
    assert unmatched == frozenset({SLOT})


def test_the_bind_refuses_a_console_whose_turn_started_after_it_was_matched(pool_rig):
    pool_rig.scenario(mode="never_settle", recall=True, capture=str(pool_rig.capture))
    before = start_host(pool_rig)
    working(pool_rig)

    with pytest.raises(runner.ConsoleBusy, match=f"'{AGENT}'.*a turn"):
        runner.bind_console(AGENT, pool_token=POOL_TOKEN, lease_token="token-of-the-holder")

    # the wall never went up: the developer's own frame, with no token, is taken
    with connect(AGENT) as developer:
        assert developer.status()["status"]["state"] == "working"
    frames = [json.loads(line) for line in pool_rig.capture.read_text().splitlines()]
    assert "new_session" not in [f["type"] for f in frames]
    assert same_process(pool_rig, before)


def test_a_console_that_takes_no_lease_is_no_member_that_failed_to_start(pool_rig):
    asked = waiting.Waiting(
        pool_rig.service(pool_rig.config(console_member())), "rebasers",
        holder_label="a", cwd=pool_rig.work, wait=0, mint_id=lambda: "request-a",
    )

    answer = pool_routes._failed(asked, runner.ConsoleBusy(f"console '{SLOT}': a turn is running"))

    assert answer.status_code == 409
    assert SLOT in answer.detail and "a turn is running" in answer.detail


# -- the conversation ---------------------------------------------------------


def test_the_next_holder_of_a_console_gets_nothing_of_the_former_holders_conversation(pool_rig):
    pool_rig.scenario(reply="done", recall=True)
    svc, before = attached(pool_rig)
    first = svc.grant("rebasers", holder_label="project-a", cwd=pool_rig.work)
    assert prompt(first, "MARKER-OF-PROJECT-A").exit_code == 0
    svc.end_lease(first.lease_id, "released")

    second = svc.grant("rebasers", holder_label="project-b", cwd=pool_rig.work)

    # before a prompt of its own, the holder has no last answer to read
    read = last(second)
    assert read.exit_code == 0
    assert read.output.strip() == ""
    asked = prompt(second, "what were you told?")
    assert asked.exit_code == 0
    assert "MARKER-OF-PROJECT-A" not in asked.output
    assert "[recalls: nothing]" in asked.output
    svc.end_lease(second.lease_id, "released")
    # cleared in place: the pi is the one the developer started
    assert same_process(pool_rig, before)


def test_the_first_holder_gets_nothing_of_the_developers_own_conversation(pool_rig):
    pool_rig.scenario(reply="done", recall=True)
    svc = pool_rig.service(pool_rig.config(console_member()))
    before = start_host(pool_rig)
    told = cli.invoke(agent_app, ["prompt", AGENT, "MARKER-OF-THE-DEVELOPER"])
    assert told.exit_code == 0
    console.attach(svc, SLOT, AGENT)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert last(grant).output.strip() == ""
    asked = prompt(grant, "what were you told?")
    assert asked.exit_code == 0
    assert "MARKER-OF-THE-DEVELOPER" not in asked.output
    assert same_process(pool_rig, before)


def test_a_console_whose_pi_does_not_clear_its_conversation_is_not_granted(pool_rig):
    pool_rig.scenario(reply="done", recall=True, cancel_new_session=True)
    svc, before = attached(pool_rig)

    with pytest.raises(runner.RunnerError, match=f"'{AGENT}'.*did not clear its conversation"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    # the lease ended as one on a member that did not start
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
        [ended] = store.list_leases()
        causes = [t.cause for t in store.list_transitions(lease_id=ended.id)]
    assert ended.state == "released"
    assert any(c.startswith("member did not start") and AGENT in c for c in causes)
    # the wall is down again: the developer's own frame, with no token, is taken
    assert cli.invoke(agent_app, ["prompt", AGENT, "mine again"]).exit_code == 0
    assert same_process(pool_rig, before)


def test_a_bind_whose_pi_does_not_clear_lowers_the_wall_again(pool_rig):
    pool_rig.scenario(reply="done", recall=True, cancel_new_session=True)
    before = start_host(pool_rig)

    with pytest.raises(runner.RunnerError, match="cancelled"):
        runner.bind_console(AGENT, pool_token=POOL_TOKEN, lease_token="token-of-the-holder")

    # no lease is bound: a frame with no token reaches the developer's pi
    with connect(AGENT) as developer:
        assert developer.request({"type": "get_last_assistant_text"})["success"]
    assert same_process(pool_rig, before)


def test_a_console_in_a_turn_of_the_developers_is_not_cleared_and_not_granted(pool_rig):
    pool_rig.scenario(mode="never_settle", recall=True, capture=str(pool_rig.capture))
    svc, before = attached(pool_rig)
    working(pool_rig)

    # the one slot the pool has takes no lease now, so the request waits
    with pytest.raises(service.NoFreeMember, match=f"'{SLOT}'.*console"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert asks(pool_rig, svc, "a").attempt() is None

    frames = [json.loads(line) for line in pool_rig.capture.read_text().splitlines()]
    assert "new_session" not in [f["type"] for f in frames]
    with pool_rig.store() as store:
        assert store.list_leases() == []
    assert same_process(pool_rig, before)
    # the turn is the developer's: nothing of the request reached it
    assert "abort" not in [f["type"] for f in frames]
    assert pool_rig.status(AGENT)["state"] == "working"


def test_a_console_is_cleared_once_the_turn_its_last_holder_left_running_is_aborted(pool_rig):
    pool_rig.scenario(mode="never_settle", recall=True)
    svc, before = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    with connect(AGENT, lease_token=old.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(AGENT)["state"] == "working")
    svc.end_lease(old.lease_id, "released")

    new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    assert new.agent == AGENT
    assert same_process(pool_rig, before)


def test_a_member_the_pool_starts_is_sent_no_clearing(pool_rig):
    pool_rig.scenario(reply="done", recall=True, capture=str(pool_rig.capture))
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert prompt(grant, "hello").exit_code == 0
    frames = [json.loads(line) for line in pool_rig.capture.read_text().splitlines()]
    assert "new_session" not in [f["type"] for f in frames]
    svc.end_lease(grant.lease_id, "released")


def holder_verb(grant, verb: str, *args: str):
    """What `specflo agent <verb>` comes to for the holder of *grant*."""
    return cli.invoke(agent_app, [verb, grant.agent, *args, "--lease-token", grant.token])


def test_the_holders_stop_on_a_console_is_refused_and_the_process_stays_the_developers(pool_rig):
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    refused = holder_verb(grant, "stop", "--timeout", "5")

    assert refused.exit_code != 0
    assert "developer's console" in refused.stderr
    assert "not the holder's to stop" in refused.stderr
    assert same_process(pool_rig, before)
    # the rest of the lease is the holder's as before
    assert prompt(grant, "hello").exit_code == 0
    assert holder_verb(grant, "wait").exit_code == 0
    assert "done" in last(grant).output
    assert "hello" in holder_verb(grant, "log").output
    assert holder_verb(grant, "reset").exit_code == 0
    assert same_process(pool_rig, before)


def test_the_developer_stops_the_console_between_leases_with_no_token(pool_rig):
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert holder_verb(grant, "stop", "--timeout", "5").exit_code != 0
    svc.end_lease(grant.lease_id, "released")

    stopped = cli.invoke(agent_app, ["stop", AGENT])

    assert stopped.exit_code == 0, stopped.output
    assert wait_until(lambda: not pid_alive(before["host_pid"]))
    assert not pid_alive(before["pi_pid"])


def test_the_pools_token_stops_a_leased_console_as_before(pool_rig):
    svc, before = attached(pool_rig)
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with connect(AGENT) as daemon:
        assert daemon.request({"type": "stop", "pool_token": POOL_TOKEN})["success"] is True

    assert wait_until(lambda: not pid_alive(before["host_pid"]))


def test_the_holders_stop_on_a_member_the_pool_starts_works_as_before(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    running = pool_rig.status(grant.agent)

    stopped = holder_verb(grant, "stop")

    assert stopped.exit_code == 0, stopped.output
    assert wait_until(lambda: not pid_alive(running["host_pid"]))
    svc.end_lease(grant.lease_id, "released")


# -- the ending ---------------------------------------------------------------


def test_releasing_the_lease_clears_the_binding_and_leaves_the_pi_alive(pool_rig):
    svc, before = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    ended = svc.end_lease(old.lease_id, "released")

    assert (ended.state, ended.cause) == ("released", "released")
    assert same_process(pool_rig, before)
    # the ending is written down for the former holder alone: the host is the
    # developer's and runs on, and no ending of the pool's is theirs to read
    root = AgentPaths.resolve(AGENT).root
    assert agent_lease.read_ended(root) is None
    assert agent_lease.read_ended(root, old.token)["cause"] == "released"
    assert agent_lease.read_ended(root, "never-held") is None
    # the binding is gone: the host takes the next lease, and it is still attached
    assert slot_state(pool_rig) == console.ATTACHED
    new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert new.agent == AGENT
    assert same_process(pool_rig, before)
    assert prompt(new, "hello").exit_code == 0


def test_the_developers_own_verbs_are_told_nothing_of_a_lease_that_ended(pool_rig):
    svc, _ = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.end_lease(old.lease_id, "released")
    console.detach(svc, SLOT)

    # the developer is done for the day and stops the own agent, with no token
    stopped = cli.invoke(agent_app, ["stop", AGENT])
    assert stopped.exit_code == 0, stopped.output

    for verb in (["status", AGENT], ["log", AGENT], ["stop", AGENT]):
        done = cli.invoke(agent_app, verb)
        assert done.exit_code != EXIT_UNREACHABLE, f"{verb}: {done.output}"
        assert "lease released" not in done.output
    # the former holder still learns how its own lease ended
    root = AgentPaths.resolve(AGENT).root
    assert agent_lease.read_ended(root, old.token)["cause"] == "released"


def test_a_new_lease_on_the_console_forgets_the_last_ending(pool_rig):
    svc, _ = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.end_lease(old.lease_id, "released")

    svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    # a new holder whose host went away would read the former lease's ending
    root = AgentPaths.resolve(AGENT).root
    assert agent_lease.read_ended(root) is None
    assert agent_lease.read_ended(root, old.token)["cause"] == "released"


def test_a_released_holder_is_told_so_when_the_console_is_leased_again(pool_rig):
    svc, before = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.end_lease(old.lease_id, "released")
    new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    refused = prompt(old, "still there?")

    assert refused.exit_code == EXIT_UNREACHABLE
    assert "lease released" in refused.stderr
    assert prompt(new, "hello").exit_code == 0
    assert same_process(pool_rig, before)


def test_an_expired_lease_on_a_console_ends_the_same_way(pool_rig):
    real_time(pool_rig)
    svc, before = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)

    new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.get_lease(old.lease_id).state == "expired"
    assert new.agent == AGENT
    assert same_process(pool_rig, before)
    refused = prompt(old, "still there?")
    assert refused.exit_code == EXIT_UNREACHABLE
    assert "lease expired" in refused.stderr
    assert prompt(new, "hello").exit_code == 0


def test_a_preempted_lease_on_a_console_ends_the_same_way(pool_rig, stamps):  # noqa: F811
    svc, before = attached(pool_rig, preempt_after=300)
    stamps[AGENT] = stamped("idle", 0)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)
    second = asks(pool_rig, svc, "b")
    assert second.attempt() is None

    granted = second.attempt()

    assert granted is not None and granted.agent == AGENT
    with pool_rig.store() as store:
        assert store.get_lease(old.lease_id).state == "preempted"
    assert same_process(pool_rig, before)
    refused = prompt(old, "still there?")
    assert refused.exit_code == EXIT_UNREACHABLE
    assert "lease preempted by request-b" in refused.stderr
    assert prompt(granted, "hello").exit_code == 0


def test_a_turn_the_holder_left_running_is_aborted_and_the_pi_lives_on(pool_rig):
    pool_rig.scenario(mode="never_settle")
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    with connect(AGENT, lease_token=grant.token) as holder:
        holder.send({"type": "prompt", "message": "a long turn"})
        assert wait_until(lambda: pool_rig.status(AGENT)["state"] == "working")

    svc.end_lease(grant.lease_id, "released")

    assert wait_until(lambda: pool_rig.status(AGENT)["state"] == "idle")
    assert same_process(pool_rig, before)


def test_a_console_whose_slot_is_no_longer_declared_is_not_stopped(pool_rig):
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    # a later configuration: the slot is gone, and then a member the pool
    # starts stands under its name
    gone = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    renamed = Member(
        name=SLOT, command=pool_rig.command, backing="local", labels=(), capacity=1,
        egress="local", model="tc3",
    )
    for config in (pool_rig.config(pool_rig.local_member()), pool_rig.config(renamed)):
        with pool_rig.store() as store:
            lease = store.get_lease(grant.lease_id)
            assert console.leased(lease, config, store.list_consoles())

    gone.end_lease(grant.lease_id, "released")

    assert same_process(pool_rig, before)


def test_a_lease_on_a_member_the_pool_starts_is_no_console_lease(pool_rig):
    local = pool_rig.local_member()
    svc, _ = attached(pool_rig, console_member(), local)
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    grant = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert grant.agent == local.name
    pi_pid = pool_rig.status(local.name)["pi_pid"]

    with pool_rig.store() as store:
        lease = store.get_lease(grant.lease_id)
        assert not console.leased(lease, svc.config, store.list_consoles())
    svc.end_lease(grant.lease_id, "released")

    assert wait_until(lambda: not pid_alive(pi_pid))


# -- detach during a lease ----------------------------------------------------


def test_detach_during_a_lease_leaves_it_active_and_the_slot_is_offline_once_it_ends(pool_rig):
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert console.detach(svc, SLOT) == console.DRAINING

    with pool_rig.store() as store:
        assert [lease.id for lease in store.list_leases(state="active")] == [grant.lease_id]
    assert prompt(grant, "still mine").exit_code == 0
    assert slot_state(pool_rig) == console.DRAINING
    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    svc.end_lease(grant.lease_id, "released")

    # the row of the drained slot may stay; it reads offline and serves no one
    assert slot_state(pool_rig) == console.OFFLINE
    assert same_process(pool_rig, before)
    with pytest.raises(service.NoFreeMember, match=f"'{SLOT}'.*console"):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    waits = asks(pool_rig, svc, "b")
    assert waits.attempt() is None

    # until a new attach, and the same host may be the one attached
    console.attach(svc, SLOT, AGENT)
    granted = waits.attempt()
    assert granted is not None and granted.agent == AGENT
    assert [(row.agent, row.draining) for row in console_rows(pool_rig)] == [(AGENT, False)]
    assert same_process(pool_rig, before)


# -- a host that dies ---------------------------------------------------------


def test_a_console_whose_host_died_reads_offline_and_its_request_waits(pool_rig):
    svc, before = attached(pool_rig)
    assert slot_state(pool_rig) == console.ATTACHED

    kill_host(before)

    rows = console_rows(pool_rig)
    assert [(row.agent, row.draining) for row in rows] == [(AGENT, False)]
    assert console.state(SLOT, rows, [], {AGENT: None}) == console.OFFLINE
    assert slot_state(pool_rig) == console.OFFLINE
    assert console.unmatched(svc.config, rows, {AGENT: None}) == frozenset({SLOT})
    # not a member that did not start: the request waits, as for an attach
    with pytest.raises(service.NoFreeMember, match=f"'{SLOT}'.*console"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert asks(pool_rig, svc, "a").attempt() is None
    with pool_rig.store() as store:
        assert [row.pool for row in store.list_waiting()] == ["rebasers"]
        assert store.list_leases() == []


def test_the_rule_reads_a_host_with_a_status_as_alive_and_no_statuses_as_not_asked():
    rows = [ConsoleAttachment(slot=SLOT, agent=AGENT, attached="2026-03-01T12:00:00.000+00:00")]

    assert console.state(SLOT, rows, [], {AGENT: {"state": "idle"}}) == console.ATTACHED
    assert console.state(SLOT, rows, []) == console.ATTACHED
    # a host no one has a status of is not one to lease
    assert console.state(SLOT, rows, [], {}) == console.OFFLINE


def test_a_request_to_a_daemon_whose_console_died_is_refused_as_full(
    console_daemon, pool_rig  # noqa: F811
):
    before = start_host(pool_rig)
    assert cli.invoke(app, ["console", "attach", SLOT, AGENT]).exit_code == 0
    kill_host(before)

    refused = cli.invoke(app, ["lease", "request", "rebasers", "--wait", "0"])

    assert refused.exit_code != 0
    assert SLOT in refused.output
    assert "502" not in refused.output and "did not take the lease" not in refused.output
    with pool_rig.store() as store:
        assert store.list_leases() == []


def test_a_lease_out_on_a_host_that_died_expires_at_its_idle_limit(pool_rig):
    real_time(pool_rig)
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    kill_host(before)

    assert svc.expire_due() == []
    pool_rig.clock.advance(minutes=11)
    ended = svc.expire_due()

    assert [(e.lease_id, e.state) for e in ended] == [(grant.lease_id, "expired")]
    gone = prompt(grant, "still there?")
    assert gone.exit_code == EXIT_UNREACHABLE
    assert "lease expired" in gone.stderr
    assert slot_state(pool_rig) == console.OFFLINE


# -- attaching again ----------------------------------------------------------


def test_a_host_takes_the_token_it_is_bound_to_again_and_no_other(pool_rig):
    start_host(pool_rig)
    with connect(AGENT) as host:
        host.pool_bind(POOL_TOKEN)
        host.lease_bind(POOL_TOKEN, "token-of-the-holder")

        host.pool_bind(POOL_TOKEN)

        # nothing changed: the lease is still bound, and no other pool is let in
        assert not host.request({"type": "get_state", "lease_token": "another"})["success"]
        with pytest.raises(RuntimeError, match="already bound"):
            host.pool_bind("another-pool")
        with pytest.raises(RuntimeError, match="already bound"):
            host.pool_bind("")


def test_the_same_host_is_attached_again_after_a_detach(pool_rig):
    svc, before = attached(pool_rig)
    assert console.detach(svc, SLOT) == console.OFFLINE

    again = console.attach(svc, SLOT, AGENT)

    assert console_rows(pool_rig) == [again]
    assert (again.agent, again.draining) == (AGENT, False)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert grant.agent == AGENT
    assert same_process(pool_rig, before)


def test_an_agent_that_serves_another_slot_is_refused(pool_rig):
    other = Member(
        name="desk-2", command="", backing="local", labels=(), capacity=1, egress="local",
        model="tc3", kind="console",
    )
    svc, _ = attached(pool_rig, console_member(), other)

    with pytest.raises(console.ConsoleRefused, match=f"attached to console '{SLOT}'"):
        console.attach(svc, "desk-2", AGENT)
    # and while that slot drains with its lease out
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    console.detach(svc, SLOT)
    with pytest.raises(console.ConsoleRefused, match=f"attached to console '{SLOT}'"):
        console.attach(svc, "desk-2", AGENT)

    assert [row.slot for row in console_rows(pool_rig)] == [SLOT]


def test_an_agent_that_left_a_drained_slot_is_attached_to_another(pool_rig):
    other = Member(
        name="desk-2", command="", backing="local", labels=(), capacity=1, egress="local",
        model="tc3", kind="console",
    )
    svc, _ = attached(pool_rig, console_member(), other)
    console.detach(svc, SLOT)

    console.attach(svc, "desk-2", AGENT)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).member == "desk-2"


def test_the_agent_of_a_leased_member_the_pool_started_is_refused(pool_rig):
    local = pool_rig.local_member()
    svc = pool_rig.service(pool_rig.config(console_member(), local))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert grant.agent == local.name

    with pytest.raises(console.ConsoleRefused, match="a lease of the pool runs on"):
        console.attach(svc, SLOT, local.name)

    assert console_rows(pool_rig) == []
    with pool_rig.store() as store:
        assert ledger.agent_of(store.get_lease(grant.lease_id)) == local.name
    assert prompt(grant, "still mine").exit_code == 0


# -- a lease ends as its grant wrote it ---------------------------------------


def lease_on(member: str, pool_started: bool | None) -> Lease:
    """An active lease on *member*, with what its grant wrote of who started
    the process; None is a row from before that was written."""
    return Lease(
        id="lease-1", team_lease_id=None, holder_hash="0" * 64, holder_label="a",
        member=member, pool="rebasers",
        resources=(Resource("pool", "rebasers"), Resource("member", member)),
        acquired="2026-03-01T12:00:00.000+00:00", last_activity="2026-03-01T12:00:00.000+00:00",
        idle_limit=600, state="active", pool_started=pool_started,
    )


def test_a_grant_on_a_console_writes_that_the_pool_started_nothing(pool_rig):
    svc, _ = attached(pool_rig)

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.get_lease(grant.lease_id).pool_started is False
    svc.end_lease(grant.lease_id, "released")


def test_what_the_grant_wrote_decides_the_ending_whatever_is_declared_at_the_end(pool_rig):
    local = pool_rig.local_member()
    declared = pool_rig.config(local)
    gone = pool_rig.config(pool_rig.hosted_member())
    as_console = pool_rig.config(console_member(), pool_rig.hosted_member())
    started_under_the_slot = pool_rig.config(Member(
        name=SLOT, command=pool_rig.command, backing="local", labels=(), capacity=1,
        egress="local", model="tc3",
    ))
    slot_as_console = pool_rig.config(Member(
        name=local.name, command="", backing="local", labels=(), capacity=1,
        egress="local", model="tc3", kind="console",
    ))

    # a process the pool started is stopped, also on a member that is declared
    # no more, or as a console now
    for config in (declared, gone, slot_as_console):
        assert not console.leased(lease_on(local.name, True), config, [])
    # a console's process is never stopped, also when a member the pool starts
    # stands under the slot's name now
    for config in (as_console, gone, started_under_the_slot):
        assert console.leased(lease_on(SLOT, False), config, [])
    # a row from before that was written ends by the configuration, as it did
    before = lease_on(local.name, None)
    assert not console.leased(before, declared, [])
    assert console.leased(before, gone, [])
    assert console.leased(before, slot_as_console, [])


def test_an_attachment_row_that_names_the_agent_stands_over_what_the_grant_wrote(pool_rig):
    local = pool_rig.local_member()
    row = ConsoleAttachment(slot=SLOT, agent=local.name, attached="2026-03-01T12:00:00.000+00:00")

    # a developer's host under that name knows the pool's token from its attach
    assert console.leased(lease_on(local.name, True), pool_rig.config(local), [row])
    detached = dataclasses.replace(row, draining=True)
    assert console.leased(lease_on(local.name, True), pool_rig.config(local), [detached])


def test_a_console_lease_whose_row_is_gone_still_stops_nothing(pool_rig):
    svc, before = attached(pool_rig)
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    # the attachment row is gone, and the daemon is started again on a roster
    # that starts a member under the lease's member name
    with pool_rig.store() as store:
        assert store.detach_console(SLOT)
    renamed = Member(
        name=SLOT, command=pool_rig.command, backing="local", labels=(), capacity=1,
        egress="local", model="tc3",
    )
    again = pool_rig.service(pool_rig.config(renamed))

    ended = again.end_lease(grant.lease_id, "released")

    assert ended.state == "released"
    assert same_process(pool_rig, before)
    root = AgentPaths.resolve(AGENT).root
    assert agent_lease.read_ended(root) is None
    assert agent_lease.read_ended(root, grant.token)["cause"] == "released"
