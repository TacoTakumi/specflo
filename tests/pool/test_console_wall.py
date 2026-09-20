"""A console between leases, and a console whose host was started again.

A member the pool starts has no host once its lease has ended, so a former
holder finds nothing to drive. A console's host is the developer's and runs
on: between two leases no lease is bound on it, and with no lease bound the
host has no wall, which is how the developer drives the console with no token
at all. A former holder still has its token, though. So the host remembers
the tokens of the leases it has cleared, by their digests and only so many of
them, and turns away a frame that presents one, whatever is bound now. The
``specflo agent`` verb then reads the record kept for that token and tells
the former holder how its lease ended, exit 12; the developer's pi receives
nothing of it.

A developer may stop the attached host and start it again under the same
name, with no new attach. The slot's row still names the agent, and the new
host knows no pool. The next grant binds the pool's token on it again, after
the checks an attach makes, and then the lease. An agent that came back on
the TUI transport has no host to bind anything on: its slot is not matched,
and the request waits.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from specflo.agent import host as agent_host
from specflo.agent import lease as agent_lease
from specflo.agent.cli import EXIT_UNREACHABLE, agent_app
from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.daemon.poolstore import ConsoleAttachment
from specflo.pool import console, runner, service

from .test_console_attach import AGENT, SLOT, asks, console_member, console_rows, start_host
from .test_console_attach import short_interval, tui_record  # noqa: F401  (fixture)
from .test_console_lease import attached, same_process, slot_state
from .test_expiry import prompt, real_time
from .test_preempt import stamped
from .test_preempt import stamps  # noqa: F401  (fixture)
from .test_runner import POOL_TOKEN, STUB, pid_alive, wait_until

DEVELOPER = "the developer's own prompt"


def capturing(pool_rig) -> None:
    """The console's pi, started from now on, writes down every frame it receives."""
    pool_rig.scenario(reply="done", capture=str(pool_rig.capture))


def prompts(pool_rig) -> list[str]:
    """The text of every prompt the console's pi received."""
    if not pool_rig.capture.exists():
        return []
    frames = [json.loads(line) for line in pool_rig.capture.read_text().splitlines() if line]
    return [frame.get("message") for frame in frames if frame["type"] == "prompt"]


def developer_prompts(text: str = DEVELOPER):
    """The developer's verb: no token, as between the leases of a console."""
    return CliRunner().invoke(agent_app, ["prompt", AGENT, text])


def turned_away(pool_rig, svc, before, old, words: str) -> None:
    """The lease of *old* has ended and the console is not leased again: the
    former holder is told *words* and drives nothing, the developer keeps the
    console, and the next lease's token is let in."""
    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []

    refused = prompt(old, "still there?")

    assert refused.exit_code == EXIT_UNREACHABLE
    assert words in refused.stderr
    assert "done" not in refused.output
    assert prompts(pool_rig) == []
    assert same_process(pool_rig, before)
    # the developer presents no token, and the console is the developer's
    mine = developer_prompts()
    assert mine.exit_code == 0, mine.output
    assert prompts(pool_rig) == [DEVELOPER]
    # and once more under the developer's turn: the old token opens nothing
    assert prompt(old, "and now?").exit_code == EXIT_UNREACHABLE
    new = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)
    assert new.agent == AGENT
    assert prompt(new, "hello").exit_code == 0
    assert prompts(pool_rig) == [DEVELOPER, "hello"]
    assert same_process(pool_rig, before)


# -- a former holder, before the console is leased again -----------------------


def test_a_released_holder_is_turned_away_before_the_console_is_leased_again(pool_rig):
    capturing(pool_rig)
    svc, before = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    svc.end_lease(old.lease_id, "released")

    turned_away(pool_rig, svc, before, old, "lease released")


def test_an_expired_holder_is_turned_away_before_the_console_is_leased_again(pool_rig):
    capturing(pool_rig)
    real_time(pool_rig)
    svc, before = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=11)

    assert [ended.state for ended in svc.expire_due()] == ["expired"]

    turned_away(pool_rig, svc, before, old, "lease expired")


def test_a_preempted_holder_is_turned_away_before_the_console_is_leased_again(
    pool_rig, stamps, monkeypatch  # noqa: F811
):
    capturing(pool_rig)
    svc, before = attached(pool_rig, preempt_after=300)
    stamps[AGENT] = stamped("idle", 0)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    pool_rig.clock.advance(minutes=6)
    second = asks(pool_rig, svc, "b")
    assert second.attempt() is None

    # the request takes the idle lease, and its own does not come about
    with monkeypatch.context() as patched:
        def no_bind(name, **tokens):
            raise runner.RunnerError(f"console '{name}': its host did not take the lease")

        patched.setattr(runner, "bind_console", no_bind)
        with pytest.raises(runner.RunnerError):
            second.attempt()
    second.leave()

    with pool_rig.store() as store:
        assert store.get_lease(old.lease_id).state == "preempted"
    turned_away(pool_rig, svc, before, old, "lease preempted by request-b")


