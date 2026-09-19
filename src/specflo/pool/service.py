"""The pool service: a lease is granted on a free member and ended in one place.

A consumer asks for a pool by name. The service finds a member of that pool
with no lease on it, writes the lease row, starts a fresh pi for the member
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
Nothing here runs in the background: a lease ends when a caller ends it.

Grants and endings take turns, one at a time in this process. A member's slot
reads as free from the moment its lease leaves the active state, while its
process may take some seconds more to stop; a grant that ran in between would
find the member still running.

A member serves one lease at a time here whatever capacity it declares: the
agent is named for the member, and one name runs one process.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..daemon.poolstore import TRANSITION_KINDS, Lease, PoolStore, Resource, Transition
from ..errors import SpecfloError
from . import runner
from .config import Member, Pool, PoolConfig

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

    def grant(
        self,
        pool_name: str,
        *,
        holder_label: str,
        cwd: Path | str,
        idle_limit: int | None = None,
    ) -> Grant:
        """Lease a free member of the pool *pool_name* to *holder_label*.

        The member's pi runs in *cwd*. *idle_limit* is in seconds; without
        one the pool's default applies.

        Raises ``UnknownPool``, ``IdleLimitError`` for a limit above the
        pool's maximum, ``NoFreeMember``, and what the runner raises for a
        member that does not start; the lease written for it is then ended.
        """
        pool = self._pool(pool_name)
        idle_limit = _idle_limit(pool, idle_limit)
        definition = next(d for d in self.config.definitions if d.name == pool.definition)
        with self._turn, self.open_store() as store:
            member = self._free_member(pool, store)
            lease_id, token = self.mint_id(), self.mint_token()
            now = _text(self.clock())
            store.add_lease(Lease(
                id=lease_id, team_lease_id=None,
                holder_hash=hash_token(token), holder_label=holder_label,
                member=member.name, pool=pool.name,
                resources=(Resource("pool", pool.name), Resource("member", member.name)),
                acquired=now, last_activity=now, idle_limit=idle_limit, state="active",
            ))
            try:
                agent = runner.start(
                    definition, member, self.config.accounts,
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
        with self._turn, self.open_store() as store:
            ending = Transition(
                id=0, lease_id=lease_id, kind=kind, time=_text(self.clock()), cause=cause
            )
            if store.record_transition(ending, expect="active") is None:
                return _recorded_end(store, lease_id)
            # The agent is named for the member. The host aborts a running
            # turn itself before it stops pi.
            agent = store.get_lease(lease_id).member
            runner.stop(agent, kind, pool_token=self.pool_token, request_id=request_id)
        return Ended(lease_id=lease_id, state=kind, cause=cause, time=ending.time)

    def _pool(self, name: str) -> Pool:
        for pool in self.config.pools:
            if pool.name == name:
                return pool
        declared = ", ".join(p.name for p in self.config.pools) or "none"
        raise UnknownPool(f"pool '{name}' is not declared; the declared pools: {declared}.")

    def _free_member(self, pool: Pool, store: PoolStore) -> Member:
        """The first member of *pool*, in the order it lists them, with no
        active lease in any pool."""
        if len(store.list_leases(state="active", pool=pool.name)) >= pool.size:
            raise NoFreeMember(
                f"pool '{pool.name}' is full: all {pool.size} of its leases are out."
            )
        members = {member.name: member for member in self.config.members}
        for name in pool.members:
            if not store.list_leases(state="active", member=name):
                return members[name]
        raise NoFreeMember(f"pool '{pool.name}' has no free member: each one serves a lease.")


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
