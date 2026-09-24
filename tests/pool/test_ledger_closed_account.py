"""A closed account serves no new lease: its members are passed over, and a
request that only they could serve is refused at once.

The pool closes an account when its provider has nothing left to give it
until a reset, and the store keeps the reopen time. A member on a closed
account is not free however idle it is, so the ledger passes it over for the
next member the pool lists: one on another account, or a local one. When
every member the request accepts runs through a closed account, no lease
ending would make room. That request is refused for good, with the account
and its reopen time named, and it never waits. A member of an egress class
the request does not accept is not one that could serve it, so it keeps no
such request waiting either.

A member the request accepts that is only busy is still waited for, beside
one on a closed account. Closing an account ends nothing: a lease granted
before the close stays out, and its holder keeps the member.

The ledger is handed the closed accounts as it is handed the rows: which are
closed now, and when each reopens. The service works that out from the store
and its own clock, so an account is open again once its reopen time has come.

The ledger's own tests hand it rows and read its answer. The service's tests
run real processes: the stub pi under an agent host, with a made-up value in
the account's key variable, so nothing goes over a network.
"""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from specflo.cli import app
from specflo.pool import cli_admin, ledger, service, waiting
from specflo.pool import config as pool_config
from specflo.pool.config import PoolConfig

from . import test_lease_request
from .test_lease_request import checkout, pool_daemon, runner  # noqa: F401 - fixtures
from .test_ledger_accounts import (
    KEY_ENV,
    account,
    configured,
    hosted,
    kinds,
    lease_of,
    local,
    pool,
    waiting_ids,
)

REOPEN = "2026-03-02T00:00:00.000+00:00"
SHUT = {"shared": REOPEN}


def one_account_pool() -> PoolConfig:
    """One pool, "critics", whose two members both run through the account "shared"."""
    return configured(
        [account(cap=2)], [hosted("h1"), hosted("h2")], [pool("critics", "h1", "h2")]
    )


# -- the ledger: rows and closed accounts in, an answer out -------------------


def test_a_pool_served_only_by_a_closed_account_is_refused_naming_it_and_its_reopen_time():
    config = one_account_pool()

    with pytest.raises(ledger.AccountClosed) as refused:
        ledger.place(config, [], ledger.Request(pool="critics"), closed=SHUT)

    message = str(refused.value)
    assert "critics" in message and "account 'shared'" in message and REOPEN in message
    # it is not the refusal a request waits on
    assert not isinstance(refused.value, ledger.NoRoom)
    # and with nothing closed the same request fits
    assert ledger.place(config, [], ledger.Request(pool="critics")).member.name == "h1"
    assert ledger.place(config, [], ledger.Request(pool="critics"), closed={}).member.name == "h1"


def test_a_member_on_a_closed_account_is_passed_over_for_one_on_another_account():
    config = configured(
        [account(), account("spare")],
        [hosted("h1"), hosted("h2", on="spare")],
        [pool("critics", "h1", "h2")],
    )

    placed = ledger.place(config, [], ledger.Request(pool="critics"), closed=SHUT)

    assert placed.member.name == "h2"
    assert kinds(placed) == {("pool", "critics"), ("member", "h2"), ("account", "spare")}


def test_a_member_on_a_closed_account_is_passed_over_for_a_local_member():
    config = configured(
        [account()], [hosted("h1"), local("m1")], [pool("critics", "h1", "m1")]
    )

    placed = ledger.place(config, [], ledger.Request(pool="critics"), closed=SHUT)

    assert placed.member.name == "m1"


def test_an_account_closed_with_no_reopen_time_is_refused_saying_so():
    with pytest.raises(ledger.AccountClosed, match="account 'shared'") as refused:
        ledger.place(
            one_account_pool(), [], ledger.Request(pool="critics"), closed={"shared": None}
        )

    assert "no time" in str(refused.value)


def test_every_closed_account_that_keeps_the_request_is_named():
    config = configured(
        [account(), account("spare")],
        [hosted("h1"), hosted("h2", on="spare")],
        [pool("critics", "h1", "h2")],
    )
    later = "2026-03-09T00:00:00.000+00:00"

    with pytest.raises(ledger.AccountClosed) as refused:
        ledger.place(
            config, [], ledger.Request(pool="critics"),
            closed={"shared": REOPEN, "spare": later},
        )

    message = str(refused.value)
    assert "account 'shared'" in message and REOPEN in message
    assert "account 'spare'" in message and later in message


def test_a_closed_account_no_member_of_the_pool_runs_through_changes_nothing():
    placed = ledger.place(
        one_account_pool(), [], ledger.Request(pool="critics"), closed={"elsewhere": REOPEN}
    )

    assert placed.member.name == "h1"