def test_a_token_that_never_held_the_console_is_not_refused_between_leases(pool_rig):
    capturing(pool_rig)
    svc, _ = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    svc.end_lease(old.lease_id, "released")

    # the host remembers the leases it cleared, and no other token
    stranger = CliRunner().invoke(
        agent_app, ["prompt", AGENT, "hello", "--lease-token", "never-held"]
    )

    assert stranger.exit_code == 0, stranger.output
    assert prompts(pool_rig) == ["hello"]


def test_a_connection_that_showed_the_old_token_gets_nothing_of_the_next_lease(pool_rig):
    svc, _ = attached(pool_rig)
    old = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    with connect(AGENT, lease_token=old.token) as former:
        assert former.request({"type": "get_last_assistant_text"})["success"]
        svc.end_lease(old.lease_id, "released")
        # shown again between the leases, where it is refused
        refused = former.request({"type": "get_last_assistant_text"})
        assert agent_lease.is_wall_refusal(refused.get("error"))
        new = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
        assert not former.request({"type": "get_last_assistant_text"})["success"]
        heard_before = drained(former)

        with connect(AGENT, lease_token=new.token) as holder:
            holder.send({"type": "prompt", "message": "the next holder's prompt"})
            holder.read_until(lambda f: f.get("type") == "agent_settled", timeout=10)
            # pi answers in order, and the host sends one event to every
            # connection before the next: the turn has gone out everywhere
            for _ in range(2):
                assert holder.request({"type": "get_last_assistant_text"})["success"]

        assert "the next holder's prompt" not in json.dumps(heard_before)
        assert drained(former) == []


# -- the event log, which outlives a lease --------------------------------------


def shown_log(grant):
    """The holder's own verb on the member's event log."""
    return CliRunner().invoke(agent_app, ["log", grant.agent, "--lease-token", grant.token])


def test_the_next_holder_of_a_started_member_reads_nothing_of_the_lease_before(pool_rig):
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    pool_rig.scenario(reply="the answer to the first holder")
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert prompt(first, "the first holder's prompt").exit_code == 0
    svc.end_lease(first.lease_id, "released")
    pool_rig.scenario(reply="the answer to the second holder")
    second = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert second.agent == first.agent and second.token != first.token
    assert prompt(second, "the second holder's prompt").exit_code == 0

    shown = shown_log(second)

    assert shown.exit_code == 0, shown.output
    assert "the first holder's prompt" not in shown.stdout
    assert "the answer to the first holder" not in shown.stdout
    assert "the second holder's prompt" in shown.stdout
    assert "the answer to the second holder" in shown.stdout


def test_a_holder_of_a_console_reads_nothing_from_before_its_lease(pool_rig):
    capturing(pool_rig)
    svc, before = attached(pool_rig)
    assert developer_prompts().exit_code == 0
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert prompt(first, "the first holder's prompt").exit_code == 0
    svc.end_lease(first.lease_id, "released")
    assert developer_prompts("the developer, between the leases").exit_code == 0
    second = svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)
    assert prompt(second, "the second holder's prompt").exit_code == 0

    shown = shown_log(second)

    assert shown.exit_code == 0, shown.output
    assert same_process(pool_rig, before)
    assert DEVELOPER not in shown.stdout
    assert "the first holder's prompt" not in shown.stdout
    assert "the developer, between the leases" not in shown.stdout
    events = [json.loads(line) for line in shown.stdout.splitlines()]
    forwarded = [e["message"] for e in events if e["type"] == "host_forward"]
    assert forwarded == ["the second holder's prompt"]
    # one answer, the one to this holder: the log holds four by now
    assert [e["type"] for e in events].count("agent_settled") == 1
    # the console is the developer's: with no lease bound its whole log is read
    svc.end_lease(second.lease_id, "released")
    mine = CliRunner().invoke(agent_app, ["log", AGENT])
    assert mine.exit_code == 0, mine.output
    assert DEVELOPER in mine.stdout and "the first holder's prompt" in mine.stdout


