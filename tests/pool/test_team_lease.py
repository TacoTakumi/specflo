"""``specflo lease request --team``: a team is leased all or nothing, under one id.

A request may name a team instead of a pool. Every role member of the team
becomes an ordinary lease, with an agent name and a token of its own, and all
of them carry one team lease id. The team is granted only when every role's
count fits the ledger at once. Until then it holds nothing: a team that waits
for one full pool takes no member of another, so a separate request to that
other pool is still granted. A team that would not fit with no lease out at
all is refused at once and never waits.

The team is given back as one: a release of the team lease id ends every
member lease, and a release of one member lease is refused with the team
lease id named. A member that does not start takes the whole grant back. The
orchestrator that holds the team leads it; the pool has no route by which one
member reaches another.

The order and the fit are the service's, so a fake clock and one look at a
time drive them. The verb and the routes run against a real daemon on a
loopback port, whose members are real processes, as in the request verb's
tests.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from specflo.cli import app
from specflo.daemon import pool_routes
from specflo.daemon.app import create_app
from specflo.errors import SpecfloError
from specflo.pool import cli_admin, launch, service, teamlease, waiting
from specflo.pool import config as pool_config
from specflo.pool.teams import TEAMS_DIR, Role, Team
from specflo.service.pool_remote import LEASES_PATH, RemotePool
from waits import scaled, settle, wait_until

from . import test_lease_request
from .test_lease_request import (  # noqa: F401  (fixtures)
    FIXTURES,
    REBASER,
    audit_records,
    checkout,
    runner,
)
from .test_lease_request import pool_daemon as plain_daemon  # noqa: F401  (fixture)
from .test_lease_release_list import other_checkout, token_file
from .test_runner import pid_alive

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"

REVIEW = Team(
    name="review",
    roles=(
        Role(name="worker", pool="workers", count=1),
        Role(name="critic", pool="critics", count=1),
    ),
)


@pytest.fixture(autouse=True)
def short_interval(monkeypatch):
    """A waiting request is looked at again every few hundredths of a second."""
    monkeypatch.setattr(waiting, "POLL_INTERVAL", 0.05)


def team_config(pool_rig, *teams: Team):
    """Two pools: "workers" on local-1 and local-2, "critics" on hosted-1 alone."""
    first, critic = pool_rig.local_member(), pool_rig.hosted_member()
    second = dataclasses.replace(first, name="local-2")
    config = pool_rig.config(first, second)
    (rebasers,) = config.pools
    workers = dataclasses.replace(rebasers, name="workers")
    critics = dataclasses.replace(rebasers, name="critics", members=(critic.name,), size=1)
    return dataclasses.replace(
        config, members=(first, second, critic), pools=(workers, critics),
        teams=teams or (REVIEW,),
    )


def asks_team(pool_rig, svc, team: str, label: str, *, wait: float = 3600, **keys):
    ids = iter([f"request-{label}"])
    return waiting.Waiting(
        svc, None, team=team, holder_label=label, cwd=pool_rig.work, wait=wait,
        mint_id=lambda: next(ids), **keys,
    )


def active(pool_rig) -> list:
    with pool_rig.store() as store:
        return store.list_leases(state="active")


def waiting_rows(pool_rig) -> list:
    with pool_rig.store() as store:
        return store.list_waiting()


# -- the service: all or nothing, one look at a time --------------------------


def test_a_team_that_fits_is_granted_one_team_lease_id_and_a_lease_to_each_role_member(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))

    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)

    assert grant.team_lease_id.startswith("team-")
    assert [(m.role, m.pool, m.agent) for m in grant.members] == [
        ("worker", "workers", "local-1"), ("critic", "critics", "hosted-1"),
    ]
    assert len({m.token for m in grant.members}) == 2
    assert len({m.lease_id for m in grant.members}) == 2
    rows = active(pool_rig)
    assert [row.id for row in rows] == [m.lease_id for m in grant.members]
    assert {row.team_lease_id for row in rows} == {grant.team_lease_id}
    assert [row.holder_hash for row in rows] == [
        service.hash_token(m.token) for m in grant.members
    ]
    # each member is an ordinary lease: a process of its own, granted as any is
    for member in grant.members:
        assert pid_alive(pool_rig.status(member.agent)["pi_pid"])
    with pool_rig.store() as store:
        assert [t.kind for t in store.list_transitions()] == ["granted", "granted"]


def test_a_team_waits_for_a_full_pool_and_holds_nothing_of_the_other(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))
    critic = svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    team = asks_team(pool_rig, svc, "review", "lead")

    assert team.attempt() is None

    (row,) = waiting_rows(pool_rig)
    assert (row.id, row.pool, row.team, row.holder_label) == (
        "request-lead", None, "review", "lead",
    )
    assert "review" in team.full and "critic" in team.full and "critics" in team.full
    assert team.place() == 1
    # nothing of the workers pool is held for the team
    assert [lease.id for lease in active(pool_rig)] == [critic.lease_id]
    # and the team that waits ahead holds up no request that fits
    separate = svc.grant("workers", holder_label="b", cwd=pool_rig.work)
    assert separate.agent == "local-1"

    svc.end_lease(critic.lease_id, "released")
    grant = team.attempt()

    assert [(m.role, m.agent) for m in grant.members] == [
        ("worker", "local-2"), ("critic", "hosted-1"),
    ]
    assert waiting_rows(pool_rig) == []
    assert team.place() is None


def test_without_a_time_to_wait_a_team_that_does_not_fit_is_refused_and_nothing_is_written(
    pool_rig,
):
    svc = pool_rig.service(team_config(pool_rig))
    svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    with pool_rig.store() as store:
        before = (store.list_leases(), store.list_transitions())

    with pytest.raises(service.NoFreeMember, match="team 'review'.*role 'critic'"):
        svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert (store.list_leases(), store.list_transitions()) == before
    assert waiting_rows(pool_rig) == []


def test_every_member_of_a_role_must_fit_at_once(pool_rig):
    pair = Team(name="pair", roles=(Role(name="worker", pool="workers", count=2),))
    svc = pool_rig.service(team_config(pool_rig, pair))
    one = svc.grant("workers", holder_label="a", cwd=pool_rig.work)

    # one worker is free and the role takes two: the free one is not taken
    with pytest.raises(service.NoFreeMember, match="team 'pair'.*role 'worker'"):
        svc.grant_team("pair", holder_label="lead", cwd=pool_rig.work)
    assert [lease.id for lease in active(pool_rig)] == [one.lease_id]

    svc.end_lease(one.lease_id, "released")
    grant = svc.grant_team("pair", holder_label="lead", cwd=pool_rig.work)

    assert [(m.role, m.agent) for m in grant.members] == [
        ("worker", "local-1"), ("worker", "local-2"),
    ]


def test_a_team_that_would_not_fit_an_empty_pool_is_refused_at_once_and_never_waits(pool_rig):
    # each role fits its pool, and the two together take more than the pool grants
    crowd = Team(name="crowd", roles=(
        Role(name="critic", pool="critics", count=1), Role(name="judge", pool="critics", count=1),
    ))
    svc = pool_rig.service(team_config(pool_rig, crowd))

    with pytest.raises(teamlease.TeamNeverFits, match="team 'crowd'.*role 'judge'"):
        asks_team(pool_rig, svc, "crowd", "lead").attempt()

    assert waiting_rows(pool_rig) == []
    assert active(pool_rig) == []


def test_a_team_that_is_not_declared_is_refused_by_name(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))

    with pytest.raises(service.UnknownTeam, match="'reviewers'.*review"):
        svc.grant_team("reviewers", holder_label="lead", cwd=pool_rig.work)


def test_an_idle_limit_one_pool_refuses_leaves_nothing_behind(pool_rig):
    config = team_config(pool_rig)
    workers, critics = config.pools
    config = dataclasses.replace(
        config, pools=(workers, dataclasses.replace(critics, idle_max=1800))
    )
    svc = pool_rig.service(config)

    # the worker's pool allows an hour; the critic's, asked second, does not
    with pytest.raises(service.IdleLimitError, match="critics"):
        svc.grant_team("review", holder_label="lead", cwd=pool_rig.work, idle_limit=3600)

    with pool_rig.store() as store:
        assert store.list_leases() == []

    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work, idle_limit=900)
    assert {lease.idle_limit for lease in active(pool_rig)} == {900}
    assert len(grant.members) == 2


def test_a_waiting_team_that_fits_is_served_before_a_later_request(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))
    critic = svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    team = asks_team(pool_rig, svc, "review", "lead")
    assert team.attempt() is None

    svc.end_lease(critic.lease_id, "released")

    # the critic is free again, and the team that came first takes it
    with pytest.raises(service.NoFreeMember, match="came earlier, for team 'review'") as refused:
        svc.grant("critics", holder_label="b", cwd=pool_rig.work)
    assert "None" not in str(refused.value)
    assert team.attempt() is not None


def test_a_team_gives_way_to_an_earlier_request_that_fits(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))
    critic = svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    earlier = waiting.Waiting(
        svc, "critics", holder_label="b", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-b",
    )
    assert earlier.attempt() is None
    team = asks_team(pool_rig, svc, "review", "lead")
    assert team.attempt() is None
    assert team.place() == 2

    svc.end_lease(critic.lease_id, "released")

    assert team.attempt() is None
    assert "team 'review'" in team.full and "came earlier" in team.full
    assert active(pool_rig) == []
    assert earlier.attempt().agent == "hosted-1"


def test_a_member_that_does_not_start_takes_the_whole_grant_back(pool_rig, monkeypatch):
    # the critic runs through an account whose key is not there: it cannot start
    monkeypatch.delenv("TEAM_A_KEY")
    svc = pool_rig.service(team_config(pool_rig))

    with pytest.raises(launch.LaunchError, match="TEAM_A_KEY"):
        svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)

    assert active(pool_rig) == []
    assert wait_until(lambda: pool_rig.pane_names() == [])
    with pool_rig.store() as store:
        leases = store.list_leases()
        ended = {
            lease.pool: [(t.kind, t.cause) for t in store.list_transitions(lease_id=lease.id)]
            for lease in leases
        }
    assert [lease.state for lease in leases] == ["released", "released"]
    # the worker had started, and went because its team did not
    assert [kind for kind, _ in ended["workers"]] == ["granted", "released"]
    assert "team" in ended["workers"][1][1] and "did not start" in ended["workers"][1][1]
    assert "TEAM_A_KEY" in ended["critics"][0][1]
    # the members are free to the next request
    assert svc.grant("workers", holder_label="b", cwd=pool_rig.work).agent == "local-1"


def test_a_team_is_released_as_one_and_no_member_lease_by_itself(pool_rig):
    svc = pool_rig.service(team_config(pool_rig))
    grant = svc.grant_team("review", holder_label="lead", cwd=pool_rig.work)
    pids = [pool_rig.status(m.agent)["pi_pid"] for m in grant.members]
    worker = active(pool_rig)[0]

    with pytest.raises(teamlease.MemberOfATeam, match=grant.team_lease_id):
        teamlease.refuse_member_release(worker)
    assert len(active(pool_rig)) == 2

    ended = teamlease.release_team(svc, grant.team_lease_id)

    assert [(end.lease_id, end.state) for end in ended] == [
        (m.lease_id, "released") for m in grant.members
    ]
    assert active(pool_rig) == []
    assert wait_until(lambda: not any(pid_alive(pid) for pid in pids))
    # a lease of no team is released by itself as ever
    single = svc.grant("workers", holder_label="b", cwd=pool_rig.work)
    teamlease.refuse_member_release(active(pool_rig)[0])
    assert svc.end_lease(single.lease_id, "released").state == "released"


# -- the daemon and the verb --------------------------------------------------


def write_team_pool(rig) -> None:
    """A pool directory under the rig's daemon root: "workers" of two local
    members, "critics" of one, and the team "review" of a worker and a critic.
    The members' models may be loaded side by side."""
    directory = cli_admin.pool_dir(rig.root)
    folder = directory / pool_config.DEFINITIONS_DIR
    folder.mkdir(parents=True)
    (folder / "rebaser.md").write_text(REBASER, encoding="utf-8")
    shutil.copy(FIXTURES / "llama-swap.yaml", directory / "llama-swap.yaml")

    def member(name: str, model: str) -> dict:
        return {
            "name": name, "command": rig.command, "backing": "local",
            "model": model, "labels": [], "capacity": 1, "egress": "local",
        }

    def pool(name: str, members: list[str]) -> dict:
        return {
            "name": name, "definition": "rebaser", "members": members,
            "size": len(members), "idle_default": "10m", "idle_max": "4h",
        }

    data = {
        "llama_swap": "llama-swap.yaml",
        # A local member's models file is a copy of the operator's; the rig's
        # own stands in for it.
        "models_file": str(rig.models_file),
        "members": [
            member("local-1", "model-a"), member("local-2", "model-a"),
            member("local-3", "model-c"),
        ],
        "pools": [pool("workers", ["local-1", "local-2"]), pool("critics", ["local-3"])],
    }
    (directory / pool_config.POOL_FILE).write_text(yaml.safe_dump(data), encoding="utf-8")
    roles = [
        {"name": role.name, "pool": role.pool, "count": role.count} for role in REVIEW.roles
    ]
    (directory / TEAMS_DIR).mkdir()
    (directory / TEAMS_DIR / "review.md").write_text(
        "---\n" + yaml.safe_dump({"roles": roles}) + "---\n\nReview each change.\n",
        encoding="utf-8",
    )


