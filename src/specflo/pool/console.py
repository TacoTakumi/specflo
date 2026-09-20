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
the slot of such a host is offline, whatever its row says. So is the slot of
an agent whose record names another transport than rpc: the name came back
as a TUI agent, which has no host to hold a lease's wall. The lease that was
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
And it is refused for an agent whose name is one the pool starts a member's
host under, the member's own or that of a further lease on it. Agent names
are one namespace on the daemon's host, and at a grant on that member the
pool would find the attached host under the name, bound to its own token,
and stop it as the member's. A member may be declared under such a name
later, so ``started_under`` tells a configuration's members from the rows,
for whoever puts a configuration in force to refuse it.
Otherwise the pool's token is bound on the agent's host, which is all the
pool does to it until a lease. A host takes one pool's token, and takes the
same one again, so the host that left a slot may be attached anew.

A lease on an attached slot runs on the attached agent, so that is the agent
the grant names and the name on the lease row (``placement``). Its ending
stops nothing, and ``leased`` says which leases end so. The lease row keeps
what its grant knew, whether the pool started the lease's process, and a
lease ends by that: a configuration put in force later, at a start of the
daemon that no check of the leases out could refuse, may declare the member
no more, or as a console, and the process the pool started is stopped all the
same; it may start a member under a slot's name, and the console's process
runs on all the same. A row from before that was kept ends by the
configuration in force: as a console's when that declares its member a
console, or not at all. Ahead of both stand the rows: a lease on an agent
that any row names, of whatever slot and draining or not, stops nothing. A
configuration that no one could refuse may start a member under an attached
agent's name, the grant on it finds the developer's host there and ends its
lease, and that host knows the pool's token from its attach. Only a lease
that is known to be on a process the pool started has that process stopped.

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
    slot whose host is gone, is not among them, or is no rpc host is offline.
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
    ``state``), those whose host is gone or is no rpc host."""
    serving = {row.slot for row in rows if not row.draining and not _gone(row, statuses)}
    return frozenset(member.name for member in slots(config) if member.name not in serving)


def leased(lease: Lease, config: PoolConfig, rows: Iterable[ConsoleAttachment]) -> bool:
    """Does *lease* run on a developer's process, which its ending must leave
    running? It does when a row of *rows* names the lease's agent. Otherwise
    the lease's own row says, as its grant wrote it: it does not when the
    pool started the process, whatever *config* declares of the member now.
    A row from before that was written is judged by *config*: it does not
    when *config* declares its member as one the pool starts."""
    # The row of any slot, detached or not: the host that was attached under
    # the name knows the pool's token, and would be stopped as the member's.
    agent = ledger.agent_of(lease)
    if any(row.agent == agent for row in rows):
        return True
    if lease.pool_started is not None:
        return not lease.pool_started
    member = next((m for m in config.members if m.name == lease.member), None)
    # A member that is declared no more is not known to be the pool's own.
    return member is None or member.kind == CONSOLE


def started_under(
    config: PoolConfig, rows: Iterable[ConsoleAttachment]
) -> list[tuple[Member, ConsoleAttachment]]:
    """The rows of *rows* whose agent's name is one the pool starts a host
    under with *config* in force, each with the member it starts there.

    Every row counts, that of a slot that was detached or is declared no more
    too: a detach takes nothing from the host, which knows the pool's token
    until it ends, and a grant on the member would stop it as the member's.
    """
    found = []
    for row in rows:
        member = _started_as(config, row.agent)
        if member is not None:
            found.append((member, row))
    return found


def _gone(row: ConsoleAttachment, statuses: Mapping[str, object] | None) -> bool:
    if statuses is None:
        return False
    record = statuses.get(row.agent)
    if isinstance(record, Mapping):
        # A record that names no transport is an rpc host's.
        return record.get("transport", "rpc") != "rpc"
    return record is None


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
    that is attached or still drains, an agent under a name the pool starts a
    member's host under, one that serves another slot or runs a lease of a
    member the pool started, and one that is not attachable.
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
        # With no lease on it now, the name may still be one the pool starts
        # a host under at the next grant.
        started = _started_as(service.config, agent)
        if started is not None:
            raise ConsoleRefused(
                f"agent '{agent}' is not a developer's to attach: the pool starts member "
                f"'{started.name}' under that name, and a host there that knows the pool's token "
                "is stopped as the member's. Start the agent under another name to attach it."
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


def _started_as(config: PoolConfig, agent: str) -> Member | None:
    """The member of *config* whose host the pool starts under the name
    *agent*, if there is one: a member of another kind than console runs
    under its own name, and a further lease on it under that name, a dot and
    a number, unless that is a declared name (see the ledger's placement)."""
    declared = {member.name: member for member in config.members}
    stem, dot, number = agent.rpartition(".")
    if agent not in declared and dot and number.isdigit():
        agent = stem
    member = declared.get(agent)
    return member if member is not None and member.kind != CONSOLE else None


def _declared(config: PoolConfig, slot: str) -> None:
    if slot not in {member.name for member in slots(config)}:
        declared = ", ".join(member.name for member in slots(config)) or "none"
        raise ConsoleRefused(
            f"'{slot}' is not a declared console; the declared consoles: {declared}."
        )
