"""The pool service: a lease is granted on a free member and ended in one place.

A consumer asks for a pool by name. The service asks the ledger whether the
request fits and which member it gets, writes the lease row with every
resource the ledger says the lease takes, starts a fresh pi for the member
through the runner and records the grant. What goes back is the lease's id,
the name of the agent to drive and the lease token; the store keeps only the
token's hash. The row is written before the process starts, so there is never
a member running that no lease accounts for.

However a lease ends - its holder releases it, it expires, a waiting request
preempts it - it ends in ``end_lease`` and nowhere else. The move out of the
active state is one guarded write in the store, so of two callers ending one
lease one wins: the winner stops the member's process, and the agent host
aborts a running turn before it stops pi. The other caller, and anyone ending
a lease that has ended, gets the recorded end state and changes nothing.

Nothing here runs in the background, so no lease expires by itself. Each
entry point begins with ``expire_due``: the leases idle for their limit, by
the rule in ``expiry``, are ended as expired before the caller is served. An
expired lease therefore stands as active until the pool is next asked for
something, and is never counted against the one who asks.

Grants and endings take turns, one at a time in this process. A member's slot
reads as free from the moment its lease leaves the active state, while its
process may take some seconds more to stop; a grant that ran in between would
find the member still running.

Whether a request fits is the ledger's to say and nothing here counts: the
service reads the active lease rows, hands them over with the configuration
and the request, and acts on the answer (see ``ledger``). A member of
capacity above 1 serves several leases, each under an agent name the ledger
chose and the lease row keeps, and that is the name a lease's process is
stopped and its status read by.

Requests that did not fit wait as rows in the store, in the order they came.
A grant gives way to every request that came before its own and fits now, so
the earliest waiting request that fits is the one served, and one that does
not fit holds up no one behind it. The service only reads those rows and
takes the granted one out; the request that waits writes its row and comes
back to ask again (see ``waiting``).

The service reaches the agent subsystem through the runner only. It keeps no
clock and mints nothing by itself: the time, lease ids and lease tokens come
from callables handed in, so a test drives it with a fake clock and counted
ids.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from ..daemon.poolstore import (
    TRANSITION_KINDS,
    Lease,
    PoolStore,
    Transition,
    WaitingRequest,
)
from ..errors import SpecfloError
from . import expiry, ledger, runner
from .config import Pool, PoolConfig

# The ways a lease ends; each is a transition kind and the state it leaves.
ENDINGS: tuple[str, ...] = tuple(kind for kind in TRANSITION_KINDS if kind != "granted")

# The time now, timezone-aware. Passed in, so a test hands over a fake one.
Clock = Callable[[], datetime]


class UnknownPool(SpecfloError):
    """A request for a pool the configuration does not declare."""


class UnknownLease(SpecfloError):
    """A lease id the store holds no lease under."""


class IdleLimitError(SpecfloError):
    """An idle limit the pool does not allow."""


class NoFreeMember(SpecfloError):
    """A pool with no slot or no member free for one more lease."""


@dataclass(frozen=True)
class Grant:
    """What a granted request gets: the lease, the agent to drive, and the
    token that lets its holder, and no one else, drive it."""

    lease_id: str
    agent: str
    token: str


@dataclass(frozen=True)
class Ended:
    """How a lease ended, as the store recorded it: the end state, the cause
    and the time."""

    lease_id: str
    state: str
    cause: str
    time: str


def hash_token(token: str) -> str:
    """What the store keeps of a lease token: its SHA-256, in hex."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mint_id() -> str:
    return f"lease-{secrets.token_hex(8)}"


