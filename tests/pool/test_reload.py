"""A changed pool configuration is put in force without a restart of the daemon.

An admin edits the pool directory by hand and then asks the running daemon to
read it again: ``specflo serve pool reload``, run as the lease verbs are, from
a checkout with the daemon registered under the developer's token. The daemon
checks the whole directory as ``pool validate`` does. A directory that stands
is put in force as one configuration, and a request made from then on is
served under it: a new team can be asked for, and a member started from then
on runs the prompt its definition has now. A lease that is out is not
touched: its pi was started with the old prompt and runs on with it.

A directory with faults changes nothing. Requests are still granted under the
last configuration that stood, and the faults are kept on the application
and told by the pool's status until a later reload passes. A daemon that was
started on a directory with faults has no pool at all, and the reload is
what brings it one, in the same process.

A lease that is out has to end as it began. A member the pool started has
its process stopped when its lease ends, and the pool knows such a member
only from the configuration, so a directory that declares the member of an
active lease no more, or as another kind, is refused whole, with the member
and the lease named. A pool or a team that is declared no more takes nothing
from the leases that are out on it: they are given back as ever.

The members are the stub pi behind a recorder, under a real agent host that
a fake herdr places, each with a record of its own.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from specflo.cli import app
from specflo.daemon import auth, pool_routes, web
from specflo.daemon.app import create_app
from specflo.daemon.poolstore import WaitingRequest
from specflo.pool import accounts, cli_admin, events, watch
from specflo.pool import config as pool_config
from specflo.pool import service as pool_service
from specflo.pool.console import attach
from specflo.pool.runner import RunnerError
from specflo.pool.service import PoolService, UnknownPool
from specflo.pool.teams import TEAMS_DIR, Role, Team
from specflo.service.pool_remote import (
    LEASES_PATH,
    RELOAD_PATH,
    STATUS_PATH,
    RemotePool,
    release_path,
)

from . import test_lease_request
from .test_console_attach import AGENT, SLOT, start_host
from .test_lease_request import FIXTURES, audit_records, checkout, runner  # noqa: F401
from .test_lease_request import pool_daemon as plain_daemon  # noqa: F401  (fixture)
from .test_runner import STUB, pid_alive, wait_until
from .test_team_lease import active

OLD_PROMPT = "You are the worker."
NEW_PROMPT = "You are the worker. Name every file you changed."

WORKER = """\
---
role: Does one plan task and reports what changed
tools: [read, bash]
egress: local
---

{prompt}
"""


# -- the pool directory, as an admin writes it ---------------------------------


def record_of(rig, member: str) -> Path:
    """Where the recorder writes what *member*'s pi was started with."""
    return rig.tmp_path / f"record-{member}.json"


def member(rig, name: str, model: str = "model-a", **fields) -> dict:
    """A local member that starts the rig's pi double behind a record of its own."""
    recorder = rig.tmp_path / "recorder.py"
    scenario = rig.tmp_path / "scenario.json"
    return {
        "name": name, "backing": "local", "model": model, "labels": [], "capacity": 1,
        "egress": "local",
        "command": f"{sys.executable} {recorder} {record_of(rig, name)} {STUB} {scenario}",
        **fields,
    }


def console(name: str) -> dict:
    return {
        "name": name, "kind": "console", "backing": "local", "model": "model-a",
        "labels": [], "capacity": 1, "egress": "local",
    }


def pool(name: str, *members: str) -> dict:
    return {
        "name": name, "definition": "worker", "members": list(members),
        "size": len(members), "idle_default": "10m", "idle_max": "4h",
    }


