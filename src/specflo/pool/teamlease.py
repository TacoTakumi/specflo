"""A team's lease: every role member an ordinary lease, all under one team lease id.

A request may name a team instead of a pool. A team is a set of roles, each a
pool and a count (see ``teams``), and it is leased all or nothing: the pool
service grants it only when every member of every role fits the ledger at
once, and until then the request holds nothing. A team that waits for one
full pool takes no member of another, so it is in no one's way while it waits.

A granted team is as many leases as its roles count members, each with a
lease id, an agent name and a token of its own, written and started by the
very grant a single lease gets. What makes them a team is the one team lease
id on every row. The orchestrator that asked holds every token and leads the
team; the pool gives the members no way to reach each other.

The team is given back as one. A release of the team lease id ends every
member lease that is still active, each through the one function that ends a
lease. A release of one member lease by its holder is refused, with the team
lease id named, since a team short of a member is not the team that was
asked for. The refusal stands on the holder's release only: a member that
does not start, and whatever else ends a lease from inside the pool, ends
member leases one by one.

The team is kept as one too. Every member lease has the same idle limit: the
shortest default among the team's pools, or what the request asks, which the
shortest maximum among them must allow. What any member does renews every
member, and with nothing done they all expire at once; that is a rule of how
the team's leases are read, and it stands with the other rules of expiry (see
``expiry``).

This module holds what is the team's own: what a granted team is, the member
slots its roles come to, the leases that stand for members placed and not yet
written, the idle limit of its member leases, and its release. Whether the
team fits is the ledger's to say, asked by the pool service, which also makes
the grant. Nothing here runs by itself and nothing here imports anything of
the agent subsystem; the service a team is released through is handed in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..daemon.poolstore import Lease
from ..errors import SpecfloError
from . import expiry, ledger
from .config import Pool
from .teams import Role, Team

if TYPE_CHECKING:
    from .service import Ended, PoolService

# What a team member's lease is ended with when another member of its team did
# not start, and the whole grant goes back.
ROLLED_BACK = "a member of its team did not start"


class TeamNeverFits(SpecfloError):
    """A team that would not fit with no lease out at all. No lease ending
    would cure it, so the request is refused at once and does not wait."""


class MemberOfATeam(SpecfloError):
    """A holder's release of one member lease of a team. The team is released
    as one, under its team lease id, which the message names."""


@dataclass(frozen=True)
class MemberGrant:
    """One role member of a granted team: an ordinary lease, the agent to
    drive and the token that lets the team's holder, and no one else, drive it."""

    role: str
    pool: str
    lease_id: str
    agent: str
    token: str


@dataclass(frozen=True)
class TeamGrant:
    """What a granted team request gets: the one id its leases share, and the
    members in the order of the team's roles."""

    team_lease_id: str
    members: tuple[MemberGrant, ...]


def slots(team: Team) -> Iterator[Role]:
    """The member slots of *team*, one for each lease it takes: every role as
    many times as its count, in the order the roles are written."""
    for role in team.roles:
        for _ in range(role.count):
            yield role


def placed_lease(pool: str, placement: ledger.Placement) -> Lease:
    """The lease that stands for a member placed and not yet written, for the
    ledger to count when it places the next one: the row the grant would
    write, with no holder and no times."""
    return Lease(
        id="", team_lease_id=None, holder_hash="", holder_label="",
        member=placement.member.name, pool=pool, resources=placement.resources,
        acquired="", last_activity="", idle_limit=0, state=ledger.ACTIVE,
    )


def idle_limits(
    pools: Iterable[Pool], asked: int | None, limit_of: Callable[[Pool, int | None], int]
) -> dict[str, int]:
    """The idle limit of a member lease of a team on each of *pools*, by pool
    name, in seconds: one limit, the same on every pool. *limit_of* is the
    pool service's rule for one lease on one pool: what was *asked* when the
    pool allows it, or the pool's default.

    With nothing asked the limit is the shortest default among the pools.
    What is asked is put to the pool whose maximum is the shortest, so a
    refusal names that maximum and the pool that sets it. This is worked out
    before anything is granted, so a limit that is refused leaves nothing
    behind."""
    pools = list(pools)
    if asked is None:
        limit = min(limit_of(pool, None) for pool in pools)
    else:
        limit = limit_of(min(pools, key=lambda pool: pool.idle_max), asked)
    return {pool.name: limit for pool in pools}


