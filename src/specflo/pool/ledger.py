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
allows of it: a pool's ``size``, a member's ``capacity``, an account's
``cap``. A row's other columns are not read for the count.

A hosted member runs through a provider account, so a lease on it takes a
slot of the account too, and an account that two pools' members share is
counted once, as a shared member is. A local member runs through no account
and takes no slot of one.

A local member runs one model on the rig's cards, and llama-swap keeps loaded
side by side only the models that one expansion of one matrix set of its
configuration holds. So a lease on a local member takes its model too, and a
local member fits only when its model may be loaded beside every model under
lease, in whichever pool; a model is always at home beside itself. The pool
only reads that table. It asks llama-swap to load, unload or place nothing: a
request that does not fit waits for a lease to end. A hosted member runs on
no card here, so it is not checked and takes no model. A row written before a
lease took its model says nothing of one, and nothing is read into it.

A request also says which egress classes of member it accepts, and a member
of another class is not there for it: a free one is passed over and a busy
one is not waited for. A pool with no member of an accepted class has no room
to wait for, which is another refusal than a full pool's.

A member of capacity above 1 serves that many leases at once, and one agent
name runs one process, so each of them runs under a name of its own. The
first is the member's name. The further ones are the member's name with a
number after it, "m1.2", "m1.3", passing over every name the configuration
declares a member by and every name a lease runs under now. A lease that runs
under another name than its member's records it as a resource of the kind
``agent``; a row with none runs under the member's name, as every row did
before a member served more than one lease.

Not everything that runs on the rig and the accounts is a member. The daemon
runs an agent for each hosted project, outside every pool, and while that
agent is alive it stands in the ledger with the model or the account slot it
takes: a standing entry. Its resources are counted with the rows', so a local
member's model has to fit beside a standing model and a standing slot is one
out of its account's cap. It takes no pool's slot and no member. A standing
entry is no row: it has no idle limit and no state, nothing here ends it, and
a refusal it is part of says that it is the project's agent that holds what
is in the way, since no lease ending frees that.

An account can be closed: its provider gives it nothing more until a reset. A
member on a closed account serves no new lease, so it is passed over as one
of another class is, for the next member the pool lists. A request whose
every accepted member runs through a closed account has no room to wait for
either, and its refusal names each account and when it reopens. A lease that
is out on a closed account is a row like any other: nothing here ends it.

A member may be one that is not to be matched now: a console that no agent
is attached to, or one that was detached. It is passed over for the next
member the pool lists, and a pool whose other members are all full has no
room. That is a refusal a request waits on, since an attach cures it as a
lease ending cures a full pool. Which members those are is handed in by name;
the ledger reads no console's state.

``place`` is a function of the configuration, the lease rows, the standing
entries, the accounts closed now, the members not to be matched now and the
request. It opens no store, reads no clock and no file, and starts nothing:
the pool service hands it the rows, the entries, the closed accounts and
those members and acts on its answer.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from itertools import count

from ..daemon.poolstore import Lease, Resource
from ..errors import SpecfloError
from .config import LOCAL, Member, PoolConfig

# The kinds of resource a lease takes here. The account is on the row only for
# a hosted member, the model only for a local one, and the agent name only
# when it is not the member's name.
POOL = "pool"
MEMBER = "member"
ACCOUNT = "account"
MODEL = "model"
AGENT = "agent"

# The state of a lease that holds what it took.
ACTIVE = "active"


class NoRoom(SpecfloError):
    """A request that does not fit now; the message names what is full."""


class NoMemberAllowed(SpecfloError):
    """A request that no lease ending would make fit: every member of its pool
    is of an egress class it does not accept. The message names the classes."""


class AccountClosed(SpecfloError):
    """A request that no lease ending would make fit: every member of its pool
    that it accepts runs through a closed account. The message names each
    account and when it reopens."""


@dataclass(frozen=True)
class Request:
    """What is asked for: one member of the pool named, of one of the egress
    classes named. With no classes named a member of any class will do; the
    pool service works out which classes a request accepts and always names
    them."""

    pool: str
    egress: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Placement:
    """The answer to a request that fits: the member it gets, the name its
    agent runs under, and every resource the lease takes, for the lease row."""

    member: Member
    agent: str
    resources: tuple[Resource, ...]


