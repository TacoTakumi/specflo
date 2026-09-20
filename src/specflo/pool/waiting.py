"""A request that does not fit waits: a row in the store, and an asker that comes back.

A request the pool cannot grant now is refused, unless its asker gave it a
time to wait. Then it is written down in the store as waiting, under an id of
its own and the time it arrived, and the asker asks again at a short
interval until the request is granted, its time is up, or the asker goes
away. Whichever of those ends the wait, the row goes with it.

Nothing here runs by itself and nothing here sleeps. A ``Waiting`` is one
request, and ``attempt`` is one look at it: whoever serves the request - the
daemon's route - calls it, pauses, and calls it again. Each look is a grant
asked of the pool service, and the service checks expiry before it serves
anything. So a lease that passes its idle limit is ended by the very request
that waits for its slot, with no other client and no reaper. A look of a
request that has its row may also take an idle lease that stands in its way,
when the lease's pool allows that; the service decides it (see ``preempt``),
and the row's id is the request the ended lease records.

The order among those that wait is the service's: a grant gives way to the
requests that arrived before it and fit now. This module writes the row that
order is read from, and takes it out again. The row keeps the egress class
the request named, or that it named none, and the class its project pinned
when it arrived, so the service judges whether it fits as it judges the
request itself.

A request that waits can be told so: it keeps what the pool was full of, in
the service's own words, and ``place`` reads where it stands among those that
wait for its pool.

A request may name a team instead of a pool. It waits the same way, as one
row that names the team and no pool, and each look asks the service for the
whole team. A team draws on several pools, so its place is read among all
that wait.

The time a request may wait is counted on the service's clock, so a test
drives it with a fake one. It is no more than ``WAIT_MAX``, which whoever
serves the request holds it to. The row keeps the time the wait is up, so a
row whose asker went away and did not take it out holds up no one for long:
the service passes it over and takes it out some time after that (see
``service``).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from ..daemon.poolstore import WaitingRequest
from ..errors import SpecfloError
from .service import Grant, NoFreeMember, PoolService, _text
from .teamlease import TeamGrant

# How long whoever serves a waiting request pauses between two looks, in
# seconds. A slot that frees is granted within this, and the time the member
# before took to stop.
POLL_INTERVAL = 1.0

# The longest a request may ask to wait, in seconds: one day. No one waits on
# a pool for longer, and a time this far off is one the clock can count to.
WAIT_MAX = 86400


class WaitTimeout(SpecfloError):
    """A request that waited its time out and was not granted."""


def _mint_id() -> str:
    return f"request-{secrets.token_hex(8)}"


@dataclass
class Waiting:
    """One request for a member of *pool*, or for the whole of *team* when
    *pool* is None, and the *wait* it may last, in seconds.

    The rest is what the grant takes. With no time to wait the request is
    refused as the service refuses it, and no row is written.
    """

    service: PoolService
    pool: str | None
    holder_label: str
    cwd: Path | str
    idle_limit: int | None = None
    wait: float = 0
    # The most open egress class of member the request takes; the grant's default without one.
    egress: str | None = None
    # The class the requesting project pins, as its record said when the request
    # arrived, and that project; neither for a request with no pin over it.
    pinned: str | None = None
    project: str | None = None
    # The team asked for, by a request that names no pool.
    team: str | None = None
    mint_id: Callable[[], str] = _mint_id
    # What the pool was full of at the last look that found no room, as the service said it.
    full: str | None = field(default=None, init=False)
    # The id of the request's row while it has one, and when its time is up.
    _id: str | None = field(default=None, init=False, repr=False)
    _until: datetime | None = field(default=None, init=False, repr=False)

    def attempt(self) -> Grant | TeamGrant | None:
        """One look: the grant, a team's for a request that names one, or None
        for a request that waits on.

        The first look that finds no room writes the waiting row. Raises
        ``WaitTimeout``, naming what the pool is full of, at the first look
        at or after the time is up; the row is gone then. Raises whatever the
        grant raises besides; ``leave`` takes the row out after those.
        """
        ask = self.service.grant if self.team is None else self.service.grant_team
        try:
            grant = ask(
                self.pool if self.team is None else self.team,
                holder_label=self.holder_label, cwd=self.cwd,
                idle_limit=self.idle_limit, waiting_id=self._id, egress=self.egress,
                pinned=self.pinned, project=self.project,
            )
        except NoFreeMember as full:
            return self._wait_on(full)
        # the grant took the row out
        self._id = None
        return grant

    def place(self) -> int | None:
        """Where the request stands among those that wait for its pool, in
        arrival order: 1 for the next; a team's request stands among all that
        wait. None for a request that does not wait."""
        if self._id is None:
            return None
        with self.service.open_store() as store:
            ahead = [row.id for row in store.list_waiting(pool=self.pool)]
        return ahead.index(self._id) + 1 if self._id in ahead else None

    def leave(self) -> None:
        """Stop waiting: the row goes. Nothing to do for a request with none."""
        if self._id is None:
            return
        with self.service.open_store() as store:
            store.remove_waiting(self._id)
        self._id = None

    def _wait_on(self, full: NoFreeMember) -> None:
        now = self.service.clock()
        self.full = str(full)
        if self._id is None:
            if self.wait <= 0:
                raise full
            # Worked out before the row is written: whatever fails here leaves no row.
            request_id, until = self.mint_id(), now + timedelta(seconds=self.wait)
            with self.service.open_store() as store:
                store.add_waiting(WaitingRequest(
                    id=request_id, pool=self.pool, team=self.team,
                    holder_label=self.holder_label, arrived=_text(now), egress=self.egress,
                    pinned=self.pinned, until=_text(until),
                ))
            self._id, self._until = request_id, until
        elif now >= self._until:
            self.leave()
            raise WaitTimeout(f"Waited {self.wait:g} s and was not granted: {full}") from full
        return None


def forget_all(service: PoolService) -> int:
    """Take out every waiting row; how many there were.

    For when the pool opens: a request waits on a connection to the daemon
    that wrote its row, so the rows a stopped daemon left wait for no one,
    and each would be given way to for ever.
    """
    with service.open_store() as store:
        rows = store.list_waiting()
        for row in rows:
            store.remove_waiting(row.id)
    return len(rows)
