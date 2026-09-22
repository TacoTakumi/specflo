"""A developer's console: a declared slot that serves a lease only while attached.

A console is a roster entry of kind console. The pool never starts its
process, so the entry has no command, and a console is one running agent, so
its capacity is 1. Until a developer attaches a running agent host to the
slot, nothing is matched to it: a request for a pool that only the slot
serves waits, and an attach is what ends the wait. ``specflo console attach
<slot> <agent>`` asks the daemon, as the developer and as no one else, to
bind its pool token on the agent host of that name on the daemon's host. The
host must run on the rpc transport: a TUI agent has no host to raise a lease
wall on. ``specflo console detach <slot>`` makes the slot take no new lease.

The console's agent host is a real one, the stub pi under ``specflo agent
start``, which no pool started. The daemon and the checkout are the request
verb's.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace

import pytest
import yaml
from typer.testing import CliRunner

from specflo import config as checkout_config
from specflo.agent.client import connect
from specflo.agent.statefiles import AgentPaths
from specflo.cli import app
from specflo.daemon import auth
from specflo.daemon.poolstore import ConsoleAttachment
from specflo.pool import cli_admin, console, ledger, service, waiting
from specflo.pool import config as pool_config
from specflo.pool.config import Member
from specflo.service.pool_remote import RemotePool

from . import test_lease_request
from .test_config_roster import HOSTED, LOCAL, _changed, _refused, _write
from .test_lease_request import FIXTURES, REBASER, audit_records
from .test_lease_request import checkout, pool_daemon  # noqa: F401  (fixtures)
from .test_runner import pid_alive, wait_until

runner = CliRunner()

SLOT = "desk-1"
AGENT = "rob-pi"

CONSOLE = {
    "name": SLOT, "kind": "console", "backing": "local", "model": "model-a",
    "labels": ["code"], "capacity": 1, "egress": "local",
}


@pytest.fixture(autouse=True)
def short_interval(monkeypatch):
    """A waiting request is looked at again every few hundredths of a second."""
    monkeypatch.setattr(waiting, "POLL_INTERVAL", 0.05)


def console_member() -> Member:
    return Member(
        name=SLOT, command="", backing="local", labels=(), capacity=1, egress="local",
        model="tc3", kind="console",
    )


def start_host(rig, name: str = AGENT) -> dict:
    """A developer's own agent host under *name*, on the rpc transport; its status."""
    done = subprocess.run(
        [
            sys.executable, "-c", "import sys; from specflo.cli import main; sys.exit(main())",
            "agent", "start", name, "--cwd", str(rig.work), "--pi-cmd", rig.command,
            "--no-herdr",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return rig.status(name)


def tui_record(rig, name: str) -> None:
    """The discovery record of an agent on the TUI transport: no host stands behind it."""
    paths = AgentPaths.resolve(name).ensure()
    paths.status.write_text(json.dumps({
        "name": name, "state": "idle", "pid": os.getpid(),
        "transport": "tui", "ownership": "managed",
    }), encoding="utf-8")


def console_rows(rig) -> list:
    with rig.store() as store:
        return store.list_consoles()


def asks(rig, svc, label: str) -> waiting.Waiting:
    ids = iter([f"request-{label}"])
    return waiting.Waiting(
        svc, "rebasers", holder_label=label, cwd=rig.work, wait=3600,
        mint_id=lambda: next(ids),
    )


# -- the roster entry ---------------------------------------------------------


def test_a_console_entry_with_capacity_1_and_no_command_loads(tmp_path):
    path = _write(tmp_path, members=[LOCAL, CONSOLE])

    started, desk = pool_config.load_pool_file(path).members

    assert (desk.name, desk.kind, desk.capacity, desk.command) == (SLOT, "console", 1, "")
    assert (desk.backing, desk.model, desk.egress) == ("local", "model-a", "local")
    # an entry that names no kind is one the pool starts, as every entry was
    assert started.kind == "started"


def test_a_hosted_console_names_its_account(tmp_path):
    entry = _changed(HOSTED, kind="console", command=None, capacity=1)

    (desk,) = pool_config.load_pool_file(_write(tmp_path, members=[entry])).members

    assert (desk.kind, desk.account, desk.egress) == ("console", "openrouter-main", "no-train")


def test_a_console_with_a_start_command_is_refused(tmp_path):
    path = _write(tmp_path, members=[_changed(CONSOLE, command="pi --mode rpc")])

    error = _refused(path)

    assert (error.entry, error.field) == (f"member '{SLOT}'", "command")
    assert "never starts" in str(error)


def test_a_console_of_capacity_2_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(CONSOLE, capacity=2)]))

    assert (error.entry, error.field) == (f"member '{SLOT}'", "capacity")
    assert "1" in str(error)