@dataclass(frozen=True)
class Standing:
    """What a live project agent takes outside the pool: the project, the
    name its agent runs under, and the resources counted for it, a model or
    an account. The id tells it from a lease's wherever both are listed."""

    id: str
    project: str
    agent: str
    resources: tuple[Resource, ...]


def place(
    config: PoolConfig,
    leases: Iterable[Lease],
    request: Request,
    standing: Iterable[Standing] = (),
    closed: Mapping[str, str | None] | None = None,
    unmatched: Collection[str] = (),
) -> Placement:
    """Where *request* fits, given the *leases* that are out, the *standing*
    entries of the project agents that are alive, the accounts *closed* now,
    each with the time it reopens, or None when none is set, and the members
    *unmatched* now, by name: the consoles that no agent is attached to.

    The member is the first the pool lists, in its order, that is of a class
    the request accepts, serves fewer leases than its capacity and, if it is
    hosted, whose account has fewer leases out than its cap, in whichever
    pools; if it is local, whose model may be loaded beside every model under
    lease. A member of another class is passed over however free it is, and
    so is a member whose account is closed and one that is not to be matched.
    *leases* may hold rows that have ended; they count for nothing. A standing
    entry's account slot and model count as a lease's do.

    Raises ``NoRoom`` for a pool that is not declared, a pool with as many
    leases out as its size, and a pool whose members are all full, kept by a
    full account, kept by the leased models or not to be matched now, which
    it names. Raises
    ``NoMemberAllowed`` instead, and before anything is counted, for a pool
    with no member of a class the request accepts: that is not a matter of
    room. Nor is a pool whose accepted members all run through closed
    accounts, which raises ``AccountClosed``, before anything is counted too.
    """
    pool = next((p for p in config.pools if p.name == request.pool), None)
    if pool is None:
        raise NoRoom(f"pool '{request.pool}' is not declared.")
    members = {member.name: member for member in config.members}
    accepted = [
        name for name in pool.members
        if request.egress is None or members[name].egress in request.egress
    ]
    if not accepted:
        raise NoMemberAllowed(
            f"pool '{pool.name}' has no member of an egress class the request accepts, "
            + ", ".join(f"'{name}'" for name in request.egress) + ": "
            + ", ".join(f"'{name}' is class '{members[name].egress}'" for name in pool.members)
            + "."
        )
    # The closed accounts that keep an accepted member, and when each reopens.
    shut = {
        members[name].account: closed[members[name].account]
        for name in accepted if closed and members[name].account in closed
    }
    usable = [name for name in accepted if members[name].account not in shut]
    if not usable:
        raise AccountClosed(
            f"pool '{pool.name}' can be served only through a closed account now: "
            + ", ".join(f"'{name}'" for name in accepted) + " - " + _closed(shut)
            + ". No lease ending makes room for the request, so it does not wait."
        )
    out = [lease for lease in leases if lease.state == ACTIVE]
    stood = list(standing)
    # Whatever holds an account slot or a model: a lease that is out, or a
    # project's agent.
    holding = [*out, *stood]
    if _taken(out, POOL, pool.name) >= pool.size:
        raise NoRoom(f"pool '{pool.name}' is full: all {pool.size} of its leases are out.")
    accounts = {account.name: account for account in config.accounts}
    full: dict[str, int] = {}  # the accounts that keep a member with room, and their caps
    apart: dict[str, tuple[str, ...]] = {}  # the models kept off the rig, and by which
    for name in usable:
        member = members[name]
        if name in unmatched or _taken(out, MEMBER, name) >= member.capacity:
            continue
        resources = [Resource(POOL, pool.name), Resource(MEMBER, name)]
        if member.account is not None:
            account = accounts[member.account]
            if _taken(holding, ACCOUNT, account.name) >= account.cap:
                full[account.name] = account.cap
                continue
            resources.append(Resource(ACCOUNT, account.name))
        if member.backing == LOCAL and config.swap is not None:
            in_the_way = _in_the_way(config, member.model, holding)
            if in_the_way:
                apart[member.model] = in_the_way
                continue
            resources.append(Resource(MODEL, member.model))
        agent = _agent_name(member, config, out)
        if agent != name:
            resources.append(Resource(AGENT, agent))
        return Placement(member=member, agent=agent, resources=tuple(resources))
    # Only the members the request may have: the others are not what it waits for.
    listed = ", ".join(f"'{name}'" for name in accepted)
    kept = [_closed(shut)] if shut else []
    away = [f"'{name}'" for name in usable if name in unmatched]
    if away:
        kept.append(
            ", ".join(away) + " cannot be matched now: a console takes a lease only "
            "while an agent is attached to it and free to take one"
        )
    if full:
        kept.append(", ".join(
            f"account '{name}' is full: all {cap} of its leases are out"
            + _slots_of(_stood_on(stood, ACCOUNT, name))
            for name, cap in full.items()
        ) + ", in this pool or another")
    if apart:
        kept.append(", ".join(
            f"model '{model}' cannot be loaded beside " + _beside(stood, others)
            for model, others in apart.items()
        ))
    if kept:
        raise NoRoom(
            f"pool '{pool.name}' has no free member: {listed} - {'; '.join(kept)}, "
            "and any other member serves as many leases as it has capacity for."
        )
    raise NoRoom(
        f"pool '{pool.name}' has no free member: {listed} - each one serves as many "
        "leases as it has capacity for, in this pool or another."
    )


