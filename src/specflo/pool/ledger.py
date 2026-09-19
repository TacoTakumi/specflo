"""The ledger: whether a request fits, which member it gets, and what the lease takes.

There is one ledger for the whole pool configuration. Every named pool draws
on it, so a member that two pools list is counted once: a lease on it through
one pool is in the way of a request to the other, whoever asks and from
whichever project. Nobody has a share set aside. A request says which pool it
wants and nothing of who asks, and neither the configuration nor the ledger
has a field for a quota or a reserved slot.

What is counted is what the lease rows say they took. A grant writes every
resource of its lease on the row, a kind and a name each, and the ledger
counts the active rows that name a resource against what the configuration
allows of it: a pool's ``size``, a member's ``capacity``. A row's other
columns are not read for the count.

A member of capacity above 1 serves that many leases at once, and one agent
name runs one process, so each of them runs under a name of its own. The
first is the member's name. The further ones are the member's name with a
number after it, "m1.2", "m1.3", passing over every name the configuration
declares a member by and every name a lease runs under now. A lease that runs
under another name than its member's records it as a resource of the kind
``agent``; a row with none runs under the member's name, as every row did
before a member served more than one lease.

``place`` is a function of the configuration, the lease rows and the request.
It opens no store, reads no clock and no file, and starts nothing: the pool
service hands it the rows and acts on its answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import count

from ..daemon.poolstore import Lease, Resource
from ..errors import SpecfloError
from .config import Member, PoolConfig

# The kinds of resource a lease takes here. The agent name is on the row only
# when it is not the member's name.
POOL = "pool"
MEMBER = "member"
AGENT = "agent"

# The state of a lease that holds what it took.
ACTIVE = "active"


class NoRoom(SpecfloError):
    """A request that does not fit now; the message names what is full."""


@dataclass(frozen=True)
class Request:
    """What is asked for: one member of the pool named."""

    pool: str


@dataclass(frozen=True)
class Placement:
    """The answer to a request that fits: the member it gets, the name its
    agent runs under, and every resource the lease takes, for the lease row."""

    member: Member
    agent: str
    resources: tuple[Resource, ...]


def place(config: PoolConfig, leases: Iterable[Lease], request: Request) -> Placement:
    """Where *request* fits, given the *leases* that are out.

    The member is the first the pool lists, in its order, that serves fewer
    leases than its capacity, in whichever pools. *leases* may hold rows that
    have ended; they count for nothing.

    Raises ``NoRoom`` for a pool that is not declared, a pool with as many
    leases out as its size, and a pool whose members are all full.
    """
    pool = next((p for p in config.pools if p.name == request.pool), None)
    if pool is None:
        raise NoRoom(f"pool '{request.pool}' is not declared.")
    out = [lease for lease in leases if lease.state == ACTIVE]
    if _taken(out, POOL, pool.name) >= pool.size:
        raise NoRoom(f"pool '{pool.name}' is full: all {pool.size} of its leases are out.")
    members = {member.name: member for member in config.members}
    for name in pool.members:
        member = members[name]
        if _taken(out, MEMBER, name) < member.capacity:
            agent = _agent_name(member, config, out)
            resources = [Resource(POOL, pool.name), Resource(MEMBER, name)]
            if agent != name:
                resources.append(Resource(AGENT, agent))
            return Placement(member=member, agent=agent, resources=tuple(resources))
    listed = ", ".join(f"'{name}'" for name in pool.members)
    raise NoRoom(
        f"pool '{pool.name}' has no free member: {listed} - each one serves as many "
        "leases as it has capacity for, in this pool or another."
    )


def agent_of(lease: Lease) -> str:
    """The name the agent of *lease* runs under: the one on its row, or its member's."""
    return next((r.name for r in lease.resources if r.kind == AGENT), lease.member)


def _taken(out: list[Lease], kind: str, name: str) -> int:
    """How many of the leases *out* took the resource *kind* *name*."""
    return sum(1 for lease in out if Resource(kind, name) in lease.resources)


def _agent_name(member: Member, config: PoolConfig, out: list[Lease]) -> str:
    """The first name free for one more agent of *member*."""
    running = {agent_of(lease) for lease in out}
    if member.name not in running:
        return member.name
    declared = {m.name for m in config.members}
    return next(
        name for name in (f"{member.name}.{n}" for n in count(2))
        if name not in declared and name not in running
    )