def test_a_member_of_a_class_the_request_does_not_accept_is_not_one_that_could_serve_it():
    # the open member's account is open and the member is free; the request may not have it
    open_member = dataclasses.replace(hosted("o1", on="spare"), egress="open")
    config = configured(
        [account(), account("spare")], [hosted("h1"), open_member],
        [pool("critics", "h1", "o1")],
    )
    no_train = ledger.Request(pool="critics", egress=("local", "no-train"))

    with pytest.raises(ledger.AccountClosed, match="account 'shared'") as refused:
        ledger.place(config, [], no_train, closed=SHUT)

    assert "spare" not in str(refused.value)
    # and a pool with no member of an accepted class is still refused for its classes
    with pytest.raises(ledger.NoMemberAllowed):
        ledger.place(config, [], ledger.Request(pool="critics", egress=("local",)), closed=SHUT)
    # a request that accepts the open member gets it
    anything = ledger.Request(pool="critics", egress=("local", "no-train", "open"))
    assert ledger.place(config, [], anything, closed=SHUT).member.name == "o1"


def test_a_busy_member_is_waited_for_beside_one_on_a_closed_account():
    config = configured(
        [account(), account("spare")],
        [hosted("h1"), hosted("h2", on="spare")],
        [pool("critics", "h1", "h2"), pool("rebasers", "h2")],
    )
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    # h2 is out on a lease that will end: the request waits for it
    with pytest.raises(ledger.NoRoom) as full:
        ledger.place(config, out, ledger.Request(pool="critics"), closed=SHUT)

    # and the refusal says what keeps the other member
    assert "account 'shared'" in str(full.value) and REOPEN in str(full.value)


def test_a_full_open_account_is_waited_for_beside_a_closed_one():
    config = configured(
        [account(), account("spare")],
        [hosted("h1"), hosted("h2", on="spare"), hosted("h3", on="spare")],
        [pool("critics", "h1", "h2"), pool("rebasers", "h3")],
    )
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="rebasers")), 1)]

    with pytest.raises(ledger.NoRoom, match="account 'spare' is full") as full:
        ledger.place(config, out, ledger.Request(pool="critics"), closed=SHUT)

    assert "account 'shared'" in str(full.value) and REOPEN in str(full.value)


def test_the_closed_account_is_told_before_any_count_of_what_is_full():
    config = configured([account(cap=2)], [hosted("h1")], [pool("critics", "h1", size=1)])
    # granted before the close, and it fills the pool and the member
    out = [lease_of(ledger.place(config, [], ledger.Request(pool="critics")), 1)]

    with pytest.raises(ledger.NoRoom):
        ledger.place(config, out, ledger.Request(pool="critics"))
    # no lease ending makes room while the account is closed
    with pytest.raises(ledger.AccountClosed, match="account 'shared'"):
        ledger.place(config, out, ledger.Request(pool="critics"), closed=SHUT)


# -- the service: the closed accounts come from the store and the clock -------


def hosted_pools(pool_rig, monkeypatch) -> PoolConfig:
    """Two pools of two hosted members each. "critics" runs through the
    account "shared" only; "rebasers" has one member on "shared" and one on
    the account "spare"."""
    # a made-up value: the stub pi stands where pi stands and calls no provider
    monkeypatch.setenv(KEY_ENV, "not-a-real-key")
    on = {1: "shared", 2: "spare", 3: "shared", 4: "shared"}
    members = [
        dataclasses.replace(pool_rig.hosted_member(), name=f"hosted-{n}", account=on[n])
        for n in (1, 2, 3, 4)
    ]
    config = pool_rig.config(*members[:2])
    (rebasers,) = config.pools
    critics = dataclasses.replace(rebasers, name="critics", members=("hosted-3", "hosted-4"))
    return dataclasses.replace(
        config, accounts=(*config.accounts, account(cap=4), account("spare", cap=4)),
        members=tuple(members), pools=(rebasers, critics),
    )


def close(pool_rig, name: str = "shared", reopen: str | None = REOPEN) -> None:
    with pool_rig.store() as store:
        store.set_account_closed(name, reopen=reopen)


def test_a_request_only_a_closed_account_can_serve_is_refused_naming_it_and_its_reopen_time(
    pool_rig, monkeypatch
):
    svc = pool_rig.service(hosted_pools(pool_rig, monkeypatch))
    close(pool_rig)

    with pytest.raises(service.ClosedAccount) as refused:
        svc.grant("critics", holder_label="a", cwd=pool_rig.work)

    message = str(refused.value)
    assert "critics" in message and "account 'shared'" in message and REOPEN in message
    assert not isinstance(refused.value, service.NoFreeMember)
    with pool_rig.store() as store:
        assert store.list_leases() == []
    assert pool_rig.pane_names() == []


def test_the_refused_request_never_waits_however_long_it_may(pool_rig, monkeypatch):
    svc = pool_rig.service(hosted_pools(pool_rig, monkeypatch))
    close(pool_rig)
    asked = waiting.Waiting(
        svc, "critics", holder_label="a", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-a",
    )

    with pytest.raises(service.ClosedAccount, match="account 'shared'"):
        asked.attempt()

    assert waiting_ids(pool_rig) == []
    asked.leave()
    assert pool_rig.pane_names() == []


