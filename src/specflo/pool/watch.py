"""Provider refusals of leased hosted members, read from their event logs.

A hosted member's calls go to the provider under its account's key, and the
provider can refuse one. pi then ends the turn in an assistant message whose
stop reason is an error; the message's error text is the HTTP status and the
error object of the provider's answer, ``402: {"code": 402, "message": ...,
"metadata": {"limit_source": ...}}``. The agent host writes every event of a
member to its event log, and while the daemon serves, a watcher reads the
logs of the hosted members under an active lease.

What a 402 means is in ``limit_source``. The key's credit limit or the
account's credits are used up: the account is closed until the key's limit
resets, so the pool stops granting leases that only it can serve. The
in-flight spending budget is full: that passes, the account stays open, and
the same prompt is sent to the member again once the provider's Retry-After
has gone by. A 429 is a retryable error to the member, and pi retries it by
itself; nothing is written for it, and nothing for any other error.

pi does not pass the Retry-After header on: its error text carries the
status and the body only. The wait is read from a ``Retry-After: <seconds>``
line in the error text when one is there, and is ``DEFAULT_RETRY_AFTER``
otherwise.

The watcher reads and never ends a lease. It keeps, for each lease, where it
is in the log and where the last prompt is written, and for each turn to send
again the time it is due: offsets and times, no text of a member or of a
holder. The log itself is read, and the prompt sent again, by the runner, so
this module holds nothing of how pi or its host is talked to. A lease that
stood before the watcher's first pass is read from the end of its log: what a
daemon that was down did not see is not acted on late. A lease granted later
is read from the start of its host, which the pool started for it. A console's
host is the developer's and ran before the lease, and nothing in its log says
where a lease begins. So each pass notes where the log of an attached agent
without a lease ends, and a lease on the console is read from the last such
place: at most one pass of the developer's own turns is read with it.

The log is the one of the lease's agent, and a prompt is sent again to that
agent. Its name is not always the member's: the second agent of a member that
serves two leases has a name of its own, and so has the agent attached to a
console. The account is the one of the member.

One pass is a plain call, ``poll``, so a test drives it with a fake clock.
The loop around it runs each pass off the event loop, for a pass may wait on
the provider or on a member's host, and sleeps until the next pass or the
next turn that is due, whichever is first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from ..daemon.poolstore import PoolStore
from . import accounts, runner
from .config import CONSOLE, PoolConfig
from .ledger import agent_of

# The time between two passes over the logs, in seconds.
POLL_INTERVAL = 2.0
# The wait before a turn is sent again when the error text names none.
DEFAULT_RETRY_AFTER = 5.0

PAYMENT_REQUIRED = 402
# The sources of a 402 that close the account, and the one that passes.
CLOSING = frozenset({"openrouter_key_limit", "openrouter_credits"})
IN_FLIGHT = "openrouter_in_flight_budget"

# The time now, timezone-aware. Passed in, so a test hands over a fake one.
Clock = Callable[[], datetime]

_STATUS = re.compile(r"\s*(\d{3})\b")
_RETRY_AFTER = re.compile(r"^\s*retry-after:\s*(\d+(?:\.\d+)?)\s*$", re.IGNORECASE | re.MULTILINE)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderError:
    """What the text of a provider error says: the HTTP status, the source of
    the limit a 402 names, and the wait the provider asked for, in seconds."""

    status: int
    limit_source: str | None
    retry_after: float | None


def provider_error(text: str) -> ProviderError | None:
    """Read the error text of a failed call; None for one with no HTTP status,
    which no provider answered."""
    status = _STATUS.match(text)
    if status is None:
        return None
    limit_source = None
    brace = text.find("{")
    if brace >= 0:
        try:
            error, _ = json.JSONDecoder().raw_decode(text[brace:])
            limit_source = error["metadata"]["limit_source"]
        except (ValueError, KeyError, TypeError):
            pass
    wait = _RETRY_AFTER.search(text)
    return ProviderError(
        status=int(status.group(1)),
        limit_source=limit_source if isinstance(limit_source, str) else None,
        retry_after=float(wait.group(1)) if wait else None,
    )


@dataclass
class _Watched:
    """Where the watcher is in the log of one lease's agent."""

    offset: int
    prompt_at: int | None = None


@dataclass(frozen=True)
class _Retry:
    """One turn to send again: the prompt's place in the agent's log, and when."""

    lease_id: str
    agent: str
    prompt_at: int
    due: datetime