@pytest.fixture
def team_pool(monkeypatch):
    """The pool directory the request verb's daemon is started on holds a team."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_team_pool)


@pytest.fixture
def pool_daemon(team_pool, plain_daemon):
    """The request verb's daemon, under the name its checkout asks for it by."""
    return plain_daemon


def request_team(*args: str) -> dict:
    result = runner.invoke(app, ["lease", "request", "--team", "review", *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def states(pool_rig, granted: dict) -> list[str]:
    with pool_rig.store() as store:
        return [store.get_lease(member["lease"]).state for member in granted["members"]]


def test_the_verb_prints_the_team_lease_and_each_roles_agent_and_keeps_a_token_each(
    checkout, pool_rig, pool_daemon
):
    result = runner.invoke(app, ["lease", "request", "--team", "review"])

    assert result.exit_code == 0, result.output
    rows = active(pool_rig)
    (team_lease_id,) = {row.team_lease_id for row in rows}
    assert team_lease_id in result.stdout
    for role, agent in (("worker", "local-1"), ("critic", "local-3")):
        assert role in result.stdout and agent in result.stdout
        token = token_file(checkout, agent).read_text(encoding="utf-8").strip()
        assert token not in result.output
        assert service.hash_token(token) in {row.holder_hash for row in rows}
        # the holder drives each member with the token the verb stored for it
        answered = runner.invoke(app, ["agent", "prompt", agent, "look at the change"])
        assert answered.exit_code == 0, answered.output
    # one act of one identity: the team's grant
    (record,) = audit_records(pool_daemon["root"])
    assert (record["operation"], record["id"]) == ("lease_request", team_lease_id)


def test_json_output_is_one_object_with_the_team_lease_and_the_members_and_no_token(checkout):
    granted = request_team()

    assert granted["team_lease"].startswith("team-")
    assert (granted["team"], granted["remote"]) == ("review", "home")
    assert [(m["role"], m["pool"], m["agent"]) for m in granted["members"]] == [
        ("worker", "workers", "local-1"), ("critic", "critics", "local-3"),
    ]
    for member in granted["members"]:
        assert member["lease"].startswith("lease-")
        token = token_file(checkout, member["agent"]).read_text(encoding="utf-8").strip()
        assert token not in json.dumps(granted)


def test_releasing_the_team_lease_ends_every_member_and_forgets_every_token(
    checkout, pool_rig, pool_daemon
):
    granted = request_team()
    pids = [pool_rig.status(m["agent"])["pi_pid"] for m in granted["members"]]

    result = runner.invoke(app, ["lease", "release", granted["team_lease"], "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"lease": granted["team_lease"], "state": "released"}
    assert states(pool_rig, granted) == ["released", "released"]
    assert wait_until(lambda: not any(pid_alive(pid) for pid in pids))
    for member in granted["members"]:
        assert not token_file(checkout, member["agent"]).exists()
    record = audit_records(pool_daemon["root"])[-1]
    assert (record["operation"], record["id"]) == ("lease_release", granted["team_lease"])
    assert len(audit_records(pool_daemon["root"])) == 2
    # a second release changes nothing, reports how the team ended and is no one's act
    again = runner.invoke(app, ["lease", "release", granted["team_lease"], "--json"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout) == {"lease": granted["team_lease"], "state": "released"}
    assert len(audit_records(pool_daemon["root"])) == 2
    # the members are free to the next team
    assert request_team()["team_lease"] != granted["team_lease"]


def test_releasing_one_member_lease_of_a_team_exits_non_zero_naming_the_team(
    checkout, pool_rig, pool_daemon
):
    granted = request_team()
    worker = granted["members"][0]

    refused = runner.invoke(app, ["lease", "release", worker["lease"]])

    assert refused.exit_code != 0
    assert granted["team_lease"] in refused.output
    assert states(pool_rig, granted) == ["active", "active"]
    assert token_file(checkout, worker["agent"]).exists()
    assert pid_alive(pool_rig.status(worker["agent"])["pi_pid"])
    assert [r["operation"] for r in audit_records(pool_daemon["root"])] == ["lease_request"]

    assert runner.invoke(app, ["lease", "release", granted["team_lease"]]).exit_code == 0


def test_another_orchestrator_releases_neither_the_team_nor_a_member(
    checkout, pool_rig, pool_daemon, tmp_path, monkeypatch
):
    granted = request_team()
    other_checkout(tmp_path, monkeypatch, pool_daemon)

    for lease_id in (granted["team_lease"], granted["members"][0]["lease"]):
        refused = runner.invoke(app, ["lease", "release", lease_id])
        assert refused.exit_code != 0
        assert lease_id in refused.output
    # one that is not the holder is not told which team a lease is of
    assert granted["team_lease"] not in refused.output
    assert states(pool_rig, granted) == ["active", "active"]

    monkeypatch.chdir(checkout)
    assert runner.invoke(app, ["lease", "release", granted["team_lease"]]).exit_code == 0


def test_the_verb_takes_a_pool_or_a_team_and_not_both(checkout, pool_rig):
    for args in ([], ["workers", "--team", "review"]):
        refused = runner.invoke(app, ["lease", "request", *args])
        assert refused.exit_code != 0
        assert "--team" in refused.output and "pool" in refused.output
    assert active(pool_rig) == []


def test_the_route_takes_a_pool_or_a_team_and_not_both(pool_daemon, pool_rig):
    client = TestClient(create_app(pool_daemon["root"]))
    client.headers["Authorization"] = f"Bearer {pool_daemon['token']}"
    cwd = str(pool_rig.work)

    for named in ({}, {"pool": "workers", "team": "review"}, {"team": ""}, {"team": 3}):
        response = client.post(LEASES_PATH, json={"cwd": cwd, **named})
        assert response.status_code == 422, (named, response.text)
    unknown = client.post(LEASES_PATH, json={"cwd": cwd, "team": "reviewers"})
    assert unknown.status_code == 400
    assert "reviewers" in unknown.json()["detail"]
    assert active(pool_rig) == []


def test_with_the_critics_full_the_team_waits_and_a_request_to_the_workers_is_still_granted(
    pool_daemon, pool_rig
):
    holder = RemotePool(pool_daemon["url"], pool_daemon["token"], timeout=60)
    critic = holder.request("critics", cwd=str(pool_rig.work))
    box: dict = {}
    notices = []

    def ask() -> None:
        client = RemotePool(pool_daemon["url"], pool_daemon["token"], timeout=60)
        try:
            box["grant"] = client.request_team(
                "review", cwd=str(pool_rig.work), wait=60, on_waiting=notices.append
            )
        except SpecfloError as exc:
            box["error"] = exc
        finally:
            client.client.close()

    thread = threading.Thread(target=ask, daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: len(waiting_rows(pool_rig)) == 1 and notices)
        (notice,) = notices
        assert (notice.team, notice.pool, notice.place, notice.wait) == ("review", None, 1, 60)
        assert "critics" in notice.full
        # the team holds nothing while it waits
        assert [lease.id for lease in active(pool_rig)] == [critic.lease_id]
        separate = holder.request("workers", cwd=str(pool_rig.work))
        assert separate.agent == "local-1"
        settle(0.3)
        assert thread.is_alive() and not box

        holder.release(critic.lease_id, token=critic.token)
        thread.join(timeout=scaled(30))

        assert not thread.is_alive()
        grant = box["grant"]
        assert grant.team_lease_id.startswith("team-")
        assert [(m.role, m.agent) for m in grant.members] == [
            ("worker", "local-2"), ("critic", "local-3"),
        ]
        assert all(m.token for m in grant.members)
        assert waiting_rows(pool_rig) == []
    finally:
        holder.client.close()


def test_the_verb_says_on_its_error_stream_that_it_waits_for_the_team(checkout, pool_rig):
    taken = test_lease_request.request("critics")

    refused = runner.invoke(app, ["lease", "request", "--team", "review", "--wait", "1"])

    assert refused.exit_code != 0
    assert "team 'review'" in refused.stderr
    assert "Waiting for team 'review'" in refused.stderr
    assert "pool 'None'" not in refused.output
    assert [lease.id for lease in active(pool_rig)] == [taken["lease"]]


# -- structure ----------------------------------------------------------------

# What a way for one member to reach another would be called.
MESSAGING = ("message", "send", "tell", "prompt", "steer", "inbox", "mail", "chat", "peer")


def test_the_pool_offers_no_member_to_member_message_route():
    routes = [route for route in pool_routes.router.routes if hasattr(route, "methods")]
    paths = {route.path for route in routes}
    assert LEASES_PATH in paths  # the walk reaches the routes
    for route in routes:
        named = f"{route.path} {route.name}".lower()
        assert not [word for word in MESSAGING if word in named], route.path
        # a route acts on one lease at most: none names a second, or an agent
        assert set(route.param_convertors) <= {"lease_id"}, route.path
    # and nothing in the pool, or in its routes, speaks to a member
    modules = [*(SRC / "pool").glob("*.py"), SRC / "daemon" / "pool_routes.py"]
    for path in modules:
        if path.name == "cli_lease.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        called = {
            node.func.attr if isinstance(node.func, ast.Attribute)
            else getattr(node.func, "id", "")
            for node in ast.walk(tree) if isinstance(node, ast.Call)
        }
        speaks = {"prompt", "steer", "send", "send_message"}
        if path.name == "bridge.py":
            # Its client sends the one request it relays to llama-swap. A
            # member speaks to the bridge; the bridge answers it and reaches
            # no other member.
            speaks -= {"send"}
        assert not called & speaks, path.name


def test_the_team_lease_module_imports_no_agent_code_and_runs_nothing_by_itself():
    tree = ast.parse((SRC / "pool" / "teamlease.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    assert not [m for m in modules if "agent" in m or "runner" in m or "thread" in m]
    assert not [m for m in modules if m.split(".")[0] in {"asyncio", "time", "sched"}]