def write_directory(rig, *, members, pools, teams=None, prompt: str = OLD_PROMPT) -> Path:
    """The pool directory under the rig's daemon root, written whole: what was
    there before is the admin's last edit, and this is the next one."""
    directory = cli_admin.pool_dir(rig.root)
    folder = directory / pool_config.DEFINITIONS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "worker.md").write_text(WORKER.format(prompt=prompt), encoding="utf-8")
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")
    data = {"llama_swap": "llama-swap.yaml", "members": list(members), "pools": list(pools)}
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")
    shutil.rmtree(directory / TEAMS_DIR, ignore_errors=True)
    for name, roles in (teams or {}).items():
        (directory / TEAMS_DIR).mkdir(exist_ok=True)
        (directory / TEAMS_DIR / f"{name}.md").write_text(
            "---\n" + yaml.safe_dump({"roles": roles}) + "---\n\nThe orchestrator leads.\n",
            encoding="utf-8",
        )
    return directory


def write_workers(rig, **changed) -> Path:
    """One pool, "workers", of the two members "w-1" and "w-2"."""
    fields = {
        "members": [member(rig, "w-1"), member(rig, "w-2", model="model-c")],
        "pools": [pool("workers", "w-1", "w-2")],
        **changed,
    }
    return write_directory(rig, **fields)


PAIR = {"pair": [{"name": "doer", "pool": "workers", "count": 2}]}


def break_directory(rig) -> list[str]:
    """A hand edit that does not stand; the faults ``pool validate`` finds in it."""
    directory = write_workers(rig, pools=[{**pool("workers", "w-1", "w-2"), "size": 0}])
    _, faults = pool_config.load_pool_config(directory)
    assert faults
    return [str(fault) for fault in faults]


def started_with(rig, name: str) -> dict:
    """What the pi of the member *name* was started with."""
    record = record_of(rig, name)
    assert wait_until(record.is_file), f"the pi of {name} never started"
    assert wait_until(lambda: record.read_text(encoding="utf-8").endswith("}"))
    return json.loads(record.read_text(encoding="utf-8"))


def prompt_of(rig, name: str) -> str:
    argv = started_with(rig, name)["argv"]
    return argv[argv.index("--append-system-prompt") + 1]


# -- the daemon in this process, asked over its routes -------------------------


class Served:
    """The daemon on the rig's root: the application, and a client to each identity."""

    def __init__(self, rig) -> None:
        self.rig = rig
        self.application = create_app(rig.root)
        self.developer = self.client("developer")

    def client(self, identity: str) -> TestClient:
        client = TestClient(self.application)
        client.headers["Authorization"] = f"Bearer {auth.mint_token(self.rig.root, identity)}"
        return client

    @property
    def state(self):
        return self.application.state

    def reload(self):
        return self.developer.post(RELOAD_PATH, json={})

    def lease(self, **named):
        return self.developer.post(LEASES_PATH, json={"cwd": str(self.rig.work), **named})

    def granted(self, **named) -> dict:
        response = self.lease(**named)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def release(self, lease_id: str, token: str):
        return self.developer.post(release_path(lease_id), json={"token": token})


@pytest.fixture
def served(pool_rig):
    write_workers(pool_rig)
    return Served(pool_rig)


def test_a_reload_of_a_directory_that_stands_puts_it_in_force(served, pool_rig):
    before = served.state.pool
    write_workers(pool_rig, pools=[pool("workers", "w-1"), pool("others", "w-2")])

    response = served.reload()

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["pid"] == os.getpid()
    assert result["directory"] == str(cli_admin.pool_dir(pool_rig.root))
    assert (result["pools"], result["members"], result["teams"]) == (2, 2, 0)
    # the service is the one the daemon started with, under another configuration
    assert served.state.pool is before
    assert [p.name for p in before.config.pools] == ["workers", "others"]
    assert list(served.state.pool_errors) == []
    assert [p["name"] for p in served.developer.get(STATUS_PATH).json()["result"]] == [
        "workers", "others",
    ]