def test_an_unknown_kind_is_refused(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, kind="desk")]))

    assert (error.entry, error.field) == ("member 'coder-a'", "kind")
    assert "console" in str(error)


def test_a_member_the_pool_starts_still_needs_its_command(tmp_path):
    error = _refused(_write(tmp_path, members=[_changed(LOCAL, kind="started", command=None)]))

    assert (error.entry, error.field) == ("member 'coder-a'", "command")


# -- the slot's state, from the rows alone ------------------------------------


def test_a_slot_reads_offline_attached_or_draining_from_its_row_and_the_leases(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))
    start_host(pool_rig)
    assert console.state(SLOT, console_rows(pool_rig), []) == console.OFFLINE

    console.attach(svc, SLOT, AGENT)
    assert console.state(SLOT, console_rows(pool_rig), []) == console.ATTACHED

    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    console.detach(svc, SLOT)
    with pool_rig.store() as store:
        out = store.list_leases(state="active")
    # it lets the lease that is out end, and with none out it is offline
    assert console.state(SLOT, console_rows(pool_rig), out) == console.DRAINING
    assert console.state(SLOT, console_rows(pool_rig), []) == console.OFFLINE


def test_a_row_for_a_slot_that_is_no_longer_declared_is_ignored(pool_rig):
    start_host(pool_rig)
    console.attach(pool_rig.service(pool_rig.config(console_member())), SLOT, AGENT)
    local = pool_rig.local_member()
    config = pool_rig.config(local)

    assert console.unmatched(config, console_rows(pool_rig)) == frozenset()
    assert pool_rig.service(config).grant("rebasers", holder_label="a", cwd=pool_rig.work)


# -- a request waits for an attach --------------------------------------------


def test_a_request_to_a_pool_served_only_by_an_unattached_console_waits(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))

    # not refused for good, as a pool with no member it may have is: it waits
    with pytest.raises(service.NoFreeMember, match=f"'{SLOT}'.*console"):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert asks(pool_rig, svc, "a").attempt() is None

    with pool_rig.store() as store:
        assert [row.pool for row in store.list_waiting()] == ["rebasers"]
        assert store.list_leases() == []
    assert not AgentPaths.resolve(SLOT).status.exists()


def test_the_ledger_passes_over_the_members_it_is_told_not_to_match(pool_rig):
    local, desk = pool_rig.local_member(), console_member()
    config = pool_rig.config(desk, local)
    request = ledger.Request(pool="rebasers")

    assert ledger.place(config, [], request, unmatched={SLOT}).member == local
    assert ledger.place(config, [], request).member == desk
    with pytest.raises(ledger.NoRoom, match=SLOT):
        ledger.place(pool_rig.config(desk), [], request, unmatched={SLOT})


def test_after_an_attach_the_waiting_request_is_granted_on_the_attached_host(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))
    waits = asks(pool_rig, svc, "a")
    assert waits.attempt() is None
    before = start_host(pool_rig)

    attached = console.attach(svc, SLOT, AGENT)
    grant = waits.attempt()

    assert (attached.slot, attached.agent, attached.draining) == (SLOT, AGENT, False)
    assert console_rows(pool_rig) == [attached]
    # the agent to drive is the developer's own, and the lease's process is read by its name
    assert grant.agent == AGENT
    with pool_rig.store() as store:
        (row,) = store.list_leases(state="active")
        assert store.list_waiting() == []
    assert (row.member, ledger.agent_of(row)) == (SLOT, AGENT)
    # nothing was started: the same host and the same pi, and no agent under the slot's name
    after = pool_rig.status(AGENT)
    assert (after["host_pid"], after["pi_pid"]) == (before["host_pid"], before["pi_pid"])
    assert pid_alive(after["pi_pid"])
    assert not AgentPaths.resolve(SLOT).status.exists()
    assert pool_rig.pane_names() == []
    # the wall is up for the token the grant returned, and for no other
    with connect(AGENT) as client:
        assert client.request({"type": "status", "lease_token": grant.token})["success"]
        assert not client.request({"type": "status", "lease_token": "token-9"})["success"]


