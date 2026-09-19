"""A provider account is one more thing the ledger counts: one slot a lease, across pools.

A lease on a hosted member runs through the member's account, so it takes a
slot of the account besides the slot of its pool and of its member, and the
lease row says so. The account's ``cap`` is how many leases run through it at
once, in whichever pools: two pools whose members share an account share its
slots, and a request to a pool with room of its own waits while the account
is full. A local member runs through no account and takes no slot of one.

It is the same function that answers, from the same rows. A member whose
account is full is passed over for the next one the pool lists that fits, and
when none fits the answer names the account that is full.

The ledger's own tests hand it rows and read its answer. The service's tests
run real processes: the stub pi under an agent host, with a made-up value in
the account's key variable, so nothing goes over a network.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from specflo.daemon.poolstore import Lease
from specflo.pool import ledger, service, waiting
from specflo.pool.config import Account, Member, Pool, PoolConfig

KEY_ENV = "SHARED_ACCOUNT_KEY"


def account(name: str = "shared", cap: int = 1) -> Account:
    return Account(name=name, cap=cap, key_env=KEY_ENV)


def hosted(name: str, on: str = "shared") -> Member:
    return Member(
        name=name, command="pi", backing="hosted", labels=(), capacity=1,
        egress="no-train", model="some-vendor/some-model", account=on,
    )


def local(name: str) -> Member:
    return Member(
        name=name, command="pi", backing="local", labels=(), capacity=1,
        egress="local", model="tc3",
    )


def pool(name: str, *members: str, size: int = 2) -> Pool:
    return Pool(
        name=name, definition="rebaser", members=members, size=size,
        idle_default=600, idle_max=3600,
    )


def configured(accounts: list[Account], members: list[Member], pools: list[Pool]) -> PoolConfig:
    return PoolConfig(
        path=Path("pool.yaml"), accounts=tuple(accounts), members=tuple(members),
        pools=tuple(pools),
    )


def two_hosted_pools(cap: int = 1) -> PoolConfig:
    """Two pools of size 2, two hosted members each, all four on the one account."""
    return configured(
        [account(cap=cap)],
        [hosted("h1"), hosted("h2"), hosted("h3"), hosted("h4")],
        [pool("rebasers", "h1", "h2"), pool("critics", "h3", "h4")],
    )


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


def test_a_lease_on_a_hosted_member_takes_a_slot_of_its_account_and_a_local_one_takes_none():
    config = configured(
        [account()], [hosted("h1"), local("m1")],
        [pool("rebasers", "h1"), pool("critics", "m1")],
    )

    on_hosted = ledger.place(config, [], ledger.Request(pool="rebasers"))
    on_local = ledger.place(config, [], ledger.Request(pool="critics"))

    assert kinds(on_hosted) == {("pool", "rebasers"), ("member", "h1"), ("account", "shared")}
    assert kinds(on_local) == {("pool", "critics"), ("member", "m1")}


def test_a_lease_in_one_pool_fills_the_account_the_other_pool_runs_through():
    config = two_hosted_pools()
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    # "critics" has granted nothing and both its members are free
    with pytest.raises(ledger.NoRoom, match="account 'shared'") as refused:
        ledger.place(config, out, ledger.Request(pool="critics"))

    assert "critics" in str(refused.value)
    # nor does the first pool's other member fit, on the same account
    with pytest.raises(ledger.NoRoom, match="account 'shared'"):
        ledger.place(config, out, ledger.Request(pool="rebasers"))
    assert ledger.place(config, [], ledger.Request(pool="critics")).member.name == "h3"


def test_an_account_serves_as_many_leases_as_its_cap():
    config = two_hosted_pools(cap=2)
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    second = ledger.place(config, out, ledger.Request(pool="critics"))
    out.append(lease_of(second, 2))

    assert ("account", "shared") in kinds(second)
    with pytest.raises(ledger.NoRoom, match="account 'shared'"):
        ledger.place(config, out, ledger.Request(pool="rebasers"))


def test_a_member_whose_account_is_full_is_passed_over_for_a_listed_member_that_fits():
    config = configured(
        [account(), account("spare")],
        [hosted("h1"), hosted("h2"), hosted("h3", on="spare"), local("m1")],
        [pool("rebasers", "h1"), pool("critics", "h2", "h3", "m1", size=3)],
    )
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    # h2 is free, and its account is not
    on_spare = ledger.place(config, out, ledger.Request(pool="critics"))
    out.append(lease_of(on_spare, 2))
    on_local = ledger.place(config, out, ledger.Request(pool="critics"))
    out.append(lease_of(on_local, 3))

    assert on_spare.member.name == "h3"
    assert kinds(on_spare) == {("pool", "critics"), ("member", "h3"), ("account", "spare")}
    assert on_local.member.name == "m1"
    # what is left of "critics" is h2, and what keeps it is the account
    roomy = dataclasses.replace(
        config, pools=(config.pools[0], pool("critics", "h2", "h3", "m1", size=4))
    )
    with pytest.raises(ledger.NoRoom, match="account 'shared'"):
        ledger.place(roomy, out, ledger.Request(pool="critics"))


def test_the_account_is_counted_from_what_the_active_rows_say_they_took():
    config = two_hosted_pools()
    placed = ledger.place(config, [], ledger.Request(pool="rebasers"))
    ended = lease_of(placed, 1, state="released")
    # a row whose columns name nothing declared, and whose resources name the account
    odd = dataclasses.replace(lease_of(placed, 2), member="elsewhere", pool="elsewhere")

    assert ledger.place(config, [ended], ledger.Request(pool="critics")).member.name == "h3"
    with pytest.raises(ledger.NoRoom, match="account 'shared'"):
        ledger.place(config, [odd], ledger.Request(pool="critics"))


def test_a_full_member_is_not_put_down_to_its_account():
    config = configured(
        [account(cap=5)], [hosted("h1")], [pool("rebasers", "h1"), pool("critics", "h1")]
    )
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    with pytest.raises(ledger.NoRoom, match="h1") as refused:
        ledger.place(config, out, ledger.Request(pool="critics"))

    assert "account" not in str(refused.value)


# -- the service: two hosted pools on one account -----------------------------


def shared_account(pool_rig, monkeypatch, *, cap: int = 1) -> PoolConfig:
    """Two pools of size 2, "rebasers" and "critics", of two hosted members
    each, all four on the account "shared"."""
    # a made-up value: the stub pi stands where pi stands and calls no provider
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    members = [
        dataclasses.replace(pool_rig.hosted_member(), name=f"hosted-{n}", account="shared")
        for n in (1, 2, 3, 4)
    ]
    config = pool_rig.config(*members[:2])
    (rebasers,) = config.pools
    critics = dataclasses.replace(rebasers, name="critics", members=("hosted-3", "hosted-4"))
    return dataclasses.replace(
        config, accounts=(*config.accounts, account(cap=cap)), members=tuple(members),
        pools=(rebasers, critics),
    )


def waiting_ids(pool_rig) -> list[str]:
    with pool_rig.store() as store:
        return [row.id for row in store.list_waiting()]


def test_a_lease_in_the_first_pool_makes_a_request_to_the_second_wait_for_the_account(
    pool_rig, monkeypatch
):
    config = shared_account(pool_rig, monkeypatch)
    assert [p.size for p in config.pools] == [2, 2]
    svc = pool_rig.service(config)
    first = svc.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
    asked = waiting.Waiting(
        svc, "critics", holder_label="orchestrator-b", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-b",
    )

    # "critics" has both its slots and both its members free
    with pool_rig.store() as store:
        assert store.list_leases(state="active", pool="critics") == []
    assert asked.attempt() is None
    assert waiting_ids(pool_rig) == ["request-b"]
    assert pool_rig.pane_names() == ["hosted-1"]

    svc.end_lease(first.lease_id, "released")
    granted = asked.attempt()

    assert granted.agent == "hosted-3"
    assert waiting_ids(pool_rig) == []
    with pool_rig.store() as store:
        row = store.get_lease(granted.lease_id)
    assert (row.pool, row.member, row.state) == ("critics", "hosted-3", "active")
    svc.end_lease(granted.lease_id, "released")


def test_a_lease_rows_resources_name_the_account_slot_it_took(pool_rig, monkeypatch):
    svc = pool_rig.service(shared_account(pool_rig, monkeypatch))

    granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pool_rig.store() as store:
        row = store.get_lease(granted.lease_id)
    assert {(r.kind, r.name) for r in row.resources} == {
        ("pool", "rebasers"), ("member", "hosted-1"), ("account", "shared"),
    }
    svc.end_lease(granted.lease_id, "released")


def test_the_refusal_names_the_account_that_is_full(pool_rig, monkeypatch):
    svc = pool_rig.service(shared_account(pool_rig, monkeypatch))
    first = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    with pytest.raises(service.NoFreeMember, match="account 'shared'") as refused:
        svc.grant("critics", holder_label="b", cwd=pool_rig.work)

    assert "critics" in str(refused.value)
    svc.end_lease(first.lease_id, "released")