def test_a_lease_from_before_the_edit_runs_the_old_prompt_and_one_after_it_the_new(
    served, pool_rig
):
    first = served.granted(pool="workers")
    assert first["agent"] == "w-1"
    assert prompt_of(pool_rig, "w-1") == OLD_PROMPT
    pi_before = pool_rig.status("w-1")["pi_pid"]

    write_workers(pool_rig, prompt=NEW_PROMPT)
    assert served.reload().status_code == 200

    second = served.granted(pool="workers")
    assert second["agent"] == "w-2"
    assert prompt_of(pool_rig, "w-2") == NEW_PROMPT
    # the first lease's pi is the one that was started, with what it was started with
    assert pool_rig.status("w-1")["pi_pid"] == pi_before
    assert pid_alive(pi_before)
    assert prompt_of(pool_rig, "w-1") == OLD_PROMPT
    assert started_with(pool_rig, "w-1")["pid"] == pi_before


def test_without_a_reload_the_edit_is_not_in_force(served, pool_rig):
    write_workers(pool_rig, prompt=NEW_PROMPT)

    served.granted(pool="workers")

    assert prompt_of(pool_rig, "w-1") == OLD_PROMPT


def test_a_reload_is_audited_with_the_developer_identity(served, pool_rig):
    assert served.reload().status_code == 200

    (record,) = audit_records(pool_rig.root)
    assert (record["identity"], record["operation"]) == ("developer", "pool_reload")


@pytest.mark.parametrize("identity", ["requester", "agent"])
def test_only_the_developer_identity_reloads(served, pool_rig, identity):
    write_workers(pool_rig, pools=[pool("others", "w-1", "w-2")])

    response = served.client(identity).post(RELOAD_PATH, json={})

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert isinstance(detail, str) and "developer" in detail and identity in detail
    assert [p.name for p in served.state.pool.config.pools] == ["workers"]
    assert audit_records(pool_rig.root) == []


def test_a_reload_takes_no_field(served):
    response = served.developer.post(RELOAD_PATH, json={"root": "/somewhere/else"})

    assert response.status_code == 422


# -- a directory that does not stand -------------------------------------------


def test_a_reload_of_a_faulty_directory_changes_nothing_and_the_status_tells_the_faults(
    served, pool_rig
):
    in_force = served.state.pool.config
    faults = break_directory(pool_rig)

    response = served.reload()

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    for fault in faults:
        assert fault in detail
    assert "stays in force" in detail
    assert served.state.pool.config is in_force
    assert [str(fault) for fault in served.state.pool_errors] == faults
    assert audit_records(pool_rig.root) == []
    # requests are still granted, under the configuration that stood
    assert served.granted(pool="workers")["agent"] == "w-1"
    status = served.developer.get(STATUS_PATH)
    assert status.status_code == 200, status.text
    assert status.json()["result"] == [{"name": "workers", "size": 2, "in_use": 1}]
    assert status.json()["errors"] == faults


def test_a_later_reload_that_passes_clears_the_faults(served, pool_rig):
    break_directory(pool_rig)
    assert served.reload().status_code == 400

    write_workers(pool_rig, teams=PAIR)
    assert served.reload().status_code == 200

    assert list(served.state.pool_errors) == []
    assert served.developer.get(STATUS_PATH).json()["errors"] == []
    assert [team.name for team in served.state.pool.config.teams] == ["pair"]


def test_a_daemon_started_on_a_faulty_directory_gets_its_pool_from_a_reload(pool_rig):
    faults = break_directory(pool_rig)
    served = Served(pool_rig)
    assert served.state.pool is None

    # still faulty: the reload answers as every pool route does, with the faults
    refused = served.reload()
    assert refused.status_code == 400
    for fault in faults:
        assert fault in refused.json()["detail"]
    assert served.state.pool is None
    off = served.developer.get(STATUS_PATH).json()["detail"]
    assert "pool reload" in off and "start the daemon again" not in off

    write_workers(pool_rig)
    response = served.reload()

    assert response.status_code == 200, response.text
    assert response.json()["result"]["pid"] == os.getpid()
    assert isinstance(served.state.pool, PoolService)
    assert list(served.state.pool_errors) == []
    assert served.granted(pool="workers")["agent"] == "w-1"