def _mint_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class PoolService:
    """The pool of one daemon. ``open_store`` opens the pool store for one unit
    of work; ``pool_token`` is the daemon's own credential on its members'
    hosts, and ``config_root`` is where a hosted member's pi configuration
    directory is generated."""

    config: PoolConfig
    open_store: Callable[[], PoolStore]
    pool_token: str
    config_root: Path | str
    clock: Clock = _now
    mint_id: Callable[[], str] = _mint_id
    mint_token: Callable[[], str] = _mint_token
    # What a member's scoped environment is taken from; this process's when None.
    environ: Mapping[str, str] | None = None
    _turn: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    # True while the expiry check runs: the endings it makes do not check again.
    _checking: bool = field(default=False, init=False, repr=False)

    def expire_due(self) -> list[Ended]:
        """End every lease that has been idle for its limit; the endings made.

        Each entry point of the service runs this first, and so does whatever
        renders the pool's state. A lease's last activity is read from its
        row and from the status of its member's agent host.

        Raises ``RunnerError`` when an expired lease's member does not stop;
        that lease has ended all the same.
        """
        with self._turn:
            if self._checking:
                return []
            self._checking = True
            try:
                now = self.clock()
                with self.open_store() as store:
                    due = [
                        lease.id for lease in store.list_leases(state="active")
                        if expiry.expired(lease, runner.status(ledger.agent_of(lease)), now)
                    ]
                return [self.end_lease(lease_id, "expired") for lease_id in due]
            finally:
                self._checking = False

    def grant(
        self,
        pool_name: str,
        *,
        holder_label: str,
        cwd: Path | str,
        idle_limit: int | None = None,
        waiting_id: str | None = None,
    ) -> Grant:
        """Lease a free member of the pool *pool_name* to *holder_label*.

        The member's pi runs in *cwd*. *idle_limit* is in seconds; without
        one the pool's default applies. *waiting_id* is the id of the
        request's own waiting row, when it has one: the requests that wait
        ahead of that row go first, all of them when there is none, and a
        grant takes the row out.

        Raises ``UnknownPool``, ``IdleLimitError`` for a limit above the
        pool's maximum, ``NoFreeMember`` also when an earlier request is
        served first, and what the runner raises for a member that does not
        start; the lease written for it is then ended.
        """
        self.expire_due()
        pool = self._pool(pool_name)
        idle_limit = _idle_limit(pool, idle_limit)
        definition = next(d for d in self.config.definitions if d.name == pool.definition)
        with self._turn, self.open_store() as store:
            self._give_way(pool, store, waiting_id)
            placed = self._place(pool.name, store)
            if waiting_id is not None:
                store.remove_waiting(waiting_id)
            lease_id, token = self.mint_id(), self.mint_token()
            now = _text(self.clock())
            store.add_lease(Lease(
                id=lease_id, team_lease_id=None,
                holder_hash=hash_token(token), holder_label=holder_label,
                member=placed.member.name, pool=pool.name, resources=placed.resources,
                acquired=now, last_activity=now, idle_limit=idle_limit, state="active",
            ))
            try:
                # The runner names the agent for the member it is handed, so a
                # further lease on a member is handed over under its own name.
                agent = runner.start(
                    definition, replace(placed.member, name=placed.agent), self.config.accounts,
                    cwd=cwd, pool_token=self.pool_token, lease_token=token,
                    config_root=self.config_root, environ=self.environ,
                )
            except Exception as exc:
                self.end_lease(lease_id, "released", cause=f"member did not start: {exc}")
                raise
            store.record_transition(
                Transition(id=0, lease_id=lease_id, kind="granted", time=now, cause="requested"),
                expect="active",
            )
        return Grant(lease_id=lease_id, agent=agent, token=token)

    def end_lease(
        self,
        lease_id: str,
        kind: str,
        *,
        cause: str | None = None,
        request_id: str | None = None,
    ) -> Ended:
        """End the lease *lease_id*; every ending comes through here.

        *kind* is how it ends: released, expired or preempted, the last with
        the preempting request's *request_id*. *cause* is the record's own
        wording of why; without one the kind stands for it. A running turn is
        aborted and the member's process stopped, the slot is free to the
        next request, and the former holder's next verb is told the kind.

        A lease that has ended already is left as it is, and how it ended is
        returned. Raises ``UnknownLease``, and ``RunnerError`` when the
        member's host does not stop; the lease has ended all the same.
        """
        if kind not in ENDINGS:
            raise ValueError(f"a lease does not end as {kind!r}: one of {ENDINGS}")
        if kind == "preempted" and not request_id:
            raise ValueError("a preempted lease records the preempting request's id")
        if cause is None:
            cause = f"preempted by {request_id}" if kind == "preempted" else kind
        self.expire_due()
        with self._turn, self.open_store() as store:
            ending = Transition(
                id=0, lease_id=lease_id, kind=kind, time=_text(self.clock()), cause=cause
            )
            if store.record_transition(ending, expect="active") is None:
                return _recorded_end(store, lease_id)
            # The host aborts a running turn itself before it stops pi.
            agent = ledger.agent_of(store.get_lease(lease_id))
            runner.stop(agent, kind, pool_token=self.pool_token, request_id=request_id)
        return Ended(lease_id=lease_id, state=kind, cause=cause, time=ending.time)

    def _pool(self, name: str) -> Pool:
        for pool in self.config.pools:
            if pool.name == name:
                return pool
        declared = ", ".join(p.name for p in self.config.pools) or "none"
        raise UnknownPool(f"pool '{name}' is not declared; the declared pools: {declared}.")

    def _give_way(self, pool: Pool, store: PoolStore, waiting_id: str | None) -> None:
        """Refuse a request for *pool* while a request that came before it fits.

        The requests before it are the rows ahead of its own, *waiting_id*,
        and every row when it has none. One that does not fit is passed over.
        """
        for ahead in store.list_waiting():
            if ahead.id == waiting_id:
                return
            if self._fits(ahead, store):
                raise NoFreeMember(
                    f"pool '{pool.name}': a request that came earlier, for pool "
                    f"'{ahead.pool}', is served first."
                )

    def _fits(self, request: WaitingRequest, store: PoolStore) -> bool:
        """Could the waiting *request* be granted now? One that names no
        declared pool could not."""
        try:
            self._place(request.pool, store)
        except NoFreeMember:
            return False
        return True

    def _place(self, pool_name: str, store: PoolStore) -> ledger.Placement:
        """Where a request for the pool *pool_name* fits now, as the ledger
        answers from the active lease rows. Raises ``NoFreeMember``."""
        try:
            return ledger.place(
                self.config, store.list_leases(state="active"), ledger.Request(pool=pool_name)
            )
        except ledger.NoRoom as full:
            raise NoFreeMember(str(full)) from full