def agent_of(lease: Lease) -> str:
    """The name the agent of *lease* runs under: the one on its row, or its member's."""
    return next((r.name for r in lease.resources if r.kind == AGENT), lease.member)


def _taken(out: list[Lease | Standing], kind: str, name: str) -> int:
    """How many of the leases and standing entries *out* took the resource *kind* *name*."""
    return sum(1 for lease in out if Resource(kind, name) in lease.resources)


def _stood_on(stood: list[Standing], kind: str, name: str) -> list[str]:
    """The project agents that hold the resource *kind* *name* as standing
    entries, each as a refusal names it."""
    return [
        f"the agent of project '{entry.project}'"
        for entry in stood if Resource(kind, name) in entry.resources
    ]


def _closed(shut: Mapping[str, str | None]) -> str:
    """What a refusal says of the closed accounts *shut*, each with its reopen time."""
    return ", ".join(
        f"account '{name}' is closed "
        + (f"until {reopen}" if reopen is not None else "with no time set for it to reopen")
        for name, reopen in shut.items()
    )


def _slots_of(agents: list[str]) -> str:
    """What a full account's refusal adds for the *agents* that stand on it."""
    if not agents:
        return ""
    return f", counting the slot{'s' if len(agents) > 1 else ''} of " + " and ".join(agents)


def _beside(stood: list[Standing], others: tuple[str, ...]) -> str:
    """The models in the way, *others*, by what holds each: the leased ones,
    then each one a project's agent holds, which no lease ending frees."""
    held = {other: _stood_on(stood, MODEL, other) for other in others}
    leased = [f"'{other}'" for other in others if not held[other]]
    parts = ["the leased " + ", ".join(leased)] if leased else []
    parts += [
        f"'{other}', which {' and '.join(agents)} hold{'s' if len(agents) == 1 else ''}"
        for other, agents in held.items() if agents
    ]
    return " and ".join(parts)


def _in_the_way(config: PoolConfig, model: str, out: list[Lease | Standing]) -> tuple[str, ...]:
    """The models taken that keep *model* off the rig; none when it may be
    loaded beside every model the leases and standing entries *out* took.

    They are the ones it may not be loaded beside two by two, and all of them
    when it is only the whole group that fits in no combination."""
    leased = {r.name for lease in out for r in lease.resources if r.kind == MODEL}
    others = sorted(leased - {model})
    if not others or _together(config, [model, *others]):
        return ()
    return tuple(o for o in others if not _together(config, [model, o])) or tuple(others)


def _together(config: PoolConfig, models: list[str]) -> bool:
    """Whether the llama-swap configuration keeps *models* loaded side by side.
    A model it does not have is in no set of it, so it only runs alone."""
    try:
        return config.swap.fits(models)
    except SpecfloError:
        return False


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