def test_an_attached_console_serves_one_lease_at_a_time(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))
    start_host(pool_rig)
    console.attach(svc, SLOT, AGENT)
    svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)


# -- what is not attachable ---------------------------------------------------


def test_an_undeclared_slot_and_a_member_the_pool_starts_are_refused(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member(), pool_rig.local_member()))
    start_host(pool_rig)

    with pytest.raises(console.ConsoleRefused, match="'desk-9' is not a declared console"):
        console.attach(svc, "desk-9", AGENT)
    with pytest.raises(console.ConsoleRefused, match="'local-1' is not a declared console"):
        console.attach(svc, "local-1", AGENT)
    with pytest.raises(console.ConsoleRefused, match="'desk-9' is not a declared console"):
        console.detach(svc, "desk-9")

    assert console_rows(pool_rig) == []


@pytest.mark.parametrize("agent", ["local-1", "local-1.2"])
def test_an_agent_under_a_name_the_pool_starts_hosts_under_is_refused(pool_rig, agent):
    # the pool starts a member's host under the member's name, and a further
    # lease's under that name and a number: a host it found there at a grant
    # that knew its token would be stopped as its own
    svc = pool_rig.service(pool_rig.config(console_member(), pool_rig.local_member()))
    before = start_host(pool_rig, agent)

    with pytest.raises(console.ConsoleRefused, match="member 'local-1'"):
        console.attach(svc, SLOT, agent)

    assert console_rows(pool_rig) == []
    # the host was not bound: it knows no pool's token still
    with connect(agent) as client, pytest.raises(RuntimeError, match="pool_token"):
        client.lease_clear(svc.pool_token)
    assert pid_alive(before["host_pid"]) and pid_alive(before["pi_pid"])


def test_an_agent_named_like_a_further_lease_of_a_console_is_attachable(pool_rig):
    # a console is one agent under its developer's name for it: the pool
    # starts nothing under a console's name
    svc = pool_rig.service(pool_rig.config(console_member(), pool_rig.local_member()))
    start_host(pool_rig, f"{SLOT}.2")

    assert console.attach(svc, SLOT, f"{SLOT}.2").agent == f"{SLOT}.2"


def test_an_agent_that_does_not_run_is_refused(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))

    with pytest.raises(console.ConsoleRefused, match=f"'{AGENT}' is not running"):
        console.attach(svc, SLOT, AGENT)

    assert console_rows(pool_rig) == []


def test_an_agent_on_the_tui_transport_is_refused(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))
    tui_record(pool_rig, "rob-tui")

    with pytest.raises(console.ConsoleRefused, match="TUI transport"):
        console.attach(svc, SLOT, "rob-tui")

    assert console_rows(pool_rig) == []


def test_a_slot_that_is_attached_takes_no_second_agent(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))
    start_host(pool_rig)
    start_host(pool_rig, "other-pi")
    console.attach(svc, SLOT, AGENT)

    with pytest.raises(console.ConsoleRefused, match=f"attached to it already, '{AGENT}'"):
        console.attach(svc, SLOT, "other-pi")

    assert [row.agent for row in console_rows(pool_rig)] == [AGENT]


# -- detach -------------------------------------------------------------------


def test_detach_marks_the_slot_draining_and_the_next_request_waits(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))
    before = start_host(pool_rig)
    console.attach(svc, SLOT, AGENT)

    assert console.detach(svc, SLOT) == console.OFFLINE

    (row,) = console_rows(pool_rig)
    assert (row.slot, row.agent, row.draining) == (SLOT, AGENT, True)
    with pytest.raises(service.NoFreeMember, match=SLOT):
        svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    assert asks(pool_rig, svc, "a").attempt() is None
    # the developer's process is the developer's: a detach stops nothing
    assert pid_alive(before["pi_pid"]) and pid_alive(before["host_pid"])


def test_a_slot_with_nothing_attached_is_not_detached(pool_rig):
    svc = pool_rig.service(pool_rig.config(console_member()))

    with pytest.raises(console.ConsoleRefused, match="nothing is attached"):
        console.detach(svc, SLOT)


# -- a member declared later under the attached agent's name --------------------


