"""Provider accounts: what each one's key has used and has left, read from the provider.

Each declared account has one API key, held in an environment variable. The
pool asks the provider about that key and keeps the answer in the pool store:
credits used, the key's credit limit, what is left of it, and how many
free-model requests are left today. The provider is OpenRouter, and the one
call made is its key read, ``GET /api/v1/key``; the pool holds no management
key and creates no keys.

Free-model requests are counted per account and per UTC day. An account with
none left is stored closed until the day turns over, midnight UTC. A read
that fails closes nothing: the account is left as it was and the failure is
kept in place of the figures. The store makes no decision, so whether a
closed account is open again is computed here, from its reopen time.

The provider can also refuse a member's call outright, for the key's credit
limit or for the account's credits. The account is then closed until the
key's limit resets: daily, weekly or monthly, as the key read says, on the
UTC day, the UTC week from Monday and the UTC month.

While the daemon serves, a reader reads every declared account's key: once
when it starts and again after each ``READ_INTERVAL``. Which pool is in force,
and which accounts it declares, is asked at each read, so a configuration put
in force later is followed. Without a pool, and with a pool that declares no
account, the reader asks the provider nothing and looks again after the short
``IDLE_INTERVAL``: the first account a reload brings is read that soon, and
one more account at the next routine read. Each read runs off the event loop,
and no request the daemon serves asks the provider anything: a grant and a
page read the store.

The key goes into the request's authorization header and nowhere else: no
record, error or message carries it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone

import httpx

from ..daemon import poolstore
from ..daemon.poolstore import AccountFigures, PoolStore
from .config import Account

PROVIDER_URL = "https://openrouter.ai"
KEY_READ_PATH = "/api/v1/key"
READ_TIMEOUT = 10.0
# The time between two routine reads of every account's key, in seconds. The
# figures change slowly and the provider is someone else's service.
READ_INTERVAL = 15 * 60
# The wait of a reader with no key to read, in seconds, before it looks again:
# the first account that a reload brings is read this soon.
IDLE_INTERVAL = 5.0

# The time now, timezone-aware. Passed in, so a test hands over a fake one.
Clock = Callable[[], datetime]


_log = logging.getLogger(__name__)


class _ReadError(Exception):
    """Why a read gave no figures; the message is safe to store and show."""


def read_accounts(
    accounts: Iterable[Account],
    store: PoolStore,
    *,
    clock: Clock,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[poolstore.Account]:
    """Read every declared account's key figures into ``store``; the records as stored.

    One account's failed read does not stop the next. Without ``client``, one
    is opened on the provider for the length of the call.
    """
    environ = os.environ if environ is None else environ
    own = client is None
    client = httpx.Client(base_url=PROVIDER_URL, timeout=READ_TIMEOUT) if own else client
    try:
        return [
            read_account(account, store, clock=clock, client=client, environ=environ)
            for account in accounts
        ]
    finally:
        if own:
            client.close()


def read_account(
    account: Account,
    store: PoolStore,
    *,
    clock: Clock,
    client: httpx.Client,
    environ: Mapping[str, str],
) -> poolstore.Account:
    """Read one account's key figures into ``store``; the record as stored."""
    now = clock()
    read_at = _text(now)
    try:
        figures = _figures(_read_key(account, client, environ), read_at)
    except _ReadError as exc:
        return store.set_account_figures(account.name, _failed(read_at, exc))
    record = store.set_account_figures(account.name, figures)
    if figures.free_requests == 0:
        return store.set_account_closed(account.name, reopen=_text(next_daily_reset(now)))
    # Requests left today do not open a closed account: something other than
    # the daily quota may have closed it. Only its reopen time does.
    if record.closed and is_open(record, now):
        return store.set_account_open(account.name)
    return record


def close_for_limit(
    account: Account,
    store: PoolStore,
    *,
    clock: Clock,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
) -> poolstore.Account:
    """Close ``account`` after the provider refused a call for its key's
    limit or its credits; the record as stored.

    The key is read for its figures and for when its limit resets, which is
    the reopen time. A limit that never resets, and a read that fails, give
    the next daily reset instead: an account closed with no reopen time would
    stay closed, so it is tried again when the provider's day turns.
    """
    environ = os.environ if environ is None else environ
    own = client is None
    client = httpx.Client(base_url=PROVIDER_URL, timeout=READ_TIMEOUT) if own else client
    now = clock()
    reset = None
    try:
        answer = _read_key(account, client, environ)
        store.set_account_figures(account.name, _figures(answer, _text(now)))
        reset = next_key_reset(answer["data"].get("limit_reset"), now)
    except _ReadError as exc:
        store.set_account_figures(account.name, _failed(_text(now), exc))
    finally:
        if own:
            client.close()
    return store.set_account_closed(account.name, reopen=_text(reset or next_daily_reset(now)))


