"""The accounts' key figures, read by the daemon while it serves.

A daemon whose pool declares an account reads every declared account's key
once when it starts to serve and again at a fixed interval, through
``accounts.read_accounts`` and so through the provider's key read and nothing
else. An account whose daily free requests are spent reads closed with no
member's call having been refused: a request that only it can serve is refused
at once, and the pool page shows the figures and the closed state. A provider
out of reach leaves a read error, closes nothing and does not end the reading.

The provider is the fake one, the clock is the pool tests' fake one and the
reader's sleep is the test's: nothing here opens a connection or waits out
the interval.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import shutil
import threading
from datetime import timedelta

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from specflo.daemon import HEALTH_PATH, auth, pool_routes, web
from specflo.daemon import app as daemon_app
from specflo.daemon.poolstore import open_pool_store
from specflo.pool import accounts, cli_admin
from specflo.pool import config as pool_config
from specflo.pool.config import Account
from specflo.pool.service import ClosedAccount

from .conftest import START
from .stub_provider import KEY_READ_PATH, Seen, StubProvider
from .test_cli_validate import _write_three_faults
from .test_events_reloads import write_pool

KEY_A = "key-of-team-a"
KEY_B = "key-of-team-b"
ENVIRON = {"TEAM_A_KEY": KEY_A, "TEAM_B_KEY": KEY_B}
RESET = "2026-03-02T00:00:00.000+00:00"


@pytest.fixture
def provider():
    stub = StubProvider()
    stub.set_key(KEY_A, usage=12.5, limit=20.0, limit_remaining=7.5, free_used=3)
    stub.set_key(KEY_B, usage=1.0, free_used=0)
    return stub


def serving(pool_rig, *members):
    """The pool service a daemon would hold, with the accounts' keys in its environment."""
    service = pool_rig.service(pool_rig.config(*(members or (pool_rig.hosted_member(),))))
    service.environ = ENVIRON
    return service


class Stop(Exception):
    """Raised by a test's sleep to end the reader's loop."""


class Sleeps:
    """The reader's sleep: keeps each delay asked for, moves the clock by it,
    does what the test wants done between two reads, and ends the loop at the
    last one."""

    def __init__(self, clock, last: int, then=lambda: None) -> None:
        self.clock, self.last, self.then = clock, last, then
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.last:
            raise Stop
        self.clock.advance(seconds=delay)
        self.then()


def run(reader) -> None:
    with pytest.raises(Stop):
        asyncio.run(reader.run())


def account(pool_rig, name: str = "team-a"):
    with pool_rig.store() as store:
        return store.get_account(name)


# -- one read ---------------------------------------------------------------


def test_one_read_stores_every_declared_accounts_figures_through_the_key_read_alone(
    pool_rig, provider
):
    reader = accounts.AccountsReader(serving(pool_rig), client=provider.client())

    reader.read()

    first, second = account(pool_rig, "team-a"), account(pool_rig, "team-b")
    assert (first.figures.usage, first.figures.limit, first.figures.remaining) == (12.5, 20.0, 7.5)
    assert (first.figures.free_requests, first.figures.read_error) == (47, None)
    assert (second.figures.usage, second.figures.free_requests) == (1.0, 50)
    assert (first.closed, second.closed) == (False, False)
    assert provider.seen == [
        Seen("GET", KEY_READ_PATH, f"Bearer {KEY_A}"),
        Seen("GET", KEY_READ_PATH, f"Bearer {KEY_B}"),
    ]


# -- the loop ---------------------------------------------------------------


def test_the_interval_is_a_named_constant_of_five_minutes_or_more():
    assert accounts.READ_INTERVAL >= 5 * 60


def test_the_reader_reads_at_once_and_again_after_each_interval(pool_rig, provider):
    sleeps = Sleeps(pool_rig.clock, 3)
    reader = accounts.AccountsReader(serving(pool_rig), client=provider.client(), sleep=sleeps)

    run(reader)

    assert sleeps.delays == [accounts.READ_INTERVAL] * 3
    # one read before the first wait and one after each wait that ended
    assert [seen.authorization for seen in provider.seen] == [
        f"Bearer {KEY_A}", f"Bearer {KEY_B}",
    ] * 3
    assert {(seen.method, seen.path) for seen in provider.seen} == {("GET", KEY_READ_PATH)}
    # the time of a read is the fake clock's, which only the waits moved
    last_read = START + timedelta(seconds=2 * accounts.READ_INTERVAL)
    assert account(pool_rig).figures.read_at == last_read.isoformat(timespec="milliseconds")


