"""A project's agent runs outside the pool, and the ledger counts what it takes all the same.

The daemon runs one agent for a hosted project, in the project's seat. No
lease started it and no pool lists it, but it runs on the same rig and the
same provider accounts as the pool's members. So the daemon root's
configuration says which model of the rig, or which account, a project's
agent uses, and every project agent that is alive, by agent discovery, stands
in the ledger with that: its model is one more model a local member has to
fit beside, and its account slot one more slot out of the cap.

A standing entry is not a lease. It has no idle limit, so no time ends it,
and no row, so the pool cannot release or preempt it: it is there while the
agent is alive and gone when the agent stops.

The ledger's own tests hand it rows and entries and read its answer. The
service's tests run real processes: a project agent started in its seat on
the stub pi, and the stub pi under an agent host for a member.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from specflo import config, daemon
from specflo.cli import app
from specflo.daemon import pool_routes, seat
from specflo.daemon.poolstore import Lease, Resource
from specflo.pool import ledger, matrix, service, standing, waiting
from specflo.pool.config import Account, Member, Pool, PoolConfig
from specflo.service.local import LocalProjectService

from .test_lease_request import write_pool
from .test_runner import STUB

runner = CliRunner()

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "pool" / "llama-swap.yaml"


def set_key(root: Path, key: str, value: object) -> None:
    """Write *key* in the root's configuration file as an admin does: by hand."""
    path = config.config_path(root)
    path.write_text(path.read_text() + f"{key}: {json.dumps(value)}\n")


# -- the daemon root's configuration ------------------------------------------


