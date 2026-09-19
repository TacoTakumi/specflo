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
that waits for its slot, with no other client and no reaper.

The order among those that wait is the service's: a grant gives way to the
requests that arrived before it and fit now. This module writes the row that
order is read from, and takes it out again.

The time a request may wait is counted on the service's clock, so a test
drives it with a fake one.
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

# How long whoever serves a waiting request pauses between two looks, in
# seconds. A slot that frees is granted within this, and the time the member
# before took to stop.
POLL_INTERVAL = 1.0


class WaitTimeout(SpecfloError):
    """A request that waited its time out and was not granted."""


def _mint_id() -> str:
    return f"request-{secrets.token_hex(8)}"


@dataclass
class Waiting:
    """One request for a member of *pool*, and the *wait* it may last, in seconds.

    The rest is what the grant takes. With no time to wait the request is
    refused as the service refuses it, and no row is written.
    """

    service: PoolService
    pool: str
    holder_label: str
    cwd: Path | str
    idle_limit: int | None = None
    wait: float = 0
    mint_id: Callable[[], str] = _mint_id
    # The id of the request's row while it has one, and when its time is up.
    _id: str | None = field(default=None, init=False, repr=False)
    _until: datetime | None = field(default=None, init=False, repr=False)

    def attempt(self) -> Grant | None:
        """One look: the grant, or None for a request that waits on.

        The first look that finds no room writes the waiting row. Raises
        ``WaitTimeout``, naming what the pool is full of, at the first look
        at or after the time is up; the row is gone then. Raises whatever the
        grant raises besides; ``leave`` takes the row out after those.
        """
        try:
            grant = self.service.grant(
                self.pool, holder_label=self.holder_label, cwd=self.cwd,
                idle_limit=self.idle_limit, waiting_id=self._id,
            )
        except NoFreeMember as full:
            return self._wait_on(full)
        # the grant took the row out
        self._id = None
        return grant

    def leave(self) -> None:
        """Stop waiting: the row goes. Nothing to do for a request with none."""
        if self._id is None:
            return
        with self.service.open_store() as store:
            store.remove_waiting(self._id)
        self._id = None

    def _wait_on(self, full: NoFreeMember) -> None:
        now = self.service.clock()
        if self._id is None:
            if self.wait <= 0:
                raise full
            request_id = self.mint_id()
            with self.service.open_store() as store:
                store.add_waiting(WaitingRequest(
                    id=request_id, pool=self.pool, team=None,
                    holder_label=self.holder_label, arrived=_text(now),
                ))
            self._id, self._until = request_id, now + timedelta(seconds=self.wait)
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
