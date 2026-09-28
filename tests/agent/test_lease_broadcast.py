"""A leased host's events go only to connections that showed a valid token.

The wall refuses an outsider's frames, but a member's output, pi's answers and
the holder's prompt text all travel in the broadcast stream. While a lease is
bound that stream reaches the holder's connections and the daemon's, no other.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo.agent import lease as lease_module
from specflo.agent.cli import agent_app
from specflo.agent.client import AgentClient, connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import ENV_STATE_DIR, read_status
from waits import settle, wait_until

STUB = Path(__file__).parent / "stub_pi.py"

POOL = "pool-secret"
HOLDER = "holder-secret"
PROMPT = "the holder's prompt text"
REPLY = "the member's reply"


@pytest.fixture
def make_host(tmp_path):
    hosts = []
    base = tmp_path / "state"

    def make(name: str, scenario: dict | None = None) -> PiHost:
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(
            json.dumps({"reply": REPLY, **(scenario or {})}), encoding="utf-8"
        )
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file)],
                cwd=tmp_path,
                base_dir=base,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        return host

    yield make, base
    for host in hosts:
        host.close()


def lease(name: str, base: Path) -> None:
    """Bind the pool and one lease the way the pool daemon does at grant."""
    with connect(name, base_dir=base) as daemon:
        daemon.pool_bind(POOL)
        daemon.lease_bind(POOL, HOLDER)


def run_turn(client: AgentClient, **credentials) -> list[dict]:
    """Drive one whole turn from this connection; every frame it received.

    Returns only once the host has finished sending the turn to every
    connection: pi answers in order and the host sends one event to all
    before it takes up the next, so when the second probe's answer is here
    the first one's - and all before it - has gone out everywhere.
    """
    seen = []
    client.send({"type": "prompt", "message": PROMPT, **credentials})
    while not any(f.get("type") == "agent_settled" for f in seen):
        seen.append(client.read(timeout=5))
    for _ in range(2):
        probe = client.request({"type": "get_last_assistant_text", **credentials})
        assert probe["success"] is True
    return seen


def received(client: AgentClient) -> list[dict]:
    """What the host has sent this connection so far.

    The bare status probe is open to everyone and answered on the same
    ordered stream, so every frame ahead of its answer was sent before it.
    """
    frames = []
    request_id = client.send({"type": "status"})
    while True:
        frame = client.read(timeout=5)
        if frame.get("type") == "response" and frame.get("id") == request_id:
            return frames
        frames.append(frame)


def types(frames: list[dict]) -> set[str]:
    return {f.get("type") for f in frames}


def carries_the_turn(frames: list[dict]) -> bool:
    """The prompt text, the member's output and the settle are all in there."""
    text = json.dumps(frames)
    return PROMPT in text and REPLY in text and "agent_settled" in types(frames)


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        {"lease_token": "wrong"},
        {"pool_token": "wrong"},
        {"lease_token": POOL},
        {"lease_token": ""},
    ],
    ids=[
        "silent",
        "wrong-lease-token",
        "wrong-pool-token",
        "pool-token-as-lease-token",
        "empty-lease-token",
    ],
)
def test_a_connection_without_a_valid_token_receives_no_event(make_host, credentials):
    make, base = make_host
    make("b1")
    lease("b1", base)
    with connect("b1", base_dir=base) as outsider:
        if credentials is not None:
            for ctype in ("status", "prompt", "get_last_assistant_text"):
                refused = outsider.request({"type": ctype, **credentials})
                assert refused["success"] is False
        with connect("b1", base_dir=base, lease_token=HOLDER) as holder:
            assert carries_the_turn(run_turn(holder))
        # no member output, no pi response, no forwarded prompt, no state line
        assert received(outsider) == []


