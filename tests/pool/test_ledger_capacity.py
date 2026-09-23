"""One ledger for the whole factory: pool slots and member capacity, counted across pools.

Whether a request fits is one question, put to one function: given the
configuration, the leases that are out and the request, the ledger answers
with the member the request gets, the name its agent runs under and every
resource the lease takes - or says what there is no room in. It counts from
what the lease rows say they took, so a member that two pools list is one
member: a lease on it through the first pool is in the way of a request to
the second, whoever asks and from whichever project. Nobody has a share set
aside; there is no field to write one in.

The ledger's own tests hand it rows and read its answer. The service's tests
run real processes: the stub pi under an agent host, as in the grant's tests.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from specflo.agent.statefiles import AgentPaths
from specflo.daemon.poolstore import Lease
from specflo.errors import SpecfloError
from specflo.pool import config as pool_config
from specflo.pool import ledger, service, teams, waiting
from specflo.pool.config import Member, Pool, PoolConfig

from .test_runner import pid_alive, wait_until

SRC = Path(__file__).resolve().parents[2] / "src" / "specflo"


def member(name: str, capacity: int = 1) -> Member:
    return Member(
        name=name, command="pi", backing="local", labels=(), capacity=capacity,
        egress="local", model="tc3",
    )


def pool(name: str, *members: str, size: int = 1) -> Pool:
    return Pool(
        name=name, definition="rebaser", members=members, size=size,
        idle_default=600, idle_max=3600,
    )


def configured(members: list[Member], pools: list[Pool]) -> PoolConfig:
    return PoolConfig(path=Path("pool.yaml"), members=tuple(members), pools=tuple(pools))


def lease_of(placement: ledger.Placement, n: int, *, state: str = "active") -> Lease:
    """The row a grant writes for *placement*."""
    taken = dict((r.kind, r.name) for r in placement.resources)
    return Lease(
        id=f"lease-{n}", team_lease_id=None, holder_hash="h", holder_label="a",
        member=taken["member"], pool=taken["pool"], resources=placement.resources,
        acquired="2026-03-01T12:00:00.000+00:00",
        last_activity="2026-03-01T12:00:00.000+00:00", idle_limit=600, state=state,
    )


def kinds(placement: ledger.Placement) -> set[tuple[str, str]]:
    return {(r.kind, r.name) for r in placement.resources}


# -- the ledger: rows in, an answer out ---------------------------------------


def test_a_request_gets_the_first_listed_member_with_room_and_the_resources_it_takes():
    config = configured([member("m1"), member("m2")], [pool("rebasers", "m1", "m2", size=2)])

    first = ledger.place(config, [], ledger.Request(pool="rebasers"))
    second = ledger.place(config, [lease_of(first, 1)], ledger.Request(pool="rebasers"))

    assert (first.member.name, first.agent) == ("m1", "m1")
    assert kinds(first) == {("pool", "rebasers"), ("member", "m1")}
    assert (second.member.name, second.agent) == ("m2", "m2")


def test_a_member_two_pools_share_is_one_member():
    config = configured([member("m1")], [pool("rebasers", "m1"), pool("critics", "m1")])
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    # "critics" has granted nothing, and its one member serves a lease all the same
    with pytest.raises(ledger.NoRoom, match="critics") as refused:
        ledger.place(config, out, ledger.Request(pool="critics"))

    assert isinstance(refused.value, SpecfloError)
    assert "m1" in str(refused.value)
    assert ledger.place(config, [], ledger.Request(pool="critics")).member.name == "m1"


def test_a_pool_holds_no_more_leases_than_its_size():
    config = configured([member("m1"), member("m2", capacity=3)], [pool("rebasers", "m1", "m2")])
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    with pytest.raises(ledger.NoRoom, match="pool 'rebasers' is full"):
        ledger.place(config, out, ledger.Request(pool="rebasers"))


def test_a_member_serves_as_many_leases_as_its_capacity_each_under_its_own_agent_name():
    config = configured([member("m1", capacity=2)], [pool("rebasers", "m1", size=2)])
    out = []
    for n in (1, 2):
        out.append(lease_of(ledger.place(config, out, ledger.Request(pool="rebasers")), n))

    assert [ledger.agent_of(row) for row in out] == ["m1", "m1.2"]
    assert [row.member for row in out] == ["m1", "m1"]
    # the pool is as large as the member here, so what is full is the member
    roomy = dataclasses.replace(config, pools=(pool("rebasers", "m1", size=3),))
    with pytest.raises(ledger.NoRoom, match="m1"):
        ledger.place(roomy, out, ledger.Request(pool="rebasers"))
    # the name a lease gave back is the next one taken
    assert ledger.place(config, out[1:], ledger.Request(pool="rebasers")).agent == "m1"


def test_a_further_slot_of_a_member_never_runs_under_a_declared_members_name():
    config = configured(
        [member("m1", capacity=3), member("m1.2")],
        [pool("rebasers", "m1", size=3), pool("critics", "m1.2")],
    )
    out = []
    for n in (1, 2, 3):
        out.append(lease_of(ledger.place(config, out, ledger.Request(pool="rebasers")), n))

    assert [ledger.agent_of(row) for row in out] == ["m1", "m1.3", "m1.4"]
    assert ledger.place(config, out, ledger.Request(pool="critics")).agent == "m1.2"
    for row in out:
        AgentPaths.resolve(ledger.agent_of(row))  # a name the agent subsystem takes


def test_a_lease_that_runs_under_another_name_records_it_and_an_older_row_reads_as_the_member():
    config = configured([member("m1", capacity=2)], [pool("rebasers", "m1", size=2)])
    first = ledger.place(config, [], ledger.Request(pool="rebasers"))
    second = ledger.place(config, [lease_of(first, 1)], ledger.Request(pool="rebasers"))

    # under the member's own name the row is as it always was
    assert kinds(first) == {("pool", "rebasers"), ("member", "m1")}
    assert kinds(second) == {("pool", "rebasers"), ("member", "m1"), ("agent", "m1.2")}
    assert ledger.agent_of(lease_of(first, 1)) == "m1"


def test_the_count_is_of_what_the_rows_say_they_took_and_of_active_rows_only():
    config = configured([member("m1")], [pool("rebasers", "m1"), pool("critics", "m1")])
    placed = ledger.place(config, [], ledger.Request(pool="rebasers"))
    ended = lease_of(placed, 1, state="released")
    # a row whose columns name another member, and whose resources name m1
    odd = dataclasses.replace(lease_of(placed, 2), member="elsewhere", pool="elsewhere")

    assert ledger.place(config, [ended], ledger.Request(pool="critics")).member.name == "m1"
    with pytest.raises(ledger.NoRoom):
        ledger.place(config, [odd], ledger.Request(pool="critics"))


def test_a_request_for_a_pool_that_is_not_declared_does_not_fit():
    config = configured([member("m1")], [pool("rebasers", "m1")])

    with pytest.raises(ledger.NoRoom, match="reviewers"):
        ledger.place(config, [], ledger.Request(pool="reviewers"))


# -- the service: two pools on one member -------------------------------------


def shared(pool_rig, *, capacity: int = 1, size: int = 1) -> PoolConfig:
    """Two pools, "rebasers" and "critics", that list the one member local-1."""
    local = dataclasses.replace(pool_rig.local_member(), capacity=capacity)
    config = pool_rig.config(local, size=size)
    (rebasers,) = config.pools
    critics = dataclasses.replace(rebasers, name="critics")
    return dataclasses.replace(config, pools=(rebasers, critics))


def waiting_ids(pool_rig) -> list[str]:
    with pool_rig.store() as store:
        return [row.id for row in store.list_waiting()]


@pytest.mark.parametrize("projects", ["one project", "two projects"])
def test_a_lease_in_one_pool_makes_a_request_to_the_other_wait_for_the_shared_member(
    pool_rig, projects
):
    svc = pool_rig.service(shared(pool_rig))
    other = pool_rig.work
    if projects == "two projects":
        other = pool_rig.tmp_path / "another-project"
        other.mkdir()
    first = svc.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
    asked = waiting.Waiting(
        svc, "critics", holder_label="orchestrator-b", cwd=other, wait=3600,
        mint_id=lambda: "request-b",
    )

    # "critics" has a slot free: none of its leases is out
    with pool_rig.store() as store:
        assert store.list_leases(state="active", pool="critics") == []
    assert asked.attempt() is None
    assert waiting_ids(pool_rig) == ["request-b"]
    assert pool_rig.pane_names() == ["local-1"]

    svc.end_lease(first.lease_id, "released")
    granted = asked.attempt()

    assert granted.agent == "local-1"
    assert waiting_ids(pool_rig) == []
    with pool_rig.store() as store:
        row = store.get_lease(granted.lease_id)
    assert (row.pool, row.member, row.state) == ("critics", "local-1", "active")
    assert pool_rig.recorded()["cwd"] == str(other)
    svc.end_lease(granted.lease_id, "released")


def test_the_refusal_names_the_member_the_other_pool_holds(pool_rig):
    svc = pool_rig.service(shared(pool_rig))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember, match="critics") as refused:
        svc.grant("critics", holder_label="b", cwd=pool_rig.work)

    assert "local-1" in str(refused.value)
    svc.end_lease(first.lease_id, "released")


def test_a_member_of_capacity_2_serves_two_leases_as_two_agents_ended_one_by_one(pool_rig):
    svc = pool_rig.service(shared(pool_rig, capacity=2))

    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = svc.grant("critics", holder_label="b", cwd=pool_rig.work)

    assert (first.agent, second.agent) == ("local-1", "local-1.2")
    statuses = [pool_rig.status(grant.agent) for grant in (first, second)]
    assert statuses[0]["pi_pid"] != statuses[1]["pi_pid"]
    assert all(pid_alive(status["pi_pid"]) for status in statuses)
    with pool_rig.store() as store:
        row = store.get_lease(second.lease_id)
    assert row.member == "local-1"
    assert ("agent", "local-1.2") in {(r.kind, r.name) for r in row.resources}
    # the member is full now, in whichever pool
    with pytest.raises(service.NoFreeMember):
        svc.grant("critics", holder_label="c", cwd=pool_rig.work)

    svc.end_lease(second.lease_id, "released")

    assert wait_until(lambda: not pid_alive(statuses[1]["host_pid"]))
    assert pid_alive(statuses[0]["pi_pid"])
    assert pool_rig.pane_names() == ["local-1"]
    svc.end_lease(first.lease_id, "released")
    assert wait_until(lambda: not pid_alive(statuses[0]["host_pid"]))


def test_a_pool_of_size_1_on_a_member_of_capacity_2_grants_one_lease(pool_rig):
    local = dataclasses.replace(pool_rig.local_member(), capacity=2)
    svc = pool_rig.service(pool_rig.config(local, size=1))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember, match="pool 'rebasers' is full"):
        svc.grant("rebasers", holder_label="b", cwd=pool_rig.work)

    with pool_rig.store() as store:
        assert len(store.list_leases(state="active", pool="rebasers")) == 1
    svc.end_lease(first.lease_id, "released")


def test_an_idle_lease_on_a_further_slot_expires_by_its_own_agents_status(
    pool_rig, monkeypatch
):
    # a member's host stamps its activity with the real time
    pool_rig.clock.now = datetime.now(timezone.utc)
    svc = pool_rig.service(shared(pool_rig, capacity=2))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)
    second = svc.grant("critics", holder_label="b", cwd=pool_rig.work)
    asked = []
    real_status = service.runner.status

    def noted(name):
        asked.append(name)
        return real_status(name)

    monkeypatch.setattr(service.runner, "status", noted)
    pool_rig.clock.advance(minutes=11)

    ended = svc.expire_due()

    assert sorted(asked) == ["local-1", "local-1.2"]
    assert sorted(e.lease_id for e in ended) == sorted([first.lease_id, second.lease_id])
    assert pool_rig.pane_names() == []


# -- structure ----------------------------------------------------------------


def ledger_calls(function: ast.AST) -> list[str]:
    """The functions of the ledger module that *function* calls, one entry per call."""
    return [
        node.func.attr for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "ledger"
    ]


def test_the_service_asks_one_ledger_function_whether_a_request_fits():
    tree = ast.parse((SRC / "pool" / "service.py").read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    asking = {name: ledger_calls(node) for name, node in functions.items()}
    fit_calls = {name: calls for name, calls in asking.items() if "place" in calls}

    # one call in the whole module, which the grant and the waiting order share
    assert list(fit_calls) == ["_place"]
    assert fit_calls["_place"].count("place") == 1
    # and the only other thing asked of the ledger is a lease's agent name
    assert {call for calls in asking.values() for call in calls} <= {
        "place", "Request", "agent_of", "held",
    }
    # the service counts nothing itself: it reads the active leases to hand
    # them over, to the placement and to the models the bridge allows, and to
    # check expiry
    readers = {
        name for name, node in functions.items()
        if any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "list_leases" for n in ast.walk(node)
        )
    }
    assert readers == {"_place", "expire_due", "held_models"}
    source = ast.unparse(tree)
    assert ".capacity" not in source and ".size" not in source


def test_the_ledger_reads_no_store_no_clock_and_no_file():
    tree = ast.parse((SRC / "pool" / "ledger.py").read_text(encoding="utf-8"))
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert not called & {
        "now", "utcnow", "time", "monotonic", "open", "read_text", "write_text",
        "list_leases", "get_lease", "add_lease", "open_pool_store", "connect",
    }
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    # the rows' and the configuration's types, and nothing that does anything
    assert modules <= {
        "__future__", "collections.abc", "dataclasses", "itertools",
        "..daemon.poolstore", "..errors", ".config",
    }


def test_nothing_in_the_ledger_runs_by_itself():
    source = (SRC / "pool" / "ledger.py").read_text(encoding="utf-8")
    assert "threading" not in source and "asyncio" not in source


SHARES = ("quota", "reserv", "orchestrator", "holder", "project", "owner", "priority")


def test_no_field_sets_a_share_aside_for_an_orchestrator():
    schema = {
        *pool_config.SECTIONS, *pool_config.ACCOUNT_FIELDS, *pool_config.MEMBER_FIELDS,
        *pool_config.POOL_FIELDS, *teams.FIELDS, *teams.ROLE_FIELDS,
    }
    for record in (
        pool_config.Account, pool_config.Member, pool_config.Pool, pool_config.PoolConfig,
        teams.Team, teams.Role, ledger.Request,
    ):
        schema.update(f.name for f in dataclasses.fields(record))
    # what the ledger is asked with: the request says which pool and not who asks
    schema.update(inspect.signature(ledger.place).parameters)

    assert {"size", "capacity", "pool"} <= schema  # the walk reaches the fields
    assert [name for name in schema if any(word in name.lower() for word in SHARES)] == []
