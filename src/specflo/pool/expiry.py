"""When a lease has expired: a rule its reader applies, with nothing that reaps.

A lease is not renewed by a verb of its own. What renews it is what its
holder does on the member and a turn that runs there, and the member's agent
host stamps the time of both as ``last_activity`` in its status file. So the
last activity of a lease is the later of two times: the one on its row, set
at the grant, and the host's stamp. A member that is working counts as active
now, whatever its stamp says: the host moves the stamp when pi says
something, and a long tool call says nothing for as long as it runs.

A lease whose last activity lies its idle limit or more behind has expired.
Whoever reads a lease works that out; the pool service does, and ends the
lease, at the start of whatever it is next asked to do.

A team is renewed as one. Its member leases share a team lease id and an idle
limit, and the last activity a member lease is judged by is the latest among
the team's member leases that were read: a prompt to one member keeps every
member, and with nothing done anywhere they are all due at the same look. A
member's own last activity stays its own, for whoever asks how long that one
member has been idle.

The rule is a function of the lease rows, the status records and the time. It
reads no clock and no file: a status record is handed in as data, and
``None`` stands for a member with no status to go by. Whoever has the active
leases and their statuses can apply it, with no pool service at hand.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from ..daemon.poolstore import Lease

# The agent host's state for a member with a turn running.
WORKING = "working"

# A lease as it is read: its row, and its member's status record or None.
Read = tuple[Lease, Mapping[str, Any] | None]


def last_activity(lease: Lease, status: Mapping[str, Any] | None, now: datetime) -> datetime:
    """When the holder of *lease* last did something, or its member last worked.

    *status* is the status record of the member's agent host, or None.
    """
    if status is not None and status.get("state") == WORKING:
        return now
    seen = _time(lease.last_activity)
    stamped = _time(status.get("last_activity")) if status is not None else None
    if seen is None:
        raise ValueError(f"lease '{lease.id}': its last activity is not a time")
    return seen if stamped is None else max(seen, stamped)


def expired(lease: Lease, status: Mapping[str, Any] | None, now: datetime) -> bool:
    """Has *lease* been idle for its limit at the time *now*?"""
    return now - last_activity(lease, status, now) >= timedelta(seconds=lease.idle_limit)


def judged_activity(read: Iterable[Read], now: datetime) -> dict[str, datetime]:
    """The last activity each lease of *read* is judged by, by lease id: its
    own, and for a member lease of a team the latest among the team's leases
    in *read*. Hand in the active leases; one that has ended renews no one."""
    own = [(lease, last_activity(lease, status, now)) for lease, status in read]
    latest: dict[str, datetime] = {}
    for lease, seen in own:
        if lease.team_lease_id is not None:
            team = lease.team_lease_id
            latest[team] = max(seen, latest.get(team, seen))
    return {
        lease.id: seen if lease.team_lease_id is None else latest[lease.team_lease_id]
        for lease, seen in own
    }


def due(read: Iterable[Read], now: datetime) -> list[Lease]:
    """The leases of *read* idle for their limit at the time *now*, in the
    order read. A team's member leases share their limit and the time they
    are judged by, so they are all due at the same look or none is."""
    read = list(read)
    judged = judged_activity(read, now)
    return [
        lease for lease, _ in read
        if now - judged[lease.id] >= timedelta(seconds=lease.idle_limit)
    ]


def _time(text: Any) -> datetime | None:
    """*text* as a time: ISO 8601, in UTC when it names no zone. None when it is not one."""
    if not isinstance(text, str):
        return None
    try:
        time = datetime.fromisoformat(text)
    except ValueError:
        return None
    return time if time.tzinfo is not None else time.replace(tzinfo=timezone.utc)