def test_the_reload_is_one_function_that_answers_with_the_faults(served, pool_rig):
    faults = break_directory(pool_rig)

    assert [str(f) for f in pool_routes.reload_pool(served.application, "developer")] == faults

    write_workers(pool_rig)
    assert pool_routes.reload_pool(served.application, "developer") == ()
    assert [r["identity"] for r in audit_records(pool_rig.root)] == ["developer"]


# -- what the daemon reads while it serves -------------------------------------


class Reading:
    """The three readers the daemon starts, kept as they are made, with waits
    short enough for a test."""

    def __init__(self, monkeypatch) -> None:
        self.made: dict[str, object] = {}
        monkeypatch.setattr(events, "IDLE_INTERVAL", 0.01)
        monkeypatch.setattr(events, "FIRST_DELAY", 0.01)
        monkeypatch.setattr(events, "MAX_DELAY", 0.01)
        monkeypatch.setattr(watch, "POLL_INTERVAL", 0.01)
        monkeypatch.setattr(accounts, "IDLE_INTERVAL", 0.01)
        for module, name in ((events, "reader_for"), (watch, "watcher_for"),
                             (accounts, "reader_for")):
            monkeypatch.setattr(module, name, self._keeping(module.__name__, getattr(module, name)))

    def _keeping(self, key: str, make):
        def made(pool):
            self.made[key] = make(pool)
            return self.made[key]

        return made

    def follows(self, service) -> bool:
        """Whether each reader has taken the configuration in force on *service*."""
        stream, logs, keys = (self.made.get(m.__name__) for m in (events, watch, accounts))
        return (
            None not in (stream, logs, keys) and service is not None
            and stream.recorder.config is service.config
            and logs.config is service.config
            and keys.service is service
        )


def test_the_readers_follow_the_pool_a_reload_opens_and_each_configuration_after_it(
    pool_rig, monkeypatch
):
    reading = Reading(monkeypatch)
    break_directory(pool_rig)
    served = Served(pool_rig)

    with served.developer:
        write_workers(pool_rig)
        response = served.reload()
        assert response.status_code == 200, response.text
        assert response.json()["result"]["pid"] == os.getpid()
        opened = served.state.pool
        assert wait_until(lambda: reading.follows(opened)), "a reader kept no pool"

        write_workers(pool_rig, members=[member(pool_rig, "w-1"), member(pool_rig, "w-2"),
                                         member(pool_rig, "w-3")])
        assert served.reload().status_code == 200
        assert served.state.pool is opened
        assert [m.name for m in opened.config.members] == ["w-1", "w-2", "w-3"]
        assert wait_until(lambda: reading.follows(opened)), "a reader kept the old configuration"

        # a directory that does not stand changes no reader
        in_force = opened.config
        break_directory(pool_rig)
        assert served.reload().status_code == 400
        assert not wait_until(lambda: not reading.follows(opened), timeout=0.3)
        assert opened.config is in_force


# -- the leases that are out when the configuration changes --------------------


def test_a_directory_that_drops_the_member_of_an_active_lease_is_refused_whole(
    served, pool_rig
):
    lease = served.granted(pool="workers")
    pi = pool_rig.status("w-1")["pi_pid"]
    in_force = served.state.pool.config
    write_workers(
        pool_rig, members=[member(pool_rig, "w-2", model="model-c")],
        pools=[pool("workers", "w-2"), pool("others", "w-2")],
    )

    response = served.reload()

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "'w-1'" in detail and lease["lease_id"] in detail
    # nothing of the directory is in force, the part that could stand either
    assert served.state.pool.config is in_force
    assert served.lease(pool="others").status_code == 400
    assert len(served.state.pool_errors) == 1

    # its holder gives the lease back, its pi is stopped, and the reload passes
    assert served.release(lease["lease_id"], lease["token"]).status_code == 200
    assert wait_until(lambda: not pid_alive(pi))
    assert served.reload().status_code == 200
    assert [m.name for m in served.state.pool.config.members] == ["w-2"]
    assert list(served.state.pool_errors) == []