def next_key_reset(period: object, now: datetime) -> datetime | None:
    """When a key's credit limit resets next after ``now``, for the reset
    period its key read names; None for a limit that never resets, and for a
    period that is not one of the provider's."""
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return today + timedelta(days=1)
    if period == "weekly":
        return today + timedelta(days=7 - today.weekday())
    if period == "monthly":
        return (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return None


def next_daily_reset(now: datetime) -> datetime:
    """When the provider's day turns over next: the first midnight UTC after ``now``."""
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return today + timedelta(days=1)


def is_open(record: poolstore.Account | None, now: datetime) -> bool:
    """Whether an account can be used at ``now``, from what the store holds of it.

    An account nothing was written about is open. A closed one is open again
    once its reopen time has come; closed with no reopen time stays closed.
    """
    if record is None or not record.closed:
        return True
    return record.reopen is not None and now >= datetime.fromisoformat(record.reopen)


def _failed(read_at: str, exc: _ReadError) -> AccountFigures:
    """What is stored in place of the figures of a read that failed."""
    return AccountFigures(
        usage=None, limit=None, remaining=None, free_requests=None,
        read_at=read_at, read_error=str(exc),
    )


def _text(time: datetime) -> str:
    """``time`` as the pool store keeps times: ISO 8601 in UTC, to the millisecond."""
    return time.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _read_key(account: Account, client: httpx.Client, environ: Mapping[str, str]) -> object:
    """The provider's answer to a read of the account's key, decoded."""
    key = environ.get(account.key_env)
    if not key:
        raise _ReadError(f"environment variable {account.key_env} is not set")
    try:
        response = client.get(KEY_READ_PATH, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise _ReadError(f"provider unreachable: {exc}") from None
    if response.status_code != 200:
        raise _ReadError(f"provider answered {response.status_code} to the key read")
    try:
        return response.json()
    except ValueError:
        raise _ReadError("provider answer to the key read is not JSON") from None


def _figures(answer: object, read_at: str) -> AccountFigures:
    """The figures in a key-read answer; a field of the wrong shape is a read error."""
    data = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(data, dict):
        raise _ReadError("provider answer to the key read has no key record")
    free = data.get("free_model_daily_requests")
    free_requests = free.get("remaining") if isinstance(free, dict) else None
    if not isinstance(free_requests, int) or isinstance(free_requests, bool):
        raise _ReadError("provider answer to the key read has no free-model request count")
    return AccountFigures(
        usage=_number(data, "usage", optional=False),
        limit=_number(data, "limit"),
        remaining=_number(data, "limit_remaining"),
        free_requests=free_requests,
        read_at=read_at,
        read_error=None,
    )


def _number(data: dict, field: str, *, optional: bool = True) -> float | None:
    """``data[field]`` as a number; the provider sends null for a key with no limit."""
    value = data.get(field)
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _ReadError(f"provider answer to the key read has no number in {field!r}")
    return float(value)


class AccountsReader:
    """Reads the key of every account the pool *service* declares, until cancelled.

    ``client`` is what the provider is asked through; a client is opened for
    each read without one. ``sleep`` is the loop's wait. Both are for a test
    to hand in.

    With ``pool``, the service is the one it gives, taken anew at each read: a
    reload opens the pool of a daemon that had none. *service* is None until then.
    """

    def __init__(
        self,
        service,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        pool: Callable[[], object | None] | None = None,
    ) -> None:
        self.service = service
        self.client = client
        self.sleep = sleep
        self.pool = pool

    def reading(self) -> bool:
        """Take the pool in force; whether there is a key to read, which no
        pool and a pool that declares no account have not."""
        if self.pool is not None:
            self.service = self.pool()
        return self.service is not None and bool(self.service.config.accounts)

    def read(self) -> list[poolstore.Account]:
        """One read of every account the configuration in force declares; the
        records as stored."""
        if not self.reading():
            return []
        service = self.service
        with service.open_store() as store:
            return read_accounts(
                service.config.accounts, store,
                clock=service.clock, client=self.client, environ=service.environ,
            )

    async def run(self) -> None:
        """Read now and again after each interval. A read that fails is said
        and the next one is made all the same. With no key to read none is
        made, and the question is asked again after the short wait."""
        while True:
            if not self.reading():
                await self.sleep(IDLE_INTERVAL)
                continue
            try:
                await asyncio.to_thread(self.read)
            except Exception as exc:
                _log.warning("the pool's read of its accounts' keys: %s", exc)
            await self.sleep(READ_INTERVAL)


def reader_for(service) -> AccountsReader:
    """The reader that follows the pool *service*, None for no pool. Whoever
    can come to another pool later sets the reader's ``pool`` to say which."""
    return AccountsReader(service)