def _idle_limit(pool: Pool, asked: int | None) -> int:
    """The idle limit of a lease on *pool*, in seconds: what was asked, or the default."""
    if asked is None:
        return pool.idle_default
    if asked < 1:
        raise IdleLimitError(f"an idle limit of {asked} s is not a time a lease can stay idle.")
    if asked > pool.idle_max:
        raise IdleLimitError(
            f"an idle limit of {_duration(asked)} is above the maximum of pool "
            f"'{pool.name}', {_duration(pool.idle_max)}."
        )
    return asked


def _recorded_end(store: PoolStore, lease_id: str) -> Ended:
    """How the lease *lease_id* ended, from its row and the transition that ended it."""
    lease = store.get_lease(lease_id)
    if lease is None:
        raise UnknownLease(f"there is no lease '{lease_id}'.")
    last = [t for t in store.list_transitions(lease_id=lease_id) if t.kind == lease.state][-1]
    return Ended(lease_id=lease_id, state=lease.state, cause=last.cause, time=last.time)


def _text(time: datetime) -> str:
    """*time* as the pool store keeps times: ISO 8601 in UTC, to the millisecond."""
    return time.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _duration(seconds: int) -> str:
    """*seconds* as the pool file writes a limit: "4h", "10m" or "45s"."""
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
