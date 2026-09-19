"""Reading each account's key figures from the provider, and the daily free quota.

The pool asks the provider what each declared account's one key has used and
has left, and keeps the answer in the pool store. An account whose daily
free-model requests are spent is closed until the provider's day turns over.
Everything here runs against a fake provider and a fake clock: no network.
"""

import datetime
from pathlib import Path

import pytest

import specflo
from specflo import daemon
from specflo.daemon import poolstore
from specflo.daemon.poolstore import AccountFigures
from specflo.pool import accounts
from specflo.pool.config import Account

from .stub_provider import KEY_READ_PATH, Seen, StubProvider

UTC = datetime.timezone.utc
NOON = datetime.datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
NOON_TEXT = "2026-09-19T12:00:00.000+00:00"
RESET_TEXT = "2026-09-20T00:00:00.000+00:00"

MAIN = Account(name="openrouter-main", cap=4, key_env="OPENROUTER_API_KEY")
FREE = Account(name="openrouter-free", cap=2, key_env="OPENROUTER_FREE_KEY")
MAIN_KEY = "sk-or-v1-main-secret"
FREE_KEY = "sk-or-v1-free-secret"
ENVIRON = {MAIN.key_env: MAIN_KEY, FREE.key_env: FREE_KEY}


class FakeClock:
    """A clock a test sets; the reader is handed it and asks it the time."""

    def __init__(self, now: datetime.datetime) -> None:
        self.now = now

    def __call__(self) -> datetime.datetime:
        return self.now


@pytest.fixture
def store(tmp_path):
    with poolstore.open_pool_store(daemon.prepare_root(tmp_path / "daemon")) as store:
        yield store


@pytest.fixture
def provider():
    provider = StubProvider()
    provider.set_key(MAIN_KEY, usage=12.5, limit=50.0, limit_remaining=37.5, free_limit=1000,
                     free_used=40)
    provider.set_key(FREE_KEY, usage=0.0, free_limit=50, free_used=3)
    return provider


@pytest.fixture
def clock():
    return FakeClock(NOON)


def read(provider, store, clock, declared=(MAIN, FREE), environ=ENVIRON):
    with provider.client() as client:
        return accounts.read_accounts(
            declared, store, clock=clock, client=client, environ=environ
        )


# --- the figures -------------------------------------------------------------


def test_each_declared_accounts_figures_are_recorded_as_the_provider_gave_them(
    provider, store, clock
):
    read(provider, store, clock)

    assert store.get_account(MAIN.name) == poolstore.Account(
        name=MAIN.name,
        closed=False,
        reopen=None,
        figures=AccountFigures(
            usage=12.5, limit=50.0, remaining=37.5, free_requests=960,
            read_at=NOON_TEXT, read_error=None,
        ),
    )
    # A key with no credit limit has no limit and no remaining balance to report.
    assert store.get_account(FREE.name).figures == AccountFigures(
        usage=0.0, limit=None, remaining=None, free_requests=47,
        read_at=NOON_TEXT, read_error=None,
    )


def test_the_read_returns_what_it_stored_in_declared_order(provider, store, clock):
    records = read(provider, store, clock)

    assert [record.name for record in records] == [MAIN.name, FREE.name]
    assert records == [store.get_account(MAIN.name), store.get_account(FREE.name)]


def test_only_the_key_read_endpoint_is_called_with_each_accounts_own_key(
    provider, store, clock
):
    read(provider, store, clock)

    assert provider.seen == [
        Seen("GET", KEY_READ_PATH, f"Bearer {MAIN_KEY}"),
        Seen("GET", KEY_READ_PATH, f"Bearer {FREE_KEY}"),
    ]


# --- the daily free quota ----------------------------------------------------


def test_an_account_with_no_free_requests_left_is_closed_until_the_next_daily_reset(
    provider, store, clock
):
    provider.set_key(FREE_KEY, free_limit=50, free_used=50)

    read(provider, store, clock)

    closed = store.get_account(FREE.name)
    assert closed.closed is True
    assert closed.reopen == RESET_TEXT
    assert closed.figures.free_requests == 0
    # The other account has requests left and stays open.
    assert store.get_account(MAIN.name).closed is False


def test_the_providers_day_is_the_utc_day():
    late = datetime.datetime(2026, 9, 19, 23, 59, 59, tzinfo=UTC)
    midnight = datetime.datetime(2026, 9, 20, 0, 0, 0, tzinfo=UTC)
    # 20:30 on the 19th in a zone four hours behind is already the 20th in UTC.
    behind = datetime.datetime(
        2026, 9, 19, 20, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=-4))
    )

    assert accounts.next_daily_reset(NOON) == midnight
    assert accounts.next_daily_reset(late) == midnight
    assert accounts.next_daily_reset(midnight) == midnight + datetime.timedelta(days=1)
    assert accounts.next_daily_reset(behind) == midnight + datetime.timedelta(days=1)