def with_a_member_named(rig, name: str):
    """The console's configuration, and beside the slot a pool "workers" of one
    member the pool starts under *name*."""
    first = rig.config(console_member())
    started = Member(
        name=name, command=rig.command, backing="local", labels=(), capacity=1,
        egress="local", model="tc3",
    )
    workers = replace(first.pools[0], name="workers", members=(name,))
    return replace(first, members=(*first.members, started), pools=(*first.pools, workers))


def test_the_rows_whose_agent_a_configuration_starts_a_member_under_are_told(pool_rig):
    rows = [
        ConsoleAttachment(slot=SLOT, agent=AGENT, attached="2026-01-01T00:00:00.000+00:00"),
        ConsoleAttachment(
            slot="desk-2", agent="other-pi.2", attached="2026-01-01T00:00:00.000+00:00",
            draining=True,
        ),
        ConsoleAttachment(slot="desk-3", agent="free-pi", attached="2026-01-01T00:00:00.000+00:00"),
    ]

    assert console.started_under(pool_rig.config(console_member()), rows) == []
    # the member's own name, and that of a further lease on it; a slot that
    # was detached counts, because its host knows the pool's token still
    (own,) = console.started_under(with_a_member_named(pool_rig, AGENT), rows)
    assert (own[0].name, own[1].slot) == (AGENT, SLOT)
    (further,) = console.started_under(with_a_member_named(pool_rig, "other-pi"), rows)
    assert (further[0].name, further[1].agent) == ("other-pi", "other-pi.2")