def test_a_pool_with_a_member_on_another_account_still_grants(pool_rig, monkeypatch):
    svc = pool_rig.service(hosted_pools(pool_rig, monkeypatch))
    close(pool_rig)

    granted = svc.grant("rebasers", holder_label="a", cwd=pool_rig.work)

    # hosted-1 is listed first and free, and its account is closed
    assert granted.agent == "hosted-2"
    with pool_rig.store() as store:
        row = store.get_lease(granted.lease_id)
    assert {(r.kind, r.name) for r in row.resources} == {
        ("pool", "rebasers"), ("member", "hosted-2"), ("account", "spare"),
    }
    svc.end_lease(granted.lease_id, "released")


def test_a_lease_granted_before_the_close_stays_active(pool_rig, monkeypatch):
    svc = pool_rig.service(hosted_pools(pool_rig, monkeypatch))
    before = svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    assert before.agent == "hosted-3"

    close(pool_rig)
    with pytest.raises(service.ClosedAccount):
        svc.grant("critics", holder_label="b", cwd=pool_rig.work)
    assert svc.expire_due() == []

    with pool_rig.store() as store:
        row = store.get_lease(before.lease_id)
        assert [t.kind for t in store.list_transitions(lease_id=before.lease_id)] == ["granted"]
    assert row.state == "active"
    assert pool_rig.pane_names() == ["hosted-3"]
    # its holder ends it as any lease is ended
    assert svc.end_lease(before.lease_id, "released").state == "released"


def test_the_account_is_open_again_once_its_reopen_time_has_come(pool_rig, monkeypatch):
    svc = pool_rig.service(hosted_pools(pool_rig, monkeypatch))
    close(pool_rig)
    with pytest.raises(service.ClosedAccount):
        svc.grant("critics", holder_label="a", cwd=pool_rig.work)

    # the rig's clock starts at noon on the day before the reopen time
    pool_rig.clock.advance(hours=12)
    granted = svc.grant("critics", holder_label="a", cwd=pool_rig.work)

    assert granted.agent == "hosted-3"
    svc.end_lease(granted.lease_id, "released")


def test_a_waiting_request_its_account_closed_under_holds_up_no_other_request(
    pool_rig, monkeypatch
):
    config = hosted_pools(pool_rig, monkeypatch)
    rebasers, critics = config.pools
    config = dataclasses.replace(
        config, pools=(rebasers, dataclasses.replace(critics, members=("hosted-3",), size=1))
    )
    svc = pool_rig.service(config)
    first = svc.grant("critics", holder_label="a", cwd=pool_rig.work)
    asked = waiting.Waiting(
        svc, "critics", holder_label="b", cwd=pool_rig.work, wait=3600,
        mint_id=lambda: "request-b",
    )
    assert asked.attempt() is None
    assert waiting_ids(pool_rig) == ["request-b"]

    close(pool_rig)
    # the row that waits ahead could not be granted now, so it is passed over
    granted = svc.grant("rebasers", holder_label="c", cwd=pool_rig.work)

    assert granted.agent == "hosted-2"
    # and when it asks again it is told, and waits no more
    with pytest.raises(service.ClosedAccount, match="account 'shared'"):
        asked.attempt()
    asked.leave()
    assert waiting_ids(pool_rig) == []
    svc.end_lease(granted.lease_id, "released")
    svc.end_lease(first.lease_id, "released")


# -- the verb, asked of a real daemon -----------------------------------------

# The daemon keeps the time of day, so the account is closed until far from now.
FAR = "2999-01-01T00:00:00.000+00:00"
WRITE_POOL = test_lease_request.write_pool
ACCEPTING_HOSTED = test_lease_request.REBASER.replace("egress: local", "egress: no-train")


def write_hosted_pool(rig) -> None:
    """The lease verb's pool with its one member a hosted one, on the account "team-a"."""
    WRITE_POOL(rig)
    directory = cli_admin.pool_dir(rig.root)
    (directory / pool_config.DEFINITIONS_DIR / "rebaser.md").write_text(
        ACCEPTING_HOSTED, encoding="utf-8"
    )
    path = directory / pool_config.POOL_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["accounts"] = [{"name": "team-a", "cap": 2, "key_env": "TEAM_A_KEY"}]
    data["members"] = [{
        "name": "hosted-1", "command": f"{rig.command} --model some-vendor/some-model", "backing": "hosted",
        "model": "some-vendor/some-model", "account": "team-a", "labels": [],
        "capacity": 1, "egress": "no-train",
    }]
    data["pools"][0].update(members=["hosted-1"], size=1)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.fixture
def hosted_pool(monkeypatch):
    """Asked for before the daemon: the daemon's fixture writes this pool instead."""
    monkeypatch.setattr(test_lease_request, "write_pool", write_hosted_pool)


def test_the_verb_exits_non_zero_naming_the_account_and_its_reopen_time(
    hosted_pool, checkout, pool_rig  # noqa: F811
):
    close(pool_rig, "team-a", FAR)

    refused = runner.invoke(app, ["lease", "request", "rebasers"])

    assert refused.exit_code != 0
    assert "team-a" in refused.output and FAR in refused.output
    assert waiting_ids(pool_rig) == []
    with pool_rig.store() as store:
        assert store.list_leases() == []
    assert pool_rig.pane_names() == []
