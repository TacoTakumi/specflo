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

The key goes into the request's authorization header and nowhere else: no
record, error or message carries it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone

import httpx

from ..daemon import poolstore
from ..daemon.poolstore import AccountFigures, PoolStore
from .config import Account

PROVIDER_URL = "https://openrouter.ai"
KEY_READ_PATH = "/api/v1/key"
READ_TIMEOUT = 10.0

# The time now, timezone-aware. Passed in, so a test hands over a fake one.
Clock = Callable[[], datetime]


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
        failed = AccountFigures(
            usage=None, limit=None, remaining=None, free_requests=None,
            read_at=read_at, read_error=str(exc),
        )
        return store.set_account_figures(account.name, failed)
    record = store.set_account_figures(account.name, figures)
    if figures.free_requests == 0:
        return store.set_account_closed(account.name, reopen=_text(next_daily_reset(now)))
    # Requests left today do not open a closed account: something other than
    # the daily quota may have closed it. Only its reopen time does.
    if record.closed and is_open(record, now):
        return store.set_account_open(account.name)
    return record


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