def test_the_holder_and_the_daemon_both_receive_a_leased_members_events(make_host):
    make, base = make_host
    make("b2")
    lease("b2", base)
    with connect("b2", base_dir=base) as daemon, connect(
        "b2", base_dir=base, lease_token=HOLDER
    ) as watcher:
        # one frame with the credential is what opens the stream
        assert daemon.request({"type": "status", "pool_token": POOL})["success"]
        assert watcher.status()["status"]["state"] == "idle"
        with connect("b2", base_dir=base, lease_token=HOLDER) as holder:
            assert carries_the_turn(run_turn(holder))
        for listener in (daemon, watcher):
            frames = received(listener)
            assert carries_the_turn(frames)
            assert {"host_forward", "host_state", "response"} <= types(frames)


def test_the_connection_that_bound_the_lease_receives_its_events(make_host):
    make, base = make_host
    make("b3")
    with connect("b3", base_dir=base) as daemon:
        daemon.pool_bind(POOL)
        daemon.lease_bind(POOL, HOLDER)
        with connect("b3", base_dir=base, lease_token=HOLDER) as holder:
            run_turn(holder)
        assert carries_the_turn(received(daemon))


def test_the_daemon_driving_with_the_pool_token_reads_pis_answers(make_host):
    make, base = make_host
    make("b4")
    lease("b4", base)
    with connect("b4", base_dir=base) as daemon:
        assert carries_the_turn(run_turn(daemon, pool_token=POOL))


def test_a_connected_bystander_stops_receiving_when_the_lease_is_bound(make_host):
    make, base = make_host
    make("b5")
    with connect("b5", base_dir=base) as bystander:
        # no lease yet: it hears another connection's turn, as it always has
        with connect("b5", base_dir=base) as other:
            run_turn(other)
        assert carries_the_turn(received(bystander))

        lease("b5", base)
        with connect("b5", base_dir=base, lease_token=HOLDER) as holder:
            run_turn(holder)
        assert received(bystander) == []


def test_a_token_presented_late_opens_the_stream_from_then_on(make_host):
    make, base = make_host
    make("b6")
    lease("b6", base)
    with connect("b6", base_dir=base) as late, connect(
        "b6", base_dir=base, lease_token=HOLDER
    ) as holder:
        run_turn(holder)
        assert received(late) == []

        late.lease_token = HOLDER
        assert late.status()["status"]["state"] == "idle"
        run_turn(holder)
        frames = received(late)
        assert carries_the_turn(frames)
        # from then on, not from the start: one turn, the missed one not replayed
        assert [f.get("type") for f in frames].count("agent_settled") == 1


def test_a_refused_frame_does_not_close_a_stream_already_open(make_host):
    make, base = make_host
    make("b7")
    lease("b7", base)
    with connect("b7", base_dir=base, lease_token=HOLDER) as watcher:
        assert watcher.status()["status"]["state"] == "idle"
        assert watcher.request({"type": "status", "lease_token": "typo"})[
            "success"
        ] is False
        with connect("b7", base_dir=base, lease_token=HOLDER) as holder:
            run_turn(holder)
        assert carries_the_turn(received(watcher))


def test_lease_clear_reopens_the_stream_to_every_connection(make_host):
    make, base = make_host
    make("b8")
    lease("b8", base)
    with connect("b8", base_dir=base) as bystander:
        with connect("b8", base_dir=base, lease_token=HOLDER) as holder:
            run_turn(holder)
        assert received(bystander) == []

        with connect("b8", base_dir=base) as daemon:
            daemon.lease_clear(POOL)
        with connect("b8", base_dir=base) as other:
            run_turn(other)
        frames = received(bystander)
        assert carries_the_turn(frames)
        # what the lease hid stays hidden: one turn arrived, not two
        assert [f.get("type") for f in frames].count("agent_settled") == 1