def test_a_directory_that_makes_a_console_of_a_leased_member_is_refused(served, pool_rig):
    lease = served.granted(pool="workers")
    pi = pool_rig.status("w-1")["pi_pid"]
    write_workers(pool_rig, members=[console("w-1"), member(pool_rig, "w-2", model="model-c")])

    response = served.reload()

    assert response.status_code == 400
    assert "'w-1'" in response.json()["detail"]
    assert lease["lease_id"] in response.json()["detail"]
    # so the lease still ends as the lease of a member the pool started
    assert served.release(lease["lease_id"], lease["token"]).status_code == 200
    assert wait_until(lambda: not pid_alive(pi))


# -- a member declared under the name of an attached console's agent -----------


def write_desk(rig, started: str | None = None) -> Path:
    """The workers and a console slot; with *started*, a member the pool
    starts under that name as well, in a pool "others"."""
    members = [member(rig, "w-1"), member(rig, "w-2", model="model-c"), console(SLOT)]
    pools = [pool("workers", "w-1", "w-2"), pool("desk", SLOT)]
    if started is not None:
        members.append(member(rig, started))
        pools.append(pool("others", started))
    return write_directory(rig, members=members, pools=pools)


def test_a_directory_that_starts_a_member_under_an_attached_agents_name_is_refused(pool_rig):
    write_desk(pool_rig)
    served = Served(pool_rig)
    before = start_host(pool_rig)
    attach(served.state.pool, SLOT, AGENT)
    in_force = served.state.pool.config
    write_desk(pool_rig, started=AGENT)

    response = served.reload()

    # at a grant the pool would find the developer's host under the member's
    # name, bound to its own token, and stop it as the member's
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert f"member '{AGENT}'" in detail and f"console '{SLOT}'" in detail
    assert "another name" in detail and "stays in force" in detail
    assert served.state.pool.config is in_force
    assert served.lease(pool="others").status_code == 400
    (fault,) = served.state.pool_errors
    assert served.developer.get(STATUS_PATH).json()["errors"] == [str(fault)]
    assert audit_records(pool_rig.root) == []
    after = pool_rig.status(AGENT)
    assert (after["host_pid"], after["pi_pid"]) == (before["host_pid"], before["pi_pid"])
    assert pid_alive(before["host_pid"]) and pid_alive(before["pi_pid"])

    # the member is declared under another name, and the reload passes
    write_desk(pool_rig, started="w-3")
    assert served.reload().status_code == 200
    assert list(served.state.pool_errors) == []
    assert served.granted(pool="others")["agent"] == "w-3"
    assert pid_alive(before["host_pid"]) and pid_alive(before["pi_pid"])


def test_a_daemon_started_on_such_a_directory_has_no_pool_until_the_name_is_changed(pool_rig):
    write_desk(pool_rig)
    start_host(pool_rig)
    attach(Served(pool_rig).state.pool, SLOT, AGENT)
    write_desk(pool_rig, started=AGENT)

    # no reload stands between this directory and the daemon that starts on it
    again = Served(pool_rig)

    assert again.state.pool is None
    (fault,) = again.state.pool_errors
    assert f"member '{AGENT}'" in str(fault) and f"console '{SLOT}'" in str(fault)
    assert str(fault) in again.lease(pool="others").json()["detail"]
    # nor does the reload that opens the pool pass it
    assert again.reload().status_code == 400
    assert again.state.pool is None

    write_desk(pool_rig, started="w-3")
    assert again.reload().status_code == 200
    assert again.granted(pool="others")["agent"] == "w-3"