def test_spent_free_requests_close_the_account_and_its_pool_refuses_at_once(pool_rig, provider):
    service = serving(pool_rig)

    def spent() -> None:
        provider.set_key(KEY_A, free_limit=50, free_used=50)

    sleeps = Sleeps(pool_rig.clock, 2, then=spent)
    reader = accounts.AccountsReader(service, client=provider.client(), sleep=sleeps)

    run(reader)

    closed = account(pool_rig)
    assert (closed.closed, closed.reopen, closed.figures.free_requests) == (True, RESET, 0)
    assert account(pool_rig, "team-b").closed is False
    with pytest.raises(ClosedAccount) as refused:
        service.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
    assert "account 'team-a'" in str(refused.value) and RESET in str(refused.value)
    with pool_rig.store() as store:
        assert store.list_leases() == [] and store.list_waiting() == []


def test_a_provider_out_of_reach_leaves_a_read_error_closes_nothing_and_is_read_again(
    pool_rig, provider
):
    provider.unreachable = True

    def back() -> None:
        provider.unreachable = False

    seen: list[tuple] = []

    def note() -> None:
        record = account(pool_rig)
        seen.append((record.closed, record.figures.usage, record.figures.read_error))

    sleeps = Sleeps(pool_rig.clock, 2, then=lambda: (note(), back()))
    reader = accounts.AccountsReader(serving(pool_rig), client=provider.client(), sleep=sleeps)

    run(reader)

    assert len(seen) == 1
    closed, usage, error = seen[0]
    assert (closed, usage) == (False, None) and error.startswith("provider unreachable")
    assert KEY_A not in error
    after = account(pool_rig)
    assert (after.closed, after.figures.usage, after.figures.read_error) == (False, 12.5, None)


def test_a_read_that_fails_is_said_and_the_next_one_is_made_all_the_same(
    pool_rig, provider, monkeypatch, caplog
):
    reader = accounts.AccountsReader(serving(pool_rig), client=provider.client())
    reads: list[int] = []

    def read():
        reads.append(len(reads))
        raise OSError("the store is locked")

    monkeypatch.setattr(reader, "read", read)
    reader.sleep = Sleeps(pool_rig.clock, 2)

    with caplog.at_level(logging.WARNING, logger=accounts.__name__):
        run(reader)

    assert reads == [0, 1]
    assert [r.getMessage() for r in caplog.records].count(
        "the pool's read of its accounts' keys: the store is locked"
    ) == 2


def test_each_read_is_of_the_accounts_the_configuration_in_force_declares(pool_rig, provider):
    service = serving(pool_rig)
    both = service.config
    service.config = dataclasses.replace(
        both, accounts=(Account(name="team-a", cap=2, key_env="TEAM_A_KEY"),)
    )
    # a reload that stood puts the new configuration in force between two reads
    sleeps = Sleeps(pool_rig.clock, 2, then=lambda: service.swap(both, lambda *_: ()))
    reader = accounts.AccountsReader(service, client=provider.client(), sleep=sleeps)

    run(reader)

    assert [seen.authorization for seen in provider.seen] == [
        f"Bearer {KEY_A}", f"Bearer {KEY_A}", f"Bearer {KEY_B}",
    ]


def test_no_grant_waits_on_the_provider(pool_rig, provider):
    # The provider holds the key read until the grant is through: a read made
    # on the event loop, or under a lock of the store, would never let it be.
    asked, granted = threading.Event(), threading.Event()
    answer = provider._answer

    def slow(request: httpx.Request) -> httpx.Response:
        asked.set()
        assert granted.wait(5), "the grant waited on the provider"
        return answer(request)

    client = httpx.Client(transport=httpx.MockTransport(slow), base_url="https://provider.invalid")
    service = serving(pool_rig, pool_rig.local_member())
    reader = accounts.AccountsReader(service, client=client, sleep=Sleeps(pool_rig.clock, 1))

    async def serve() -> None:
        task = asyncio.create_task(reader.run())
        deadline = 500
        while not asked.is_set() and (deadline := deadline - 1):
            await asyncio.sleep(0.01)
        assert asked.is_set(), "the reader never read"
        grant = service.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)
        assert grant.agent == "local-1"
        granted.set()
        await task

    with pytest.raises(Stop):
        asyncio.run(serve())

    assert account(pool_rig).figures.usage == 12.5


def test_a_cancelled_reader_stops(pool_rig, provider):
    reader = accounts.AccountsReader(serving(pool_rig), client=provider.client())

    async def serve() -> None:
        task = asyncio.create_task(reader.run())
        deadline = 500
        while account(pool_rig, "team-b") is None and (deadline := deadline - 1):
            await asyncio.sleep(0.01)
        assert account(pool_rig, "team-b") is not None, "the reader never read"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(serve())

    assert len(provider.seen) == 2


# -- a configuration put in force later -------------------------------------