class Watcher:
    """Reads the event logs of the leased hosted members of one pool configuration.

    ``open_store`` opens the pool store for one unit of work. ``client`` and
    ``environ`` are what the key read of a closing account is made with; a
    client is opened for the read without one. ``sleep`` is the loop's wait,
    for a test to hand in.
    """

    def __init__(
        self,
        config: PoolConfig,
        open_store: Callable[[], PoolStore],
        *,
        pool_token: str,
        clock: Clock,
        client: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.open_store = open_store
        self.pool_token = pool_token
        self.clock = clock
        self.client = client
        self.environ = environ
        self.sleep = sleep
        self._watched: dict[str, _Watched] = {}
        self._retries: list[_Retry] = []
        # where the log of each attached agent without a lease ended, last pass
        self._console_ends: dict[str, int] = {}
        self._started = False

    def poll(self) -> float | None:
        """One pass over the logs; the seconds until the next turn is due to
        be sent again, None when none is."""
        now = self.clock()
        accounts_by_member = {
            m.name: m.account for m in self.config.members if m.backing == "hosted" and m.account
        }
        consoles = {
            m.name for m in self.config.members
            if m.kind == CONSOLE and m.name in accounts_by_member
        }
        with self.open_store() as store:
            leases = [
                lease for lease in store.list_leases(state="active")
                if lease.member in accounts_by_member
            ]
            active = {lease.id for lease in leases}
            self._watched = {k: v for k, v in self._watched.items() if k in active}
            self._retries = [r for r in self._retries if r.lease_id in active]
            for lease in leases:
                # the log is the agent's, whose name is not always the member's:
                # a member's second agent, or the one attached to a console
                agent = agent_of(lease)
                watched = self._watch(store, lease.id, agent, lease.member in consoles)
                if watched is None:
                    continue
                read = runner.read_log(agent, watched.offset, watched.prompt_at)
                watched.offset, watched.prompt_at = read.offset, read.prompt_at
                for prompt_at, text in read.failures:
                    self._failed(
                        store, lease.id, agent, accounts_by_member[lease.member],
                        prompt_at, provider_error(text), now,
                    )
            leased = {agent_of(lease) for lease in leases}
            self._console_ends = {
                row.agent: runner.log_end(row.agent)
                for row in store.list_consoles()
                if row.slot in consoles and row.agent not in leased
            }
        self._started = True
        due = [r for r in self._retries if r.due <= now]
        self._retries = [r for r in self._retries if r.due > now]
        for retry in due:
            if not runner.resend_prompt(retry.agent, retry.prompt_at, pool_token=self.pool_token):
                _log.warning("agent %s did not take its turn again", retry.agent)
        if not self._retries:
            return None
        return (min(r.due for r in self._retries) - now).total_seconds()

    def _watch(
        self, store: PoolStore, lease_id: str, agent: str, console: bool
    ) -> _Watched | None:
        """Where the log of the lease's *agent* is read from; None for a lease
        whose agent has not started, its log still being the last lease's.

        The host of a *console* was not started for the lease: its log is
        the developer's own up to where it ended in the last pass without a
        lease, and the lease is read from there."""
        if lease_id not in self._watched:
            if self._started:
                transitions = store.list_transitions(lease_id=lease_id)
                if not any(t.kind == "granted" for t in transitions):
                    return None
            if not self._started:
                start = runner.log_end(agent)
            elif console:
                start = self._console_ends.get(agent, runner.log_end(agent))
            else:
                start = runner.lease_log_start(agent)
            self._watched[lease_id] = _Watched(offset=start)
        return self._watched[lease_id]

    def _failed(
        self,
        store: PoolStore,
        lease_id: str,
        agent: str,
        account_name: str,
        prompt_at: int | None,
        error: ProviderError | None,
        now: datetime,
    ) -> None:
        """Act on one failed call of *agent*; only a 402 is acted on."""
        if error is None or error.status != PAYMENT_REQUIRED:
            return
        if error.limit_source in CLOSING:
            account = next(a for a in self.config.accounts if a.name == account_name)
            closed = accounts.close_for_limit(
                account, store, clock=self.clock, client=self.client, environ=self.environ
            )
            _log.warning(
                "account %s is closed until %s: the provider refused agent %s for %s",
                account_name, closed.reopen, agent, error.limit_source,
            )
        elif error.limit_source == IN_FLIGHT and prompt_at is not None:
            wait = DEFAULT_RETRY_AFTER if error.retry_after is None else error.retry_after
            self._retries.append(
                _Retry(lease_id, agent, prompt_at, due=now + timedelta(seconds=wait))
            )

    async def run(self) -> None:
        """Pass over the logs until cancelled. A pass that fails is said and
        the next one is made all the same."""
        while True:
            wait = None
            try:
                wait = await asyncio.to_thread(self.poll)
            except Exception as exc:
                _log.warning("the pool's watch of its members' logs: %s", exc)
            await self.sleep(POLL_INTERVAL if wait is None else min(POLL_INTERVAL, max(wait, 0.0)))


def watcher_for(service) -> Watcher | None:
    """The watcher for the pool *service*; None without a pool, and for a pool
    with no hosted member, whose calls no provider refuses."""
    if service is None or not any(m.backing == "hosted" for m in service.config.members):
        return None
    return Watcher(
        service.config, service.open_store, pool_token=service.pool_token,
        clock=service.clock, environ=service.environ,
    )