def drained(client) -> list[dict]:
    """What the host has sent this connection so far, its own answers apart:
    a status frame is answered on the same ordered stream, let in or not."""
    frames = []
    request_id = client.send({"type": "status"})
    while True:
        frame = client.read(timeout=5)
        if frame.get("type") == "response" and frame.get("id") == request_id:
            return frames
        frames.append(frame)


# -- the host's memory of the leases it cleared --------------------------------


@pytest.fixture
def plain_host(tmp_path):
    """An agent host in this process, on the stub pi that writes down its frames."""
    capture = tmp_path / "capture-host.jsonl"
    scenario = tmp_path / "scenario-host.json"
    scenario.write_text(json.dumps({"reply": "ok", "capture": str(capture)}), encoding="utf-8")
    hosts = []

    def make() -> tuple[PiHost, object]:
        host = PiHost(
            "w1", [sys.executable, str(STUB), str(scenario)], cwd=tmp_path,
            base_dir=tmp_path / "state",
        ).start().serve()
        hosts.append(host)
        return host, capture

    yield make
    for host in hosts:
        host.close()


def received_types(capture) -> list[str]:
    if not capture.exists():
        return []
    return [json.loads(line)["type"] for line in capture.read_text().splitlines() if line]


def test_a_cleared_lease_token_is_refused_with_no_lease_bound_and_others_are_not(plain_host):
    host, capture = plain_host()
    base = host.paths.root.parent
    with connect("w1", base_dir=base) as daemon:
        daemon.pool_bind(POOL_TOKEN)
        daemon.lease_bind(POOL_TOKEN, "first-holder")
        daemon.lease_clear(POOL_TOKEN)

        for ctype in ("prompt", "get_last_assistant_text", "abort", "stop", "status"):
            refused = daemon.request({"type": ctype, "lease_token": "first-holder"})
            assert refused["success"] is False
            assert agent_lease.is_wall_refusal(refused["error"])
        assert received_types(capture) == []
        assert host.proc.poll() is None
        # the bare probe, a verb with no token to present, and a frame with none
        assert daemon.status()["status"]["state"] == "idle"
        assert daemon.request({"type": "status", "lease_token": ""})["success"]
        assert daemon.request({"type": "get_last_assistant_text"})["success"]
        # under the next lease the old token is refused as any wrong one is
        daemon.lease_bind(POOL_TOKEN, "second-holder")
        assert not daemon.request({"type": "abort", "lease_token": "first-holder"})["success"]
        assert daemon.request({"type": "abort", "lease_token": "second-holder"})["success"]
    assert received_types(capture) == ["get_last_assistant_text", "abort"]


def test_a_token_that_is_bound_again_is_let_in_again(plain_host):
    host, _ = plain_host()
    with connect("w1", base_dir=host.paths.root.parent) as daemon:
        daemon.pool_bind(POOL_TOKEN)
        daemon.lease_bind(POOL_TOKEN, "holder")
        daemon.lease_clear(POOL_TOKEN)
        # clearing twice is harmless, and remembers nothing twice
        daemon.lease_clear(POOL_TOKEN)

        daemon.lease_bind(POOL_TOKEN, "holder")

        assert daemon.request({"type": "abort", "lease_token": "holder"})["success"]
        daemon.lease_clear(POOL_TOKEN)
        assert not daemon.request({"type": "abort", "lease_token": "holder"})["success"]


def test_the_host_remembers_only_so_many_cleared_leases(plain_host, monkeypatch):
    monkeypatch.setattr(agent_host, "ENDED_REMEMBERED", 3)
    host, _ = plain_host()
    with connect("w1", base_dir=host.paths.root.parent) as daemon:
        daemon.pool_bind(POOL_TOKEN)
        for n in range(5):
            daemon.lease_bind(POOL_TOKEN, f"holder-{n}")
            daemon.lease_clear(POOL_TOKEN)

        let_in = [
            n for n in range(5)
            if daemon.request({"type": "abort", "lease_token": f"holder-{n}"})["success"]
        ]

    # the oldest are forgotten, the newest stay refused
    assert let_in == [0, 1]


# -- a host that was stopped and started again ---------------------------------