class Pools:
    """What the daemon hands a reader in place of the pool it was made for:
    the pool in force now, none until a test puts one there."""

    def __init__(self, pool=None) -> None:
        self.pool = pool

    def __call__(self):
        return self.pool


@pytest.mark.parametrize("at_start", ["no pool", "no account"])
def test_a_reader_with_no_key_to_read_reads_soon_after_a_reload_brings_the_first_account(
    pool_rig, provider, at_start
):
    declared = pool_rig.config(pool_rig.local_member())
    pools = Pools(
        None if at_start == "no pool"
        else pool_rig.service(dataclasses.replace(declared, accounts=()))
    )
    reads: list[int] = []

    def then() -> None:
        reads.append(len(provider.seen))
        if len(reads) == 2:
            # the reload: the pool opens, or the pool that stands declares its accounts
            if pools.pool is None:
                pools.pool = pool_rig.service(declared)
            else:
                assert pools.pool.swap(declared, lambda *_: ()) == ()

    sleeps = Sleeps(pool_rig.clock, 4, then)
    reader = accounts.reader_for(pools())
    assert reader is not None, "a pool with no key to read has a reader that waits"
    reader.pool, reader.client, reader.sleep = pools, provider.client(), sleeps

    run(reader)

    # the waits with no key to read are short, so the first account is read
    # seconds after the reload; from then on the reads are a round apart
    assert accounts.IDLE_INTERVAL <= 10
    assert sleeps.delays == [
        accounts.IDLE_INTERVAL, accounts.IDLE_INTERVAL, accounts.READ_INTERVAL,
        accounts.READ_INTERVAL,
    ]
    assert reads == [0, 0, 2]
    assert account(pool_rig).figures.usage == 12.5


