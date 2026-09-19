"""The pool page: what the daemon's agent pool holds now, for a signed-in browser.

The page is read by the requester and the developer alike, and it changes
nothing. It shows each pool with its size, the leases out of it and the
requests that wait; each member with its state, what backs it and the model
reloads seen on it; each lease with its holder, how long it has been idle and
how long it has left; each provider account with its figures; what the project
agents take outside the pool; and the last things that happened to leases.

It is built on every request from the pool service the application holds:
the configuration as the service has it at that moment, the pool store, the
standing entries and the status each agent host last wrote of itself. Nothing
is kept between two requests. No lease expires by itself, so the page runs the
expiry check before it reads, as every reader of the pool's state does; a
member that does not stop is the log's matter and the page renders all the same.

A started member has no process between two leases, so it is never offline:
it is leased while an active lease names it and idle otherwise. A console is
offline with no agent attached or with one whose host is gone, draining after
a detach while its lease stands, and otherwise leased or idle like any member.

The page carries nothing a member wrote: no prompt, no reply and no log, and
of a member that did not start only that it did not. It carries no lease
token, no hash of one and no pool token either; a lease is named by its id
and its holder by the label the request gave.

With a pool configuration that did not stand, the page lists the faults and
nothing else of the pool; on a root that declares no pool it says so.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response

from ..pool import accounts, console, expiry, ledger, runner
from ..pool import config as pool_config
from ..pool.runner import RunnerError
from ..pool.service import PoolService
from . import poolstore
from .web import POOL_PATH, current_session, render

# How many of the latest transitions the page lists.
RECENT_TRANSITIONS = 20

# The states of a member that is not an offline or a draining console.
IDLE = "idle"
LEASED = "leased"

NO_POOL = "No pool is configured on this daemon."

_log = logging.getLogger(__name__)


# --- what the page shows --------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class PoolRow:
    """One pool: what it declares, how many of its leases are out, and who waits for it."""

    pool: pool_config.Pool
    in_use: int
    waiting: int


@dataclasses.dataclass(frozen=True)
class MemberRow:
    """One member: what it declares, its state, the leases on it and its reloads, oldest first."""

    member: pool_config.Member
    state: str
    in_use: int
    reloads: list[poolstore.Reload]

    @property
    def runs_on(self) -> str:
        """The model of a local member; the account of a hosted one, and its model."""
        if self.member.backing == pool_config.LOCAL:
            return self.member.model or ""
        return " ".join(part for part in (self.member.account, self.member.model) if part)


@dataclasses.dataclass(frozen=True)
class LeaseRow:
    """One active lease, with its idle time and the time it has left, as the page words them."""

    lease: poolstore.Lease
    idle: str
    to_expiry: str


@dataclasses.dataclass(frozen=True)
class AccountRow:
    """One declared account: its cap, the slots taken, and what the store knows of it."""

    account: pool_config.Account
    in_use: int
    figures: poolstore.AccountFigures | None
    open: bool
    reopen: str | None


@dataclasses.dataclass(frozen=True)
class PoolView:
    """The pool as the page shows it to ``viewer``, the signed-in identity.

    ``leases`` are the active ones. ``waiting`` is every request that waits,
    for a pool or for a team, in arrival order. ``transitions`` are the
    latest, newest first. ``reload_data`` is False while no reader of model
    events is getting them, so the reloads listed may be short of what happened.
    """

    viewer: str
    pools: list[PoolRow]
    waiting: list[poolstore.WaitingRequest]
    members: list[MemberRow]
    leases: list[LeaseRow]
    accounts: list[AccountRow]
    standing: list[ledger.Standing]
    transitions: list[poolstore.Transition]
    reload_data: bool


def span(delta: timedelta) -> str:
    """*delta* in hours, minutes and seconds, "1h 5m" or "45s"; nothing below zero."""
    seconds = max(0, int(delta.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{n}{unit}" for n, unit in ((hours, "h"), (minutes, "m"), (seconds, "s")) if n]
    return " ".join(parts) or "0s"


def limit(seconds: int | None) -> str:
    """A limit a pool declares, in seconds, as a span; nothing for one it does not declare."""
    return "" if seconds is None else span(timedelta(seconds=seconds))


def amount(value: float | None) -> str:
    """A figure of an account as it is shown; nothing for one that was not read."""
    return "" if value is None else f"{value:g}"


def cause_shown(cause: str) -> str:
    """The cause of a transition as the page words it.

    A cause is a few words of the pool's own, but for a member that did not
    start the service keeps the runner's words after them, and those are what
    an agent CLI printed: they can name paths on this host and repeat what the
    member's process wrote. The page stops before them, as the pool routes do.
    """
    head, cut, _ = cause.partition(": ")
    return f"{head} (the daemon's log has the detail)" if cut else head


def _taken(holding, kind: str, name: str) -> int:
    """How many of *holding*, leases or standing entries, took the resource *kind* *name*."""
    taken = poolstore.Resource(kind, name)
    return sum(1 for held in holding if taken in held.resources)


def _member_state(member, active, rows, statuses) -> str:
    """Offline, draining, leased or idle. Only a console is ever one of the first two."""
    if member.kind == pool_config.CONSOLE:
        slot = console.state(member.name, rows, active, statuses)
        if slot != console.ATTACHED:
            return slot
    return LEASED if _taken(active, ledger.MEMBER, member.name) else IDLE


def pool_view(service: PoolService, viewer: str) -> PoolView:
    """The page's view of the pool *service* holds, at the time on its clock."""
    config = service.config
    now = service.clock()
    with service.open_store() as store:
        active = store.list_leases(state=ledger.ACTIVE)
        waiting = store.list_waiting()
        records = {record.name: record for record in store.list_accounts()}
        reloads = store.list_reloads()
        reload_data = store.reload_data_available()
        rows = store.list_consoles()
        transitions = store.list_transitions(limit=RECENT_TRANSITIONS)
    standing = list(service.standing())
    agents = {ledger.agent_of(lease) for lease in active} | {row.agent for row in rows}
    statuses = {agent: runner.status(agent) for agent in agents}
    judged = expiry.judged_activity(
        [(lease, statuses[ledger.agent_of(lease)]) for lease in active], now
    )
    lease_rows = []
    for lease in active:
        idle = max(now - judged[lease.id], timedelta(0))
        left = timedelta(seconds=lease.idle_limit) - idle
        lease_rows.append(LeaseRow(lease=lease, idle=span(idle), to_expiry=span(left)))
    return PoolView(
        viewer=viewer,
        pools=[
            PoolRow(
                pool=pool,
                in_use=_taken(active, ledger.POOL, pool.name),
                waiting=sum(1 for request in waiting if request.pool == pool.name),
            )
            for pool in config.pools
        ],
        waiting=waiting,
        members=[
            MemberRow(
                member=member,
                state=_member_state(member, active, rows, statuses),
                in_use=_taken(active, ledger.MEMBER, member.name),
                reloads=[reload for reload in reloads if reload.member == member.name],
            )
            for member in config.members
        ],
        leases=lease_rows,
        accounts=[
            AccountRow(
                account=account,
                # A project agent's slot counts as a lease's does.
                in_use=_taken([*active, *standing], ledger.ACCOUNT, account.name),
                figures=record.figures if record is not None else None,
                open=accounts.is_open(record, now),
                reopen=record.reopen if record is not None else None,
            )
            for account in config.accounts
            for record in (records.get(account.name),)
        ],
        standing=standing,
        transitions=list(reversed(transitions)),
        reload_data=reload_data,
    )


# --- the page -------------------------------------------------------------------

pages = APIRouter()


def _expire_due(service: PoolService) -> None:
    """End the leases that are due before the pool's state is read. A member
    that does not stop is the log's matter: its lease has ended all the same."""
    try:
        service.expire_due()
    except RunnerError as exc:
        _log.warning("an expired lease: %s", exc)


@pages.get(POOL_PATH, include_in_schema=False)
def pool_page(request: Request, identity: str = Depends(current_session)) -> Response:
    """The pool as it stands now; the faults of a configuration that did not
    stand in its place, or that no pool is configured."""
    service = getattr(request.app.state, "pool", None)
    if service is None:
        errors = [str(error) for error in getattr(request.app.state, "pool_errors", ())]
        return render("pool.html", identity=identity, view=None, errors=errors, no_pool=NO_POOL)
    _expire_due(service)
    return render(
        "pool.html", identity=identity, view=pool_view(service, identity), limit=limit,
        amount=amount, cause_shown=cause_shown,
    )