def test_a_former_holder_connection_receives_nothing_under_the_next_lease(make_host):
    make, base = make_host
    make("b9")
    lease("b9", base)
    with connect("b9", base_dir=base, lease_token=HOLDER) as former, connect(
        "b9", base_dir=base
    ) as daemon:
        assert carries_the_turn(run_turn(former))
        daemon.lease_clear(POOL)
        daemon.lease_bind(POOL, "second-holder")
        # both were entitled to the first lease's events; drop those
        received(former)
        received(daemon)

        with connect("b9", base_dir=base, lease_token="second-holder") as holder:
            run_turn(holder)
        assert received(former) == []
        # the pool token is not a lease's: the daemon's connection stays open
        assert carries_the_turn(received(daemon))


@pytest.mark.parametrize("pool_bound", [False, True], ids=["plain", "pool-bound"])
def test_with_no_lease_bound_every_connection_receives(make_host, pool_bound):
    make, base = make_host
    make("b10")
    if pool_bound:
        with connect("b10", base_dir=base) as daemon:
            daemon.pool_bind(POOL)
    with connect("b10", base_dir=base) as silent, connect(
        "b10", base_dir=base, lease_token="stale-token"
    ) as stale:
        with connect("b10", base_dir=base) as other:
            run_turn(other)
        assert carries_the_turn(received(silent))
        assert carries_the_turn(received(stale))


# -- the holder's streaming verbs -------------------------------------------


@pytest.fixture
def cli_rig(tmp_path, monkeypatch, make_host):
    """The verbs, pointed at this test's hosts and away from any real token."""
    make, base = make_host
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(base))
    monkeypatch.delenv(lease_module.ENV_LEASE_TOKEN, raising=False)
    monkeypatch.chdir(work)
    return make, base


def test_the_holders_wait_still_streams_to_the_settle(cli_rig):
    make, base = cli_rig
    host = make("b11", {"mode": "never_settle"})
    lease("b11", base)
    with connect("b11", base_dir=base, lease_token=HOLDER) as holder:
        assert holder.request({"type": "prompt", "message": PROMPT})["success"]
    assert wait_until(lambda: read_status(host.paths.status)["state"] == "working")
    stamped = read_status(host.paths.status)["last_activity"]

    def end_the_turn_once_wait_listens() -> None:
        # wait opens with the holder's status frame, which moves the stamp;
        # the daemon's abort does not, and it settles the turn
        wait_until(
            lambda: read_status(host.paths.status)["last_activity"] != stamped,
            timeout=10,
            message="the holder's wait never moved the stamp",
        )
        settle(0.3)
        with connect("b11", base_dir=base) as daemon:
            daemon.request({"type": "abort", "pool_token": POOL})

    ender = threading.Thread(target=end_the_turn_once_wait_listens, daemon=True)
    ender.start()
    result = CliRunner().invoke(
        agent_app, ["wait", "b11", "--timeout", "10", "--lease-token", HOLDER]
    )
    ender.join(timeout=15)
    assert result.exit_code == 0, result.output

    # and an outsider's wait is still turned away at the wall
    assert CliRunner().invoke(agent_app, ["wait", "b11"]).exit_code != 0


def test_the_holders_log_follow_still_streams(cli_rig):
    make, base = cli_rig
    make("b12")
    lease("b12", base)
    follower = subprocess.Popen(
        [
            sys.executable, "-c",
            "import sys; from specflo.cli import main; sys.exit(main())",
            "agent", "log", "b12", "--follow", "--lease-token", HOLDER,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, ENV_STATE_DIR: str(base)},
    )
    try:
        lines: queue.Queue = queue.Queue()
        threading.Thread(
            target=lambda: [lines.put(line) for line in follower.stdout], daemon=True
        ).start()
        with connect("b12", base_dir=base, lease_token=HOLDER) as holder:
            run_turn(holder)
        seen = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                seen.append(lines.get(timeout=1))
            except queue.Empty:
                continue
            if '"agent_settled"' in seen[-1]:
                break
        assert any('"agent_settled"' in line for line in seen)
        assert any(REPLY in line for line in seen)
    finally:
        follower.kill()
        follower.wait(timeout=5)
