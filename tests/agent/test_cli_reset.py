"""`specflo agent reset`: the lease holder clears a member's conversation
context in place - the same pi process, started the same way, with nothing
left of what it was told."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo.agent import lease
from specflo.agent.cli import EXIT_BUSY, EXIT_GENERIC, EXIT_UNREACHABLE, agent_app
from specflo.agent.client import connect
from specflo.agent.host import PiHost
from specflo.agent.statefiles import ENV_STATE_DIR, AgentPaths, read_status

STUB = Path(__file__).parent / "stub_pi.py"

POOL = "pool-secret"
HOLDER = "holder-secret"
REPLY = "noted"
MARKER = "heron"
# the way the pool hands a member its definition's prompt
DEFINITION_ARGV = ["--append-system-prompt", "You review code. Be terse."]


def captured(path: Path) -> list[dict]:
    """Every frame the stub pi received on stdin."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def argv_of(host: PiHost) -> list[str]:
    """The argv of the live pi process, as the system reports it."""
    cmdline = Path(f"/proc/{host.proc.pid}/cmdline")
    if not cmdline.exists():
        return list(host.proc.args)  # no procfs on this platform
    return cmdline.read_bytes().decode().split("\0")[:-1]


def run(*args: str):
    return CliRunner().invoke(agent_app, list(args))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """In-process hosts whose pi remembers what it is told in a session."""
    base = tmp_path / "state"
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv(ENV_STATE_DIR, str(base))
    monkeypatch.delenv(lease.ENV_LEASE_TOKEN, raising=False)
    monkeypatch.chdir(work)
    hosts = []

    def make(name: str, leased: bool = True, **scenario):
        capture = tmp_path / f"capture-{name}.jsonl"
        scenario_file = tmp_path / f"scenario-{name}.json"
        scenario_file.write_text(
            json.dumps(
                {"reply": REPLY, "recall": True, "capture": str(capture), **scenario}
            ),
            encoding="utf-8",
        )
        host = (
            PiHost(
                name,
                [sys.executable, str(STUB), str(scenario_file), *DEFINITION_ARGV],
                cwd=tmp_path,
                base_dir=base,
            )
            .start()
            .serve()
        )
        hosts.append(host)
        if leased:
            # the way the pool daemon raises the wall at grant
            with connect(name, base_dir=base) as daemon:
                daemon.pool_bind(POOL)
                daemon.lease_bind(POOL, HOLDER)
        return host, capture

    yield make, base
    for host in hosts:
        host.close()


def tell_the_marker(name: str) -> None:
    told = run("prompt", name, f"remember the word {MARKER}", "--lease-token", HOLDER)
    assert told.exit_code == 0, told.stderr
    assert MARKER not in told.stdout  # nothing to recall yet


def ask(name: str) -> str:
    asked = run("prompt", name, "what was the word?", "--lease-token", HOLDER)
    assert asked.exit_code == 0, asked.stderr
    return asked.stdout


def test_holder_reset_clears_the_context_and_keeps_the_process(rig):
    make, _ = rig
    host, capture = make("r1")
    tell_the_marker("r1")
    assert MARKER in ask("r1")  # the member does recall within a session
    pi_pid = read_status(host.paths.status)["pi_pid"]
    argv = argv_of(host)

    result = run("reset", "r1", "--lease-token", HOLDER)

    assert result.exit_code == 0, result.stderr
    assert "r1" in result.stdout
    assert MARKER not in ask("r1")
    # in place: the same pi process, still started with its definition
    assert host.proc.poll() is None
    assert host.proc.pid == pi_pid
    snapshot = read_status(host.paths.status)
    assert snapshot["pi_pid"] == pi_pid
    assert snapshot["state"] == "idle"
    assert argv_of(host) == argv
    flag = argv.index("--append-system-prompt")
    assert argv[flag + 1] == DEFINITION_ARGV[1]
    # pi was sent its own reset command, and never the credential
    frames = captured(capture)
    assert [f["type"] for f in frames].count("new_session") == 1
    assert not any("lease_token" in f for f in frames)
    assert HOLDER not in host.paths.events.read_text()


def test_holder_token_is_found_the_way_every_verb_finds_it(rig, monkeypatch):
    make, _ = rig
    make("r2")
    tell_the_marker("r2")
    monkeypatch.setenv(lease.ENV_LEASE_TOKEN, HOLDER)
    assert run("reset", "r2").exit_code == 0
    assert MARKER not in ask("r2")


@pytest.mark.parametrize(
    "credentials",
    [[], ["--lease-token", "wrong"], ["--lease-token", POOL]],
    ids=["no-token", "wrong-token", "pool-token-as-lease-token"],
)
def test_reset_without_the_holder_token_is_refused(rig, credentials):
    make, _ = rig
    host, capture = make("r3")
    tell_the_marker("r3")
    pi_pid = read_status(host.paths.status)["pi_pid"]
    frames_before = captured(capture)
    events_before = host.paths.events.read_text()

    result = run("reset", "r3", *credentials)

    assert result.exit_code == EXIT_GENERIC
    assert "lease" in result.stderr
    assert MARKER not in result.output
    # pi received nothing, so the context is kept
    assert captured(capture) == frames_before
    assert host.paths.events.read_text() == events_before
    assert MARKER in ask("r3")
    assert read_status(host.paths.status)["pi_pid"] == pi_pid


def test_reset_on_an_unleased_host_needs_no_token(rig):
    make, _ = rig
    _, capture = make("r4", leased=False)
    assert run("prompt", "r4", f"remember the word {MARKER}").exit_code == 0
    result = run("reset", "r4")
    assert result.exit_code == 0, result.stderr
    asked = run("prompt", "r4", "what was the word?")
    assert asked.exit_code == 0 and MARKER not in asked.stdout
    assert not any("lease_token" in f for f in captured(capture))


def test_reset_of_a_working_agent_is_refused_as_busy(rig):
    make, _ = rig
    _, capture = make("r5", mode="never_settle")
    started = run("prompt", "r5", "go", "--no-wait", "--lease-token", HOLDER)
    assert started.exit_code == 0, started.stderr

    result = run("reset", "r5", "--lease-token", HOLDER)

    assert result.exit_code == EXIT_BUSY
    assert "busy" in result.stderr
    assert "new_session" not in [f["type"] for f in captured(capture)]


def test_a_reset_that_pi_cancels_is_an_error(rig):
    make, _ = rig
    make("r6", cancel_new_session=True)
    tell_the_marker("r6")

    result = run("reset", "r6", "--lease-token", HOLDER)

    assert result.exit_code == EXIT_GENERIC
    assert "cancelled" in result.stderr
    assert MARKER in ask("r6")


def test_reset_reports_why_the_lease_ended(rig):
    make, base = rig
    host, _ = make("r7")
    # the lease ends the way the pool ends it: the host stops, the cause lands
    with connect("r7", base_dir=base) as daemon:
        assert daemon.request({"type": "stop", "pool_token": POOL})["success"]
    assert host.wait_stopped(timeout=10)
    lease.write_ended(host.paths.root, "expired")

    result = run("reset", "r7", "--lease-token", HOLDER)

    assert result.exit_code == EXIT_UNREACHABLE
    assert "lease expired" in result.stderr


def test_reset_of_an_unknown_agent_is_plainly_unreachable(rig):
    _, base = rig
    AgentPaths.resolve("r8", base).ensure()
    result = run("reset", "r8")
    assert result.exit_code == EXIT_UNREACHABLE
    assert "lease" not in result.stderr