def test_a_lease_on_a_pool_that_is_declared_no_more_is_given_back_as_ever(served, pool_rig):
    lease = served.granted(pool="workers")
    pi = pool_rig.status("w-1")["pi_pid"]
    write_workers(pool_rig, pools=[pool("others", "w-1", "w-2")])

    assert served.reload().status_code == 200

    # the member is one lease's, whichever pool it was leased from
    assert served.granted(pool="others")["agent"] == "w-2"
    assert served.lease(pool="others").status_code == 400
    assert served.developer.get(STATUS_PATH).json()["result"] == [
        {"name": "others", "size": 2, "in_use": 1},
    ]
    ended = served.release(lease["lease_id"], lease["token"])
    assert ended.status_code == 200, ended.text
    assert ended.json()["result"]["state"] == "released"
    assert wait_until(lambda: not pid_alive(pi))
    assert served.granted(pool="others")["agent"] == "w-1"


def test_a_team_lease_of_a_team_that_is_declared_no_more_is_given_back_as_one(pool_rig):
    write_workers(pool_rig, teams=PAIR)
    served = Served(pool_rig)
    team = served.granted(team="pair")
    pis = [pool_rig.status(name)["pi_pid"] for name in ("w-1", "w-2")]

    write_workers(pool_rig)
    assert served.reload().status_code == 200
    assert served.lease(team="pair").status_code == 400

    token = team["members"][0]["token"]
    ended = served.release(team["team_lease_id"], token)
    assert ended.status_code == 200, ended.text
    assert active(pool_rig) == []
    assert wait_until(lambda: not any(pid_alive(pi) for pi in pis))


# -- an expired lease whose member does not stop -------------------------------

# What the runner's words may hold of the member's host, and no answer may.
HOST_WORDS = "/home/someone/pi: no answer in 20 s"


def expired_and_stuck(served, monkeypatch) -> dict:
    """A lease on "w-1" that is past its idle limit at the next request, with
    a member whose stop fails from now on. The process is stopped all the
    same, so the test leaves none behind."""
    lease = served.granted(pool="workers")
    served.state.pool.clock = lambda: datetime.now(timezone.utc) + timedelta(minutes=11)
    real_stop = pool_service.runner.stop

    def stop(agent, cause, **tokens):
        real_stop(agent, cause, **tokens)
        raise RunnerError(f"member '{agent}' did not stop: {HOST_WORDS}")

    monkeypatch.setattr(pool_service.runner, "stop", stop)
    return lease


def test_a_reload_is_put_in_force_when_an_expired_member_does_not_stop(
    served, pool_rig, monkeypatch
):
    lease = expired_and_stuck(served, monkeypatch)
    write_workers(pool_rig, teams=PAIR)

    response = served.reload()

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json()["result"]["teams"] == 1
    assert HOST_WORDS not in response.text and "/home/someone" not in response.text
    assert [team.name for team in served.state.pool.config.teams] == ["pair"]
    assert list(served.state.pool_errors) == []
    # the lease has ended, which is all the reload had to do with it
    assert active(pool_rig) == []
    with pool_rig.store() as store:
        assert store.get_lease(lease["lease_id"]).state == "expired"
    assert [r["operation"] for r in audit_records(pool_rig.root)] == [
        "lease_request", "pool_reload",
    ]