@pytest.mark.parametrize("detached", [False, True])
def test_a_grant_on_a_member_under_the_attached_agents_name_leaves_the_host_running(
    pool_rig, detached
):
    svc = pool_rig.service(pool_rig.config(console_member()))
    before = start_host(pool_rig)
    console.attach(svc, SLOT, AGENT)
    if detached:
        console.detach(svc, SLOT)
    # a daemon that starts again reads its directory with no reload to refuse
    # it: the member is declared under the name of the host that was attached
    later = pool_rig.service(with_a_member_named(pool_rig, AGENT))

    with pytest.raises(service.runner.RunnerError, match=f"agent '{AGENT}' already runs"):
        later.grant("workers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert store.list_leases(state=ledger.ACTIVE) == []
    time.sleep(0.5)  # a stop that was sent would have landed by now
    after = pool_rig.status(AGENT)
    assert (after["host_pid"], after["pi_pid"]) == (before["host_pid"], before["pi_pid"])
    assert pid_alive(before["host_pid"]) and pid_alive(before["pi_pid"])
    with connect(AGENT) as client:
        assert client.status()["status"]["state"] == "idle"


# -- the verbs, asked of a daemon ---------------------------------------------


def write_console_pool(rig) -> None:
    """A pool directory under the rig's daemon root: one pool, "rebasers",
    served by the console slot alone."""
    directory = cli_admin.pool_dir(rig.root)
    folder = directory / pool_config.DEFINITIONS_DIR
    folder.mkdir(parents=True)
    (folder / "rebaser.md").write_text(REBASER, encoding="utf-8")
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    data = {
        "llama_swap": "llama-swap.yaml",
        "models_file": str(rig.models_file),
        "members": [_changed(CONSOLE, labels=[])],
        "pools": [{
            "name": "rebasers", "definition": "rebaser", "members": [SLOT],
            "size": 1, "idle_default": "10m", "idle_max": "4h",
        }],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def console_daemon(monkeypatch, request):
    """The request verb's daemon and checkout, on a pool that a console serves."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_console_pool)
    served = request.getfixturevalue("pool_daemon")
    request.getfixturevalue("checkout")
    return served


def register_as(identity: str, served: dict) -> str:
    """Register the daemon once more, under a token of *identity*; the remote's name."""
    token = auth.mint_token(served["root"], identity)
    added = runner.invoke(app, ["remote", "add", identity, served["url"], "--token", token])
    assert added.exit_code == 0, added.output
    return identity


def test_the_pool_directory_with_a_console_validates(console_daemon):
    _, faults = pool_config.load_pool_config(cli_admin.pool_dir(console_daemon["root"]))

    assert faults == []


def test_attach_as_the_developer_binds_the_host_and_the_waiting_request_is_granted(
    console_daemon, pool_rig
):
    before = start_host(pool_rig)
    client = RemotePool(console_daemon["url"], console_daemon["token"], timeout=60)
    granted = []
    asked = threading.Thread(
        target=lambda: granted.append(client.request("rebasers", cwd=str(pool_rig.work), wait=30))
    )
    asked.start()
    try:
        def waits() -> bool:
            with pool_rig.store() as store:
                return bool(store.list_waiting())

        assert wait_until(waits), "the request did not wait"

        result = runner.invoke(app, ["console", "attach", SLOT, AGENT])
    finally:
        asked.join(timeout=30)

    assert result.exit_code == 0, result.output
    assert SLOT in result.stdout and AGENT in result.stdout
    (grant,) = granted
    assert grant.agent == AGENT
    assert pool_rig.status(AGENT)["pi_pid"] == before["pi_pid"]
    # the pool's token is bound on the host: it takes no other pool's
    with connect(AGENT) as host:
        with pytest.raises(RuntimeError, match="already bound"):
            host.pool_bind("another-pool")
    records = [r for r in audit_records(console_daemon["root"]) if r["id"] == SLOT]
    assert [(r["identity"], r["operation"]) for r in records] == [("developer", "console_attach")]


def test_attach_prints_json_when_asked(console_daemon, pool_rig):
    start_host(pool_rig)

    result = runner.invoke(app, ["console", "attach", SLOT, AGENT, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "slot": SLOT, "agent": AGENT, "state": "attached", "remote": "home",
    }


@pytest.mark.parametrize("identity", ["requester", "agent"])
def test_attach_and_detach_under_another_identity_exit_non_zero(
    console_daemon, pool_rig, identity
):
    start_host(pool_rig)
    remote = register_as(identity, console_daemon)

    attach = runner.invoke(app, ["console", "attach", SLOT, AGENT, "--remote", remote])
    # two remotes are registered now, so the developer's is named too
    mine = runner.invoke(app, ["console", "attach", SLOT, AGENT, "--remote", "home"])
    assert mine.exit_code == 0, mine.output
    detach = runner.invoke(app, ["console", "detach", SLOT, "--remote", remote])

    for result in (attach, detach):
        assert result.exit_code != 0
        assert "developer" in " ".join(result.output.split())
        assert "Traceback" not in result.output
    # the attach that stands is the developer's, and it still stands
    assert [(row.agent, row.draining) for row in console_rows(pool_rig)] == [(AGENT, False)]
    operations = [r["operation"] for r in audit_records(console_daemon["root"])]
    assert operations == ["console_attach"]


def test_attach_to_an_undeclared_slot_exits_non_zero(console_daemon, pool_rig):
    start_host(pool_rig)

    result = runner.invoke(app, ["console", "attach", "desk-9", AGENT])

    assert result.exit_code != 0
    assert "'desk-9' is not a declared console" in " ".join(result.output.split())
    assert console_rows(pool_rig) == []
    assert audit_records(console_daemon["root"]) == []


def test_attach_of_a_tui_agent_exits_non_zero(console_daemon, pool_rig):
    tui_record(pool_rig, "rob-tui")

    result = runner.invoke(app, ["console", "attach", SLOT, "rob-tui"])

    assert result.exit_code != 0
    assert "TUI transport" in " ".join(result.output.split())
    assert console_rows(pool_rig) == []


def test_detach_makes_the_next_request_wait(console_daemon, pool_rig):
    start_host(pool_rig)
    assert runner.invoke(app, ["console", "attach", SLOT, AGENT]).exit_code == 0

    result = runner.invoke(app, ["console", "detach", SLOT, "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"slot": SLOT, "state": "offline", "remote": "home"}
    assert [(row.agent, row.draining) for row in console_rows(pool_rig)] == [(AGENT, True)]
    refused = runner.invoke(app, ["lease", "request", "rebasers", "--wait", "0"])
    assert refused.exit_code != 0
    assert SLOT in refused.output
    operations = [r["operation"] for r in audit_records(console_daemon["root"])]
    assert operations == ["console_attach", "console_detach"]


# -- local mode ---------------------------------------------------------------


@pytest.mark.parametrize("verb", (["console", "attach", SLOT, AGENT], ["console", "detach", SLOT]))
def test_a_console_verb_with_no_remote_says_pools_need_a_daemon(tmp_path, monkeypatch, verb):
    root = tmp_path / "checkout"
    root.mkdir()
    checkout_config.init_config(root)
    monkeypatch.chdir(root)

    result = runner.invoke(app, verb)

    assert result.exit_code != 0
    assert "Pools need a daemon" in " ".join(result.output.split())
