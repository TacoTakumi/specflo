"""A developer's console: a declared slot that serves a lease only while attached.

The roster is complete, so a console is declared like any member, as an entry
of kind ``console`` (see ``config``). What is declared is a slot, not a
process: the pool never starts one for it. A developer who runs pi under an
agent host on the daemon's host attaches that host to the slot by name, and
from then on the slot is a member like any other, matched by the same ledger
and leased by the same rows.

A slot is in one of three states, and they are read from the store's console
rows and the active lease rows, by functions that open nothing, so a page
reads them with no service at hand. With no row the slot is offline. With a
row it is attached, and the row names the agent. A detach marks the row
draining: the slot takes no new lease, the lease that is out on it stands,
and with none out the slot is offline again, row or no row. A row whose slot
the configuration no longer declares is no slot's, and is never read.

The ledger matches nothing to a slot that is offline or draining. The pool
service hands it those slots by name at every placement (``unmatched``), and
the ledger passes them over. A pool that only such slots serve has no room,
which is the refusal a request waits on: an attach ends the wait.

An attach is refused for a name that is not a declared console, for a slot an
agent is attached to or that still drains, and for an agent that does not run
on the daemon's host or runs on the TUI transport. Otherwise the pool's token
is bound on the agent's host, which is all the pool does to it until a lease:
a host takes one pool's token, once, so a host that serves another slot, or a
leased member's, does not take it and is refused by that.

A lease on an attached slot runs on the attached agent, so that is the agent
the grant names and the name on the lease row (``placement``).

Nothing here talks to an agent host; the runner does, as for every member.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from datetime import timezone
from typing import TYPE_CHECKING

from ..daemon.poolstore import Conflict, ConsoleAttachment, Lease, Resource
from ..errors import SpecfloError
from . import ledger, runner
from .config import CONSOLE, Member, PoolConfig

if TYPE_CHECKING:
    from .service import PoolService

# The states of a console slot.
OFFLINE = "offline"
ATTACHED = "attached"
DRAINING = "draining"


class ConsoleRefused(SpecfloError):
    """An attach or a detach that is refused; the message says why."""


def slots(config: PoolConfig) -> tuple[Member, ...]:
    """The console slots *config* declares."""
    return tuple(member for member in config.members if member.kind == CONSOLE)


def state(slot: str, rows: Iterable[ConsoleAttachment], leases: Iterable[Lease]) -> str:
    """The state of the console *slot*, from the console *rows* and the *leases*.

    *leases* may hold rows that have ended; they count for nothing.
    """
    row = next((row for row in rows if row.slot == slot), None)
    if row is None:
        return OFFLINE
    if not row.draining:
        return ATTACHED
    taken = Resource(ledger.MEMBER, slot)
    out = any(lease.state == ledger.ACTIVE and taken in lease.resources for lease in leases)
    return DRAINING if out else OFFLINE


def unmatched(config: PoolConfig, rows: Iterable[ConsoleAttachment]) -> frozenset[str]:
    """The console slots of *config* that take no new lease now: those with no
    agent attached, and those that were detached."""
    serving = {row.slot for row in rows if not row.draining}
    return frozenset(member.name for member in slots(config) if member.name not in serving)


def placement(
    placed: ledger.Placement, rows: Iterable[ConsoleAttachment]
) -> ledger.Placement:
    """*placed* as the lease row is to say it: a lease on a console runs on
    the agent attached to the slot, under that agent's own name. A placement
    on any other member, or on a slot with no agent, is returned as it is."""
    agent = next((row.agent for row in rows if row.slot == placed.member.name), None)
    if placed.member.kind != CONSOLE or agent is None:
        # With no row the question was whether the slot could ever serve.
        return placed
    resources = tuple(r for r in placed.resources if r.kind != ledger.AGENT)
    if agent != placed.member.name:
        # As for a further lease on a member: a row with no agent name runs
        # under its member's.
        resources += (Resource(ledger.AGENT, agent),)
    return replace(placed, agent=agent, resources=resources)


def attach(service: PoolService, slot: str, agent: str) -> ConsoleAttachment:
    """Attach the running agent host *agent* to the console *slot* of the pool
    of *service*; the row that says so.

    Raises ``ConsoleRefused`` for a name that is no declared console, a slot
    that is attached or still drains, and an agent that is not attachable.
    """
    _declared(service.config, slot)
    with service.open_store() as store:
        rows = store.list_consoles()
        now = state(slot, rows, store.list_leases(state=ledger.ACTIVE))
        if now == ATTACHED:
            held = next(row.agent for row in rows if row.slot == slot)
            raise ConsoleRefused(
                f"console '{slot}' has an agent attached to it already, '{held}'; detach it "
                "first."
            )
        if now == DRAINING:
            raise ConsoleRefused(
                f"console '{slot}' was detached and its lease is still out; it goes offline, "
                "and can be attached again, when that lease ends."
            )
        try:
            runner.attach_console(agent, pool_token=service.pool_token)
        except runner.NotAttachable as exc:
            raise ConsoleRefused(str(exc)) from exc
        attached = ConsoleAttachment(
            slot=slot, agent=agent,
            attached=service.clock().astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        )
        # The row of a slot that drained is still there, and says nothing now.
        store.detach_console(slot)
        try:
            store.attach_console(attached)
        except Conflict as exc:
            raise ConsoleRefused(
                f"console '{slot}' was attached by another call in the meantime."
            ) from exc
    return attached


def detach(service: PoolService, slot: str) -> str:
    """Detach the console *slot*: it takes no new lease from now on. The
    slot's state then: draining while its lease is out, or offline.

    The lease that is out stands, and nothing is stopped. Raises
    ``ConsoleRefused`` for a name that is no declared console and for a slot
    with nothing attached.
    """
    _declared(service.config, slot)
    with service.open_store() as store:
        if store.set_console_draining(slot) is None:
            raise ConsoleRefused(f"console '{slot}': nothing is attached to it.")
        return state(slot, store.list_consoles(), store.list_leases(state=ledger.ACTIVE))


def _declared(config: PoolConfig, slot: str) -> None:
    if slot not in {member.name for member in slots(config)}:
        declared = ", ".join(member.name for member in slots(config)) or "none"
        raise ConsoleRefused(
            f"'{slot}' is not a declared console; the declared consoles: {declared}."
        )