def test_a_save_from_a_management_page_is_in_force_when_an_expired_member_does_not_stop(
    served, pool_rig, monkeypatch
):
    expired_and_stuck(served, monkeypatch)
    browser = TestClient(served.application, follow_redirects=False)
    token = auth.mint_token(pool_rig.root, "developer")
    signed = browser.post(web.SIGNIN_PATH, data={"identity": "developer", "token": token})
    assert signed.status_code == 303, signed.text
    form = {
        "session": browser.cookies[web.SESSION_COOKIE], "name": "worker",
        "role": "Does one plan task and reports what changed", "tools": "read\nbash\n",
        "egress": "local", "prompt": NEW_PROMPT,
    }

    response = browser.post("/pool/definitions/worker/edit", data=form)

    assert response.status_code == 303, response.text
    assert [d.prompt for d in served.state.pool.config.definitions] == [NEW_PROMPT]
    assert list(served.state.pool_errors) == []
    assert [r["operation"] for r in audit_records(pool_rig.root)] == [
        "lease_request", "definition_save", "pool_reload",
    ]
    # a member started from now on runs what the page saved
    assert served.granted(pool="workers")["agent"] == "w-1"
    assert prompt_of(pool_rig, "w-1") == NEW_PROMPT


# -- the service ---------------------------------------------------------------


def nothing_in_the_way(old, new, store) -> list:
    return []


def test_a_row_that_waits_for_a_pool_declared_no_more_holds_up_no_one(pool_rig):
    local = pool_rig.local_member()
    service = pool_rig.service(pool_rig.config(local))
    with pool_rig.store() as store:
        store.add_waiting(WaitingRequest(
            id="w-1", pool="gone", team=None, holder_label="someone",
            arrived="2026-03-01T11:00:00.000+00:00",
        ))

    grant = service.grant("rebasers", holder_label="o", cwd=pool_rig.work)

    assert grant.agent == local.name
    # and the request that waits is told so at its next look
    with pytest.raises(UnknownPool):
        service.grant("gone", holder_label="someone", cwd=pool_rig.work, waiting_id="w-1")


def test_a_swap_answers_with_what_stands_in_its_way_and_swaps_nothing_then(pool_rig):
    config = pool_rig.config(pool_rig.local_member())
    service = pool_rig.service(config)
    other = replace(config, pools=(replace(config.pools[0], name="others"),))
    seen = []

    def in_the_way(old, new, store):
        seen.append((old, new, store.list_leases()))
        return ["a lease is out"]

    assert service.swap(other, in_the_way) == ("a lease is out",)
    assert service.config is config
    assert seen == [(config, other, [])]

    assert service.swap(other, nothing_in_the_way) == ()
    assert service.config is other


class Turn:
    """The service's turn, with a count of how deep it is taken."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.depth = 0
        # for each read and each write of the configuration: was the turn taken?
        self.reads: list[bool] = []
        self.writes: list[bool] = []

    def __enter__(self):
        self.lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *exc) -> None:
        self.depth -= 1
        self.lock.release()


class Watched(PoolService):
    """A service that notes, for each read and each write of its
    configuration after it was made, whether its turn was taken."""

    def __getattribute__(self, name):
        if name == "config":
            turn = object.__getattribute__(self, "__dict__").get("_turn")
            if isinstance(turn, Turn):
                turn.reads.append(turn.depth > 0)
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value) -> None:
        turn = self.__dict__.get("_turn")
        if name == "config" and isinstance(turn, Turn):
            turn.writes.append(turn.depth > 0)
        object.__setattr__(self, name, value)


def watched(pool_rig, config) -> tuple[Watched, Turn]:
    plain = pool_rig.service(config)
    service = Watched(
        config=config, open_store=plain.open_store, pool_token=plain.pool_token,
        config_root=plain.config_root, clock=plain.clock, mint_id=plain.mint_id,
        mint_token=plain.mint_token,
    )
    turn = Turn()
    service._turn = turn
    return service, turn


def test_the_configuration_is_swapped_and_read_by_a_grant_inside_one_turn(pool_rig):
    local = pool_rig.local_member()
    second = replace(local, name="local-2")
    config = pool_rig.config(local, second)
    both = Team(name="both", roles=(Role(name="doer", pool="rebasers", count=2),))
    config = replace(config, teams=(both,))
    service, turn = watched(pool_rig, config)

    service.swap(replace(config, pools=config.pools), nothing_in_the_way)
    assert turn.writes == [True]

    turn.reads.clear()
    service.grant("rebasers", holder_label="o", cwd=pool_rig.work)
    assert turn.reads and all(turn.reads)
    service.end_lease("lease-1", "released")

    turn.reads.clear()
    service.grant_team("both", holder_label="o", cwd=pool_rig.work)
    assert turn.reads and all(turn.reads)


# -- the verb, asked of a real daemon ------------------------------------------


@pytest.fixture
def workers_pool(monkeypatch):
    """The pool directory the request verb's daemon is started on is the one above."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_workers)