def test_a_root_whose_configuration_names_nothing_has_a_project_agent_that_takes_nothing(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")

    assert config.project_agent_use(root) == config.ProjectAgentUse(model=None, account=None)
    # nor does a directory that is no root at all
    assert config.project_agent_use(tmp_path / "nowhere") == config.ProjectAgentUse()


def test_the_root_configuration_names_the_model_or_the_account_of_the_project_agent(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")

    assert config.project_agent_use(root) == config.ProjectAgentUse(model="model-a")

    other = daemon.prepare_root(tmp_path / "other")
    set_key(other, config.PROJECT_AGENT_ACCOUNT, "team-a")

    assert config.project_agent_use(other) == config.ProjectAgentUse(account="team-a")


def test_a_value_that_is_no_name_reads_as_no_value(tmp_path, capsys):
    root = daemon.prepare_root(tmp_path / "daemon")
    set_key(root, config.PROJECT_AGENT_MODEL, 12)
    set_key(root, config.PROJECT_AGENT_ACCOUNT, "  ")
    config.reset_warnings()

    assert config.project_agent_use(root) == config.ProjectAgentUse()
    assert config.PROJECT_AGENT_MODEL in capsys.readouterr().err


def test_the_keys_survive_a_write_of_the_file_and_are_not_called_unrecognized(tmp_path, monkeypatch):
    root = daemon.prepare_root(tmp_path / "daemon")
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")

    config.write_value(root, config.field_for("agent_transport"), "rpc")

    assert config.project_agent_use(root).model == "model-a"
    assert config.report_config(root)["unknown"] == []
    monkeypatch.chdir(root)
    assert "Not recognized" not in runner.invoke(app, ["config", "list"]).stdout


def test_a_checkout_is_offered_neither_key(tmp_path):
    config.init_config(tmp_path)

    assert "project_agent" not in config.config_path(tmp_path).read_text()
    assert not {config.PROJECT_AGENT_MODEL, config.PROJECT_AGENT_ACCOUNT} & set(config.FIELDS_BY_NAME)
    assert [entry["key"] for entry in config.report_config(tmp_path)["keys"]] == list(
        config.FIELDS_BY_NAME
    )


# -- which entries stand: the project agents that are alive --------------------


AGENTS = {"dark-mode": "project-dark-mode", "login-fix": "project-login-fix"}


def test_every_live_project_agent_stands_with_the_model_the_root_names(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")

    entries = standing.entries(root, AGENTS, lambda slug: slug == "login-fix")

    assert entries == (
        ledger.Standing(
            id="standing-login-fix", project="login-fix", agent="project-login-fix",
            resources=(Resource("model", "model-a"),),
        ),
    )
    assert len(standing.entries(root, AGENTS, lambda slug: True)) == 2
    assert standing.entries(root, AGENTS, lambda slug: False) == ()


def test_a_project_agent_on_an_account_stands_with_a_slot_of_it(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")
    set_key(root, config.PROJECT_AGENT_ACCOUNT, "team-a")

    (entry,) = standing.entries(root, {"login-fix": "project-login-fix"}, lambda slug: True)

    assert entry.resources == (Resource("account", "team-a"),)


def test_with_nothing_named_nothing_stands_and_no_agent_is_asked_after(tmp_path):
    root = daemon.prepare_root(tmp_path / "daemon")

    def asked(slug):
        raise AssertionError("no agent is asked after when none would take anything")

    assert standing.entries(root, AGENTS, asked) == ()


# -- the ledger: rows and entries in, an answer out ---------------------------


def local(name: str, model: str) -> Member:
    return Member(
        name=name, command="pi", backing="local", labels=(), capacity=1,
        egress="local", model=model,
    )


def hosted(name: str) -> Member:
    return Member(
        name=name, command="pi", backing="hosted", labels=(), capacity=1,
        egress="no-train", model="some-vendor/some-model", account="shared",
    )


def pool(name: str, *members: str) -> Pool:
    return Pool(
        name=name, definition="rebaser", members=members, size=4,
        idle_default=600, idle_max=3600,
    )


def configured(cap: int = 2) -> PoolConfig:
    """A pool for each model of the fixture, and one of two hosted members on one account."""
    members = [local(f"m-{n}", f"model-{n}") for n in "abcd"] + [hosted("h1"), hosted("h2")]
    pools = [pool(f"on-{n}", f"m-{n}") for n in "abcd"] + [pool("hosted", "h1", "h2")]
    return PoolConfig(
        path=Path("pool.yaml"), accounts=(Account(name="shared", cap=cap, key_env="SHARED_KEY"),),
        members=tuple(members), pools=tuple(pools), llama_swap=FIXTURE,
        swap=matrix.read(FIXTURE),
    )


def entry(kind: str, name: str, slug: str = "login-fix") -> ledger.Standing:
    return ledger.Standing(
        id=f"standing-{slug}", project=slug, agent=f"project-{slug}",
        resources=(Resource(kind, name),),
    )


# An active row; a test gives it the member, the pool and the resources it took.
LEASE = Lease(
    id="lease-1", team_lease_id=None, holder_hash="h", holder_label="a",
    member="m", pool="p", resources=(),
    acquired="2026-03-01T12:00:00.000+00:00",
    last_activity="2026-03-01T12:00:00.000+00:00", idle_limit=600, state="active",
)


def test_a_standing_model_keeps_an_either_or_model_off_the_rig_and_the_refusal_says_who_holds_it():
    on_a = entry("model", "model-a")

    with pytest.raises(ledger.NoRoom, match="model 'model-b'") as refused:
        ledger.place(configured(), [], ledger.Request(pool="on-b"), standing=[on_a])

    # no lease is out: what holds model-a is the project's agent, and it is named
    assert "model-a" in str(refused.value)
    assert "the agent of project 'login-fix'" in str(refused.value)
    assert "leased" not in str(refused.value)
    # with the agent gone the same request fits
    assert ledger.place(configured(), [], ledger.Request(pool="on-b")).member.name == "m-b"


def test_a_standing_model_is_at_home_beside_the_models_of_its_combination_and_itself():
    on_a = [entry("model", "model-a")]

    assert ledger.place(configured(), [], ledger.Request(pool="on-c"), standing=on_a).member.name == "m-c"
    assert ledger.place(configured(), [], ledger.Request(pool="on-a"), standing=on_a).member.name == "m-a"


def test_a_refusal_tells_a_leased_model_from_a_standing_one():
    config_ = configured()
    placed = ledger.place(config_, [], ledger.Request(pool="on-c"))
    lease = dataclasses.replace(
        LEASE, member="m-c", pool="on-c", resources=placed.resources,
    )

    with pytest.raises(ledger.NoRoom, match="model 'model-d'") as refused:
        ledger.place(
            config_, [lease], ledger.Request(pool="on-d"), standing=[entry("model", "model-a")]
        )

    assert "the leased 'model-c'" in str(refused.value)
    assert "'model-a', which the agent of project 'login-fix' holds" in str(refused.value)


def test_a_standing_account_slot_counts_against_the_cap():
    slot = entry("account", "shared")
    other = entry("account", "shared", slug="dark-mode")

    # a cap of 2: one agent leaves one slot, two leave none
    placed = ledger.place(configured(), [], ledger.Request(pool="hosted"), standing=[slot])
    assert placed.member.name == "h1"
    with pytest.raises(ledger.NoRoom, match="account 'shared' is full") as refused:
        ledger.place(configured(), [], ledger.Request(pool="hosted"), standing=[slot, other])
    assert "the agent of project 'dark-mode'" in str(refused.value)
    assert "the agent of project 'login-fix'" in str(refused.value)
    # a lease and an agent fill it together
    lease = dataclasses.replace(LEASE, member="h1", pool="hosted", resources=placed.resources)
    with pytest.raises(ledger.NoRoom, match="account 'shared' is full") as refused:
        ledger.place(configured(), [lease], ledger.Request(pool="hosted"), standing=[slot])
    assert "the agent of project 'login-fix'" in str(refused.value)


def test_a_standing_entry_takes_no_slot_of_a_pool_and_no_member():
    on_a = [entry("model", "model-a"), entry("account", "shared", slug="dark-mode")]

    placed = ledger.place(configured(), [], ledger.Request(pool="on-a"), standing=on_a)

    assert placed.member.name == "m-a" and placed.agent == "m-a"
    assert {(r.kind, r.name) for r in placed.resources} == {
        ("pool", "on-a"), ("member", "m-a"), ("model", "model-a"),
    }


def test_a_standing_entry_has_no_idle_limit_and_no_state_to_leave():
    fields = {field.name for field in dataclasses.fields(ledger.Standing)}

    assert fields == {"id", "project", "agent", "resources"}


# -- the service: a live project agent beside the pool ------------------------


SLUG = "login-fix"


def project_agent(pool_rig) -> Path:
    """A hosted project on the rig's daemon root, its agent alive on the stub pi; the root."""
    root = daemon.prepare_root(pool_rig.root)
    projects = LocalProjectService(root, config.load_config(root), actor="requester", hosted=True)
    project = projects.create_project("Login fix")
    projects.start_brainstorm(project.slug)
    assert project.slug == SLUG
    seat.scaffold(root, SLUG, "http://127.0.0.1:8741", "agent-secret")
    config.write_value(root, config.field_for("agent_transport"), "rpc")
    scenario = pool_rig.tmp_path / "scenario-project.json"
    scenario.write_text(json.dumps({"reply": "ok"}), encoding="utf-8")
    seat.start_agent(root, SLUG, pi_cmd=f"{sys.executable} {STUB} {scenario}")
    assert seat.liveness(root, SLUG).alive
    return root


def on_model_b(pool_rig) -> PoolConfig:
    """One pool, "rebasers", of one local member that runs model-b."""
    member = dataclasses.replace(pool_rig.local_member(), name="local-b", model="model-b")
    return dataclasses.replace(
        pool_rig.config(member), llama_swap=FIXTURE, swap=matrix.read(FIXTURE)
    )


def standing_service(pool_rig, root: Path, pool_config: PoolConfig) -> service.PoolService:
    svc = pool_rig.service(pool_config)
    svc.standing = lambda: standing.entries(
        root, seat.agent_mapping(root), lambda slug: seat.liveness(root, slug).alive
    )
    return svc


def test_a_request_for_the_other_model_waits_on_a_live_project_agent_and_its_stop_grants_it(pool_rig):
    root = project_agent(pool_rig)
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")
    svc = standing_service(pool_rig, root, on_model_b(pool_rig))
    asked = waiting.Waiting(
        svc, "rebasers", holder_label="orchestrator", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-b",
    )

    # the pool has granted nothing and its one member is free
    assert asked.attempt() is None
    with pool_rig.store() as store:
        assert [row.id for row in store.list_waiting()] == ["request-b"]
        assert store.list_leases() == []
    with pytest.raises(service.NoFreeMember, match="the agent of project 'login-fix'"):
        svc.grant("rebasers", holder_label="another", cwd=pool_rig.work, waiting_id="request-b")

    assert seat.stop_agent(root, SLUG) == seat.agent_name(SLUG)
    granted = asked.attempt()

    assert granted.agent == "local-b"
    svc.end_lease(granted.lease_id, "released")


def test_no_time_ends_a_standing_entry(pool_rig):
    root = project_agent(pool_rig)
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")
    svc = standing_service(pool_rig, root, on_model_b(pool_rig))

    pool_rig.clock.advance(days=30)

    assert svc.expire_due() == []
    assert seat.liveness(root, SLUG).alive
    assert [e.project for e in svc.standing()] == [SLUG]
    with pytest.raises(service.NoFreeMember, match="model 'model-b'"):
        svc.grant("rebasers", holder_label="orchestrator", cwd=pool_rig.work)


def test_end_lease_refuses_a_standing_entry_and_the_agent_stays(pool_rig):
    root = project_agent(pool_rig)
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")
    svc = standing_service(pool_rig, root, on_model_b(pool_rig))
    (held,) = svc.standing()

    for kind, extra in (("released", {}), ("preempted", {"request_id": "request-b"})):
        with pytest.raises(service.UnknownLease):
            svc.end_lease(held.id, kind, **extra)

    assert seat.liveness(root, SLUG).alive
    assert svc.standing() == (held,)
    with pool_rig.store() as store:
        assert store.list_transitions() == []


def test_a_service_handed_no_reader_counts_no_standing_entry(pool_rig):
    root = project_agent(pool_rig)
    set_key(root, config.PROJECT_AGENT_MODEL, "model-a")
    svc = pool_rig.service(on_model_b(pool_rig))

    assert tuple(svc.standing()) == ()
    granted = svc.grant("rebasers", holder_label="orchestrator", cwd=pool_rig.work)
    svc.end_lease(granted.lease_id, "released")


def test_the_daemons_pool_counts_the_project_agents_of_its_root(pool_rig):
    root = project_agent(pool_rig)
    # the pool on disk has one local member, on model-a
    write_pool(pool_rig)
    set_key(root, config.PROJECT_AGENT_MODEL, "model-b")

    svc, errors = pool_routes.open_pool(root)

    assert errors == ()
    assert [(e.project, e.agent, e.resources) for e in svc.standing()] == [
        (SLUG, seat.agent_name(SLUG), (Resource("model", "model-b"),)),
    ]
    with pytest.raises(service.NoFreeMember, match="the agent of project 'login-fix'"):
        svc.grant("rebasers", holder_label="orchestrator", cwd=pool_rig.work)

    seat.stop_agent(root, SLUG)

    assert tuple(svc.standing()) == ()
    granted = svc.grant("rebasers", holder_label="orchestrator", cwd=pool_rig.work)
    assert granted.agent == "local-1"
    svc.end_lease(granted.lease_id, "released")