def test_a_closed_account_reads_open_again_after_its_reopen_time(provider, store, clock):
    provider.set_key(FREE_KEY, free_limit=50, free_used=50)
    read(provider, store, clock)

    assert accounts.is_open(store.get_account(FREE.name), clock()) is False
    clock.now = datetime.datetime(2026, 9, 19, 23, 59, 59, tzinfo=UTC)
    assert accounts.is_open(store.get_account(FREE.name), clock()) is False
    clock.now = datetime.datetime(2026, 9, 20, 0, 0, 1, tzinfo=UTC)
    assert accounts.is_open(store.get_account(FREE.name), clock()) is True


def test_an_account_nothing_was_written_about_reads_open(store, clock):
    assert accounts.is_open(store.get_account(MAIN.name), clock()) is True


def test_a_read_after_the_reset_stores_the_account_open_again(provider, store, clock):
    provider.set_key(FREE_KEY, free_limit=50, free_used=50)
    read(provider, store, clock)

    clock.now = datetime.datetime(2026, 9, 20, 0, 5, 0, tzinfo=UTC)
    provider.set_key(FREE_KEY, free_limit=50, free_used=0)
    read(provider, store, clock)

    reopened = store.get_account(FREE.name)
    assert (reopened.closed, reopened.reopen) == (False, None)
    assert reopened.figures.free_requests == 50


def test_a_read_before_the_reopen_time_leaves_a_closed_account_closed(provider, store, clock):
    # Something other than the daily quota may have closed it; requests left
    # today say nothing about that, so only the reopen time opens it.
    store.set_account_closed(MAIN.name, reopen=RESET_TEXT)

    read(provider, store, clock)

    kept = store.get_account(MAIN.name)
    assert (kept.closed, kept.reopen) == (True, RESET_TEXT)
    assert kept.figures.free_requests == 960


# --- a read that fails -------------------------------------------------------


def test_an_unreachable_provider_leaves_the_account_open_and_records_the_error(
    provider, store, clock
):
    provider.unreachable = True

    read(provider, store, clock)

    for declared in (MAIN, FREE):
        record = store.get_account(declared.name)
        assert record.closed is False
        assert record.figures == AccountFigures(
            usage=None, limit=None, remaining=None, free_requests=None,
            read_at=NOON_TEXT, read_error=record.figures.read_error,
        )
        assert "connection refused" in record.figures.read_error


def test_a_failed_read_does_not_open_an_account_that_was_closed(provider, store, clock):
    provider.set_key(FREE_KEY, free_limit=50, free_used=50)
    read(provider, store, clock)

    provider.unreachable = True
    read(provider, store, clock)

    record = store.get_account(FREE.name)
    assert (record.closed, record.reopen) == (True, RESET_TEXT)
    assert record.figures.read_error is not None


def test_a_refusal_from_the_provider_is_recorded_with_its_status(provider, store, clock):
    provider.status = 401

    read(provider, store, clock, declared=(MAIN,))

    record = store.get_account(MAIN.name)
    assert record.closed is False
    assert "401" in record.figures.read_error
    assert record.figures.free_requests is None


@pytest.mark.parametrize("body", ["not json", "[]", '{"data": {"usage": "lots"}}'])
def test_an_answer_that_is_not_a_key_record_is_a_read_error(provider, store, clock, body):
    provider.body = body

    read(provider, store, clock, declared=(MAIN,))

    record = store.get_account(MAIN.name)
    assert record.closed is False
    assert record.figures.read_error is not None
    assert record.figures.usage is None


def test_an_unset_key_variable_is_a_read_error_naming_the_variable_and_sends_nothing(
    provider, store, clock
):
    read(provider, store, clock, declared=(MAIN,), environ={})

    record = store.get_account(MAIN.name)
    assert record.closed is False
    assert MAIN.key_env in record.figures.read_error
    assert provider.seen == []


def test_one_accounts_failed_read_does_not_stop_the_next(provider, store, clock):
    read(provider, store, clock, environ={FREE.key_env: FREE_KEY})

    assert store.get_account(MAIN.name).figures.read_error is not None
    assert store.get_account(FREE.name).figures.free_requests == 47


# --- the key stays where it is -----------------------------------------------


def test_no_key_value_reaches_the_store_or_an_error(provider, store, clock):
    for setup in ("reachable", "unreachable", "refused", "garbled"):
        provider.unreachable = setup == "unreachable"
        provider.status = 401 if setup == "refused" else 200
        provider.body = "not json" if setup == "garbled" else None
        records = read(provider, store, clock)
        for key in (MAIN_KEY, FREE_KEY):
            assert key not in repr(records)
            assert key not in repr(store.list_accounts())


def test_no_source_file_calls_a_key_creation_endpoint():
    # The provider mints keys at the key-read path plus an "s"; that takes a
    # management key, which the pool never holds. The read path followed by a
    # quote is the only form the source may carry.
    creation = KEY_READ_PATH + "s"
    source = Path(specflo.__file__).resolve().parent
    offenders = [
        str(path.relative_to(source))
        for path in sorted(source.rglob("*.py"))
        if creation in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

    reader = Path(accounts.__file__).read_text(encoding="utf-8")
    assert KEY_READ_PATH in reader
    assert ".post(" not in reader and ".put(" not in reader and ".patch(" not in reader