def test_a_reader_cancelled_while_it_has_no_key_to_read_stops(pool_rig, provider):
    waiting = asyncio.Event()

    async def sleep(delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    reader = accounts.reader_for(None)
    assert reader is not None, "a daemon with no pool has a reader that waits"
    reader.client, reader.sleep = provider.client(), sleep

    async def serve() -> None:
        task = asyncio.create_task(reader.run())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(serve())

    assert provider.seen == []


# -- which daemon reads -----------------------------------------------------


def test_the_reader_is_for_a_pool_that_declares_an_account(pool_rig):
    service = serving(pool_rig)
    none_declared = pool_rig.service(
        dataclasses.replace(pool_rig.config(pool_rig.local_member()), accounts=())
    )

    # no pool, and a pool that declares no account, have no key to read
    assert accounts.reader_for(None).read() == []
    assert accounts.reader_for(none_declared).read() == []
    with pool_rig.store() as store:
        assert store.list_accounts() == []
    reader = accounts.reader_for(service)
    assert (reader.service, reader.client) == (service, None)


def test_in_a_test_a_reader_handed_no_client_finds_no_provider(pool_rig):
    # the daemon's own reader opens its client on the provider's address; the
    # pool tests' autouse fixture has put a closed port there
    reader = accounts.reader_for(serving(pool_rig))

    reader.read()

    record = account(pool_rig)
    assert record.closed is False
    assert record.figures.read_error.startswith("provider unreachable")


class Readers:
    """In place of ``reader_for``: each pool asked about is kept, and the
    reader made reads the fake provider."""

    def __init__(self, monkeypatch, provider: StubProvider) -> None:
        self.asked: list[object] = []
        self.made: list[accounts.AccountsReader] = []
        self.provider = provider
        self._make = accounts.reader_for
        monkeypatch.setattr(daemon_app.accounts, "reader_for", self)

    def __call__(self, pool):
        self.asked.append(pool)
        reader = self._make(pool)
        if reader is not None:
            reader.client = self.provider.client()
            self.made.append(reader)
        return reader


def account_row(html: str, name: str) -> list[str]:
    """The cells of the account *name* on the pool page, as text."""
    accounts_section = re.search(r'<section id="accounts">(.*?)</section>', html, re.S).group(1)
    row = re.search(rf'<tr data-account="{name}">(.*?)</tr>', accounts_section, re.S).group(1)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    return [" ".join(re.sub(r"<[^>]+>", "", cell).split()) for cell in cells]


def test_the_daemon_reads_the_accounts_while_it_serves_and_the_page_shows_them(
    pool_rig, provider, monkeypatch
):
    provider.set_key(KEY_A, usage=12.5, limit=20.0, limit_remaining=7.5, free_used=50)
    readers = Readers(monkeypatch, provider)
    application = daemon_app.create_app(pool_rig.root)
    application.state.pool = service = serving(pool_rig)
    # making the application reads nothing: the reader runs while the daemon serves
    assert readers.asked == [] and provider.seen == []

    with TestClient(application, follow_redirects=False) as client:
        assert readers.asked == [service]
        deadline = 200
        while len(provider.seen) < 2 and (deadline := deadline - 1):
            client.get(HEALTH_PATH)
        token = auth.mint_token(pool_rig.root, "developer")
        signin = client.post(web.SIGNIN_PATH, data={"identity": "developer", "token": token})
        assert signin.status_code == 303, signin.text
        html = client.get("/pool").text
        with pytest.raises(ClosedAccount):
            service.grant("rebasers", holder_label="orchestrator-a", cwd=pool_rig.work)

    assert account_row(html, "team-a")[3:7] == ["12.5", "20", "7.5", f"closed until {RESET}"]
    assert account_row(html, "team-b")[6] == "open"
    assert {(seen.method, seen.path) for seen in provider.seen} == {("GET", KEY_READ_PATH)}
    assert len(provider.seen) == 2
    # no member ran, so no call of one was refused: the read alone closed the account
    with pool_rig.store() as store:
        assert store.list_leases() == []


def test_a_daemon_with_no_account_no_pool_or_a_pool_with_faults_starts_no_reader(
    tmp_path, provider, monkeypatch
):
    readers = Readers(monkeypatch, provider)
    bare = tmp_path / "bare"
    bare.mkdir()
    no_account = tmp_path / "no-account"
    no_account.mkdir()
    write_pool(no_account)
    faulty = _write_three_faults(tmp_path / "faulty")

    for daemon_root in (bare, no_account, faulty):
        with TestClient(daemon_app.create_app(daemon_root)) as client:
            assert client.get(HEALTH_PATH).status_code == 200

    assert [pool is None for pool in readers.asked] == [True, False, True]
    assert provider.seen == []
    with open_pool_store(no_account) as store:
        assert store.list_accounts() == []


def write_pool_with_an_account(root) -> None:
    """The pool directory of ``write_pool``, with the account "team-a" declared."""
    write_pool(root)
    pool_file = cli_admin.pool_dir(root) / pool_config.POOL_FILE
    data = yaml.safe_load(pool_file.read_text(encoding="utf-8"))
    data["accounts"] = [{"name": "team-a", "cap": 2, "key_env": "TEAM_A_KEY"}]
    pool_file.write_text(yaml.safe_dump(data), encoding="utf-8")


@pytest.mark.parametrize("at_start", ["faults", "no account"])
def test_a_daemon_that_got_its_first_account_from_a_reload_reads_its_key(
    tmp_path, provider, monkeypatch, at_start
):
    monkeypatch.setattr(accounts, "IDLE_INTERVAL", 0.01)
    monkeypatch.setenv("TEAM_A_KEY", KEY_A)
    Readers(monkeypatch, provider)
    root = tmp_path / "daemon"
    if at_start == "faults":
        _write_three_faults(root)
    else:
        root.mkdir()
        write_pool(root)
    application = daemon_app.create_app(root)
    assert (application.state.pool is None) == (at_start == "faults")

    with TestClient(application) as client:
        assert client.get(HEALTH_PATH).status_code == 200
        assert provider.seen == []
        # the admin writes the directory anew and has the daemon read it again
        shutil.rmtree(cli_admin.pool_dir(root))
        write_pool_with_an_account(root)
        assert pool_routes.reload_pool(application, "developer") == ()
        deadline = 500
        while not provider.seen and (deadline := deadline - 1):
            client.get(HEALTH_PATH)

    assert provider.seen == [Seen("GET", KEY_READ_PATH, f"Bearer {KEY_A}")]
    with open_pool_store(root) as store:
        assert store.get_account("team-a").figures.usage == 12.5


def test_a_reload_that_is_refused_brings_no_reader_a_key_to_read(tmp_path, provider, monkeypatch):
    monkeypatch.setattr(accounts, "IDLE_INTERVAL", 0.01)
    monkeypatch.setenv("TEAM_A_KEY", KEY_A)
    Readers(monkeypatch, provider)
    root = tmp_path / "daemon"
    root.mkdir()
    write_pool(root)
    application = daemon_app.create_app(root)

    with TestClient(application) as client:
        in_force = application.state.pool.config
        shutil.rmtree(cli_admin.pool_dir(root))
        write_pool_with_an_account(root)
        pool_file = cli_admin.pool_dir(root) / pool_config.POOL_FILE
        pool_file.write_text(
            pool_file.read_text(encoding="utf-8").replace("size: 1", "size: 0"), encoding="utf-8"
        )
        assert pool_routes.reload_pool(application, "developer") != ()
        for _ in range(50):
            client.get(HEALTH_PATH)
        assert application.state.pool.config is in_force

    assert provider.seen == []
