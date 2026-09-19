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

An attached host is the developer's to end, and one that is gone serves no
one. Whoever reads the states hands in what the hosts last wrote of
themselves, by agent name, with None for a host that is gone (``statuses``):
the slot of such a host is offline, whatever its row says. The lease that was
out on it stands until it has been idle for its limit, as the lease of any
member whose pi went away. With no statuses handed in, the rows are taken at
their word.

The ledger matches nothing to a slot that is offline or draining. The pool
service hands it those slots by name at every placement (``unmatched``), and
the ledger passes them over. A pool that only such slots serve has no room,
which is the refusal a request waits on: an attach ends the wait.

An attach is refused for a name that is not a declared console, for a slot an
agent is attached to or that still drains, and for an agent that does not run
on the daemon's host or runs on the TUI transport. It is refused as well for
an agent that serves another slot, attached or still draining, and for one
that a lease on a member the pool started runs on: a host serves one slot.
Otherwise the pool's token is bound on the agent's host, which is all the
pool does to it until a lease. A host takes one pool's token, and takes the
same one again, so the host that left a slot may be attached anew.

A lease on an attached slot runs on the attached agent, so that is the agent
the grant names and the name on the lease row (``placement``). Its ending
stops nothing, and ``leased`` says which leases end so: those on a declared
console, those on a slot's attached agent when a later configuration
declares the slot otherwise, and those on a member the configuration no
longer declares at all. Only a lease that is known to be on a member the
pool started has its process stopped.

Nothing here talks to an agent host; the runner does, as for every member.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def state(
    slot: str,
    rows: Iterable[ConsoleAttachment],
    leases: Iterable[Lease],
    statuses: Mapping[str, object] | None = None,
) -> str:
    """The state of the console *slot*, from the console *rows* and the *leases*.

    *leases* may hold rows that have ended; they count for nothing. With
    *statuses*, what each attached host last wrote of itself by agent name, a
    slot whose host is gone, or is not among them, is offline.
    """
    row = next((row for row in rows if row.slot == slot), None)
    if row is None or _gone(row, statuses):
        return OFFLINE
    if not row.draining:
        return ATTACHED
    taken = Resource(ledger.MEMBER, slot)
    out = any(lease.state == ledger.ACTIVE and taken in lease.resources for lease in leases)
    return DRAINING if out else OFFLINE


def unmatched(
    config: PoolConfig,
    rows: Iterable[ConsoleAttachment],
    statuses: Mapping[str, object] | None = None,
) -> frozenset[str]:
    """The console slots of *config* that take no new lease now: those with no
    agent attached, those that were detached and, with *statuses* (see
    ``state``), those whose host is gone."""
    serving = {row.slot for row in rows if not row.draining and not _gone(row, statuses)}
    return frozenset(member.name for member in slots(config) if member.name not in serving)


def leased(lease: Lease, config: PoolConfig, rows: Iterable[ConsoleAttachment]) -> bool:
    """Does *lease* run on a developer's process, which its ending must leave
    running? It does not when *config* declares its member as one the pool
    starts and no row of *rows* attached the lease's agent under that name."""
    member = next((m for m in config.members if m.name == lease.member), None)
    if member is None or member.kind == CONSOLE:
        # A member that is declared no more is not known to be the pool's own.
        return True
    agent = ledger.agent_of(lease)
    return any(row.slot == lease.member and row.agent == agent for row in rows)


def _gone(row: ConsoleAttachment, statuses: Mapping[str, object] | None) -> bool:
    return statuses is not None and statuses.get(row.agent) is None


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
    that is attached or still drains, an agent that serves another slot or
    runs a lease of a member the pool started, and one that is not attachable.
    """
    _declared(service.config, slot)
    with service.open_store() as store:
        rows = store.list_consoles()
        out = store.list_leases(state=ledger.ACTIVE)
        now = state(slot, rows, out)
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
        # A host takes this pool's token again, so it is asked here whether
        # the agent serves already: one host is one slot's.
        for row in rows:
            if row.agent == agent and row.slot != slot and state(row.slot, rows, out) != OFFLINE:
                raise ConsoleRefused(
                    f"agent '{agent}' is attached to console '{row.slot}'; detach it there "
                    "first, and let its lease end."
                )
        if any(ledger.agent_of(lease) == agent for lease in out):
            raise ConsoleRefused(
                f"agent '{agent}' is not a developer's to attach: a lease of the pool runs on "
                "it, on a member the pool started."
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
