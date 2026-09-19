"""Which leases a waiting request may take: a rule its reader applies.

A pool may declare ``preempt_after``. A lease of such a pool may be taken by
a request that waits, once the lease has been idle for longer than that and
while its member runs no turn. A lease of a pool that declares none is never
taken, however long it has been idle. Nothing ranks one request above
another: what may be taken is a matter of the lease alone, and who takes it
is whoever waits for it first.

The idle time is the lease's own: the time since its last activity, which is
the later of its row's time and its host's stamp (see ``expiry``). A member
in a turn counts as active now by that rule already. The state is asked here
as well, so that a lease is never taken in mid-turn whatever is made of its
times.

A team is taken as one or not at all. Its unit is every active member lease
of the team, and the unit may be taken only when every one of them may be,
each by the value of its own pool and by its own idle time. That is not the
time the team is kept by: what one member does renews the whole team against
expiry, and it leaves the other members as idle as they were here. So a team
with one pool that declares no ``preempt_after``, one member active inside
its pool's value, or one member in a turn is left whole. A unit is as idle as
the member of it that was active last, and its leases come the longest idle
first. That is the order to end them in: the member that keeps the team goes
last, so no other member reads as expired while the team is being ended.

What is taken is the fewest units that make the request fit, the longest
idle first. Whether a request fits with some leases gone is not known here:
the one who asks hands in that answer as a function, and the pool service has
it from the ledger.

The rule is a function of the lease rows, the status records, the pools and
the time. It reads no clock and no file and ends nothing: whoever has the
active leases and their statuses can apply it, with no pool service at hand.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any

from ..daemon.poolstore import Lease
from . import expiry
from .config import Pool


@dataclass(frozen=True)
class Unit:
    """What is taken as one: a lease of no team, or every active member lease
    of the team *team_lease_id*, the longest idle first. *idle* is how long
    it has been idle: the shortest idle time among its leases."""

    team_lease_id: str | None
    leases: tuple[Lease, ...]
    idle: timedelta


def idle_time(lease: Lease, status: Mapping[str, Any] | None, now: datetime) -> timedelta:
    """How long *lease* has been idle at the time *now*, by its own last
    activity; no time at all for a member in a turn."""
    return now - expiry.last_activity(lease, status, now)


def eligible(
    lease: Lease, status: Mapping[str, Any] | None, pool: Pool | None, now: datetime
) -> bool:
    """May *lease*, taken by itself, be ended for a waiting request at the
    time *now*? *pool* is the pool it was leased from, None when that pool is
    not declared any more; *status* is its member's status record, or None."""
    if pool is None or pool.preempt_after is None:
        return False
    if status is not None and status.get("state") == expiry.WORKING:
        return False
    return idle_time(lease, status, now) > timedelta(seconds=pool.preempt_after)


def units(read: Iterable[expiry.Read], pools: Iterable[Pool], now: datetime) -> list[Unit]:
    """The units of *read* that may be taken at the time *now*, the longest
    idle first, and in the order read among those idle alike. Hand in every
    active lease: a team is judged by all of its member leases in *read*."""
    by_name = {pool.name: pool for pool in pools}
    grouped: dict[tuple[str | None, str], list[expiry.Read]] = {}
    for lease, status in read:
        # a lease of no team is a unit by itself
        team = lease.team_lease_id
        grouped.setdefault((team, "" if team else lease.id), []).append((lease, status))
    found = []
    for members in grouped.values():
        if not all(
            eligible(lease, status, by_name.get(lease.pool), now) for lease, status in members
        ):
            continue
        idle = sorted(
            ((idle_time(lease, status, now), lease) for lease, status in members),
            key=lambda timed: timed[0], reverse=True,
        )
        found.append(Unit(
            team_lease_id=idle[0][1].team_lease_id,
            leases=tuple(lease for _, lease in idle), idle=idle[-1][0],
        ))
    return sorted(found, key=lambda unit: unit.idle, reverse=True)


def fewest(
    units: Sequence[Unit], fits: Callable[[Collection[str]], bool]
) -> tuple[Unit, ...]:
    """The fewest of *units* to end so that the request fits; none when no
    set of them makes it fit. *fits* says whether the request fits with the
    leases of those ids gone. *units* come the longest idle first, and among
    sets of one size the first that fits is of the longest idle."""
    if not units or not fits(_ids(units)):
        return ()
    for size in range(1, len(units)):
        for chosen in combinations(units, size):
            if fits(_ids(chosen)):
                return chosen
    return tuple(units)


def _ids(units: Iterable[Unit]) -> frozenset[str]:
    return frozenset(lease.id for unit in units for lease in unit.leases)