@pytest.fixture
def pool_daemon(workers_pool, plain_daemon):
    """The request verb's daemon, under the name its checkout asks for it by."""
    return plain_daemon


def reload_verb(root, *args: str):
    return runner.invoke(app, ["serve", "--root", str(root), "pool", "reload", *args])


def test_after_a_hand_edit_and_the_reload_verb_a_new_team_is_leased_by_the_same_process(
    checkout, pool_rig, pool_daemon
):
    client = RemotePool(pool_daemon["url"], pool_daemon["token"])
    pid = client.reload()["pid"]
    unknown = runner.invoke(app, ["lease", "request", "--team", "pair", "--wait", "0"])
    assert unknown.exit_code != 0
    assert "team 'pair' is not declared" in " ".join(unknown.output.split())

    write_workers(pool_rig, teams=PAIR)
    reloaded = reload_verb(pool_daemon["root"])

    assert reloaded.exit_code == 0, reloaded.output
    output = " ".join(reloaded.output.split())
    assert f"process {pid}" in output and "remote 'home'" in output
    assert "1 team" in output
    assert pid == os.getpid()
    asked = runner.invoke(app, ["lease", "request", "--team", "pair", "--wait", "0", "--json"])
    assert asked.exit_code == 0, asked.output
    assert [m["agent"] for m in json.loads(asked.stdout)["members"]] == ["w-1", "w-2"]
    assert client.reload()["pid"] == pid
    assert [r["operation"] for r in audit_records(pool_daemon["root"])].count("pool_reload") == 3


def test_the_reload_verb_prints_every_fault_and_the_old_configuration_serves_on(
    checkout, pool_rig, pool_daemon
):
    faults = break_directory(pool_rig)

    reloaded = reload_verb(pool_daemon["root"])

    assert reloaded.exit_code == 1
    output = " ".join(reloaded.output.split())
    for fault in faults:
        assert " ".join(fault.split()) in output, reloaded.output
    assert "Traceback" not in reloaded.output
    granted = runner.invoke(app, ["lease", "request", "workers", "--json"])
    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.stdout)["agent"] == "w-1"


def test_the_reload_verb_is_refused_to_a_requesters_token(checkout, pool_rig, pool_daemon):
    token = auth.mint_token(pool_daemon["root"], "requester")
    added = runner.invoke(app, ["remote", "add", "guest", pool_daemon["url"], "--token", token])
    assert added.exit_code == 0, added.output

    reloaded = reload_verb(pool_daemon["root"], "--remote", "guest")

    assert reloaded.exit_code == 1
    assert "developer" in reloaded.output and "Traceback" not in reloaded.output


def test_the_reload_verb_outside_a_checkout_says_how_the_daemon_is_reached(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    reloaded = reload_verb(tmp_path / "daemon")

    assert reloaded.exit_code == 1
    assert "remote" in reloaded.output and "Traceback" not in reloaded.output


def test_the_reload_verb_says_when_the_daemon_read_another_directory(
    checkout, pool_daemon, tmp_path
):
    reloaded = reload_verb(tmp_path / "elsewhere")

    assert reloaded.exit_code == 0, reloaded.output
    output = " ".join(reloaded.output.split())
    assert str(cli_admin.pool_dir(pool_daemon["root"])) in output
    assert "not the pool directory under --root" in output