def stop_host(before: dict) -> None:
    """The developer stops the console's agent host, and its pi with it."""
    done = subprocess.run(
        [
            sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())",
            "agent", "stop", AGENT,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert wait_until(lambda: not pid_alive(before["host_pid"]))


def test_a_host_started_again_on_the_rpc_transport_is_bound_again_and_leased(pool_rig):
    svc, first = attached(pool_rig)
    stop_host(first)
    assert slot_state(pool_rig) == console.OFFLINE
    again = start_host(pool_rig)
    assert again["host_pid"] != first["host_pid"]
    assert slot_state(pool_rig) == console.ATTACHED

    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    assert grant.agent == AGENT
    assert same_process(pool_rig, again)
    assert prompt(grant, "hello").exit_code == 0
    with connect(AGENT) as fresh:
        # the pool's token is on the new host, and the wall is up for the grant alone
        assert not fresh.request({"type": "abort", "lease_token": "another"})["success"]
        with pytest.raises(RuntimeError, match="already bound"):
            fresh.pool_bind("another-pool")
    # and the lease ends as on any console
    svc.end_lease(grant.lease_id, "released")
    assert same_process(pool_rig, again)
    assert "lease released" in prompt(grant, "still there?").stderr


def test_a_waiting_request_is_granted_once_the_host_runs_again(pool_rig):
    svc, first = attached(pool_rig)
    stop_host(first)
    waits = asks(pool_rig, svc, "a")
    assert waits.attempt() is None

    again = start_host(pool_rig)
    granted = waits.attempt()

    assert granted is not None and granted.agent == AGENT
    assert same_process(pool_rig, again)


def test_a_host_started_again_under_another_pools_token_is_not_leased(pool_rig):
    svc, first = attached(pool_rig)
    stop_host(first)
    again = start_host(pool_rig)
    with connect(AGENT) as fresh:
        fresh.pool_bind("another-pool")

    with pytest.raises(runner.RunnerError, match=AGENT):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases(state="active") == []
    assert same_process(pool_rig, again)
    with connect(AGENT) as fresh:
        # no wall of this pool's went up on it
        assert fresh.request({"type": "get_last_assistant_text"})["success"]


def test_an_agent_that_came_back_on_the_tui_transport_is_not_matched(pool_rig):
    svc, first = attached(pool_rig)
    stop_host(first)
    tui_record(pool_rig, AGENT)

    with pytest.raises(service.NoFreeMember, match=f"'{SLOT}'.*console"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert asks(pool_rig, svc, "a").attempt() is None

    assert slot_state(pool_rig) == console.OFFLINE
    assert [(row.agent, row.draining) for row in console_rows(pool_rig)] == [(AGENT, False)]
    with pool_rig.store() as store:
        assert [row.pool for row in store.list_waiting()] == ["rebasers"]
        assert store.list_leases() == []


def test_the_rule_reads_a_status_record_of_the_tui_transport_as_no_host(pool_rig):
    rows = [ConsoleAttachment(slot=SLOT, agent=AGENT, attached="2026-03-01T12:00:00.000+00:00")]
    config = pool_rig.config(console_member())
    tui = {AGENT: {"state": "idle", "transport": "tui", "host_pid": 1}}
    rpc = {AGENT: {"state": "idle", "transport": "rpc", "host_pid": 1}}

    assert console.state(SLOT, rows, [], tui) == console.OFFLINE
    assert console.unmatched(config, rows, tui) == frozenset({SLOT})
    assert console.state(SLOT, rows, [], rpc) == console.ATTACHED
    assert console.unmatched(config, rows, rpc) == frozenset()
    # a record that names no transport is an rpc host's
    assert console.unmatched(config, rows, {AGENT: {"state": "idle"}}) == frozenset()


def test_a_lease_is_not_bound_on_an_agent_of_the_tui_transport(pool_rig):
    tui_record(pool_rig, AGENT)

    with pytest.raises(runner.RunnerError, match="TUI transport"):
        runner.bind_console(AGENT, pool_token=POOL_TOKEN, lease_token="token-1")


# -- the pool's own stop, where a former holder's token lies about -------------


def test_the_pool_stops_a_member_from_a_directory_that_holds_its_lease_token(
    pool_rig, monkeypatch
):
    # the daemon runs inside the holder's checkout, where the token file is kept
    svc = pool_rig.service(pool_rig.config(pool_rig.local_member()))
    grant = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    kept = agent_lease.token_file(pool_rig.work, grant.agent)
    kept.parent.mkdir(parents=True)
    kept.write_text(grant.token, encoding="utf-8")
    monkeypatch.chdir(pool_rig.work)
    monkeypatch.setenv(agent_lease.ENV_LEASE_TOKEN, grant.token)
    pi_pid = pool_rig.status(grant.agent)["pi_pid"]

    # the stop verb is the pool's, and presents no former holder's token
    ended = svc.end_lease(grant.lease_id, "released")

    assert ended.state == "released"
    assert wait_until(lambda: not pid_alive(pi_pid))