def refuse_member_release(lease: Lease) -> None:
    """Refuse a holder's release of *lease* when it is an active member lease
    of a team; a lease of no team, and one that has ended, passes."""
    if lease.team_lease_id is None or lease.state != ledger.ACTIVE:
        return
    raise MemberOfATeam(
        f"Lease '{lease.id}' is one member of the team lease '{lease.team_lease_id}', and a "
        "team is released as one. Release the team: "
        f"`specflo lease release {lease.team_lease_id}`."
    )


def team_leases(service: PoolService, team_lease_id: str) -> list[Lease]:
    """Every lease of the team *team_lease_id*, ended ones too, in grant order;
    none for an id that is no team's."""
    with service.open_store() as store:
        return store.list_leases(team_lease_id=team_lease_id)


def release_team(service: PoolService, team_lease_id: str) -> list[Ended]:
    """Release the team *team_lease_id* as one: how each member lease ended,
    in grant order. A member that ended before is reported as it ended.

    Every member is ended though one of them does not stop; the first
    ``RunnerError`` is raised when all have ended.
    """
    return end_members(
        service, [lease.id for lease in team_leases(service, team_lease_id)], "released"
    )


def end_members(
    service: PoolService,
    lease_ids: Iterable[str],
    kind: str,
    *,
    cause: str | None = None,
    request_id: str | None = None,
) -> list[Ended]:
    """End each of *lease_ids* as *kind*, through the service's one function
    that ends a lease; *request_id* is the request that took them, for members
    that are preempted. How each ended, in the order of *lease_ids*. A member
    whose process does not stop has ended all the same, so the others are
    ended too before that failure is raised.

    The members of a team are ended with the one that keeps it last (see
    ``keeper_last``), so that a team ended inside its limit has no member
    found expired on the way. When the leases cannot be read for that, they
    are ended in the order given."""
    lease_ids = list(lease_ids)
    ended, failed = {}, None
    try:
        with service.open_store() as store:
            out = [
                lease for lease in store.list_leases(state="active") if lease.id in lease_ids
            ]
        order = keeper_last(lease_ids, service.read(out), service.clock())
    except SpecfloError as exc:
        order, failed = lease_ids, exc
    for lease_id in order:
        try:
            ended[lease_id] = service.end_lease(
                lease_id, kind, cause=cause, request_id=request_id
            )
        except SpecfloError as exc:
            failed = failed or exc
    if failed is not None:
        raise failed
    return [ended[lease_id] for lease_id in lease_ids]


def keeper_last(
    lease_ids: Iterable[str], read: Iterable[expiry.Read], now: datetime
) -> list[str]:
    """*lease_ids* with the member leases of each team put the longest idle
    first at the time *now*, in the places that team's leases hold. *read* is
    the active leases among them, each with its member's status. An id of no
    team, and one that is not in *read*, stays where it is; members idle alike
    keep their order.

    The expiry check runs before each ending, and a team is judged by the
    latest activity among its member leases that are still active. A member
    that kept the team and ended before the others would leave them to be
    judged without it, and a team given back inside its limit would be
    recorded, stopped and told of as expired. Ended last, it is there for
    every look. A team that is past its limit has expired whole at the first
    look, as ever."""
    lease_ids = list(lease_ids)
    seen = {
        lease.id: (lease.team_lease_id, expiry.last_activity(lease, status, now))
        for lease, status in read if lease.team_lease_id is not None
    }
    places: dict[str, list[int]] = {}
    for place, lease_id in enumerate(lease_ids):
        if lease_id in seen:
            places.setdefault(seen[lease_id][0], []).append(place)
    for held in places.values():
        members = sorted((lease_ids[place] for place in held), key=lambda member: seen[member][1])
        for place, lease_id in zip(held, members):
            lease_ids[place] = lease_id
    return lease_ids
