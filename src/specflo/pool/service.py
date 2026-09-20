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
a lease that has ended, gets the recorded end state and changes nothing. The
one process the winner does not stop is a developer's console's.

Nothing here runs in the background, so no lease expires by itself. Each
entry point begins with ``expire_due``: the leases idle for their limit, by
the rule in ``expiry``, are ended as expired before the caller is served. An
expired lease therefore stands as active until the pool is next asked for
something, and is never counted against the one who asks. A team's member
leases are judged by the latest activity on any of them, so they are all
ended at the same check or none is, each as a lease of no team is.

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

A request is served only by a member whose egress class is not more open
than the request's ceiling: the strictest of the class it names, no-train
when it names none, the class the pool's definition accepts, and the class
the requesting project pins, when there is one (see ``egress``). The pin is
handed in by whoever holds the project's record; nothing the request itself
says can widen it. The service works the ceiling out and the ledger passes
over the members above it. A pool with no member under the ceiling is refused
for good, as ``EgressRefused``: no lease ending would cure it, so it is not
the refusal a request waits on.

Nor is a pool whose members under the ceiling all run through closed
accounts. Which accounts are closed now is read from the store at each
request and judged on the service's clock (see ``accounts``), and the ledger
passes their members over. That request is refused at once as
``ClosedAccount``, with each account and its reopen time named. Closing an
account ends no lease.

Requests that did not fit wait as rows in the store, in the order they came.
A grant gives way to every request that came before its own and fits now, so
the earliest waiting request that fits is the one served, and one that does
not fit holds up no one behind it. A row keeps the egress class its request
named and the class its project pinned when it arrived, so an earlier request
fits under its own ceiling and not the asker's. The service only reads those
rows and takes the granted one out; the request that waits writes its row and
comes back to ask again (see ``waiting``).

A request that waits may take an idle lease (see ``preempt``). At a look that
finds no room the service reads which of the active leases may be taken, each
by its pool's ``preempt_after``, its own idle time and its member's state, and
a team only as a whole. It asks the ledger, as ever, whether the request fits
with some of them gone. The fewest that make it fit, the longest idle first,
are ended as preempted: each through ``end_lease``, with the id of the
request's waiting row as the request that took it. The request is then placed
again and granted in the same turn. When nothing that may be taken makes it
fit, nothing is ended. A request with no waiting row takes nothing, so there
is always an id to record. Nor does a request take what would serve a request
that arrived before it: that one takes it at its own look, so the request a
transition names is the one that got the member.

A request may name a team instead of a pool (see ``teamlease``). A team fits
when every member of every role fits at once: the members are placed one
after the other, each with those placed before it counted as if they were
out, and nothing is written until the last one fits. So a team that waits
holds nothing, and its row in the queue is judged by the same placement as
the request itself. A team that would not fit with no lease out at all is
refused at once as ``TeamNeverFits``. Each member is then granted as a single
lease is, by the same function, with the team lease id on its row; a member
that does not start ends the members granted before it, and no lease of the
team stays out.

A console is a member whose process is its developer's (see ``console``). At
every placement the service reads which consoles have an agent attached, and
through the runner whether each attached host still runs, and hands the
ledger the other consoles, which it passes over. So a pool that only they
serve has no room and its request waits: for an attach, also when the host
that was attached has died. A lease on a console is written as any other,
under the attached agent's name, and its grant binds the lease on that
agent's host and starts nothing. Its ending, whichever of the three it is,
comes through ``end_lease`` like every ending and stops nothing either: the
runner lowers the wall, aborts a turn the holder left running and records
the ending for the former holder, and the developer's pi runs on. Which
leases end so is ``console.leased`` to say. A lease that is out on a host
that died expires at its idle limit, as any lease whose pi went away.

The configuration the service holds is the one in force, and it is put there
whole (``swap``): when the daemon starts, and again when the pool directory
was changed and stood the check. The swap is one assignment inside a turn,
and a grant reads the configuration inside its own, so a request is served
under the one before the swap or the one after it and never under parts of
both. A lease that is out is not touched by a swap: its member's process was
started under the configuration of its grant and runs on as it was started.
Its row counts against the new configuration by the names it holds, a lease
from a pool that is declared no more is given back as ever, and a request
that waits for such a pool holds up no one and is told at its next look.
What may stand in a swap's way is for the caller to say, from the two
configurations and the store; the service still reads the lease rows only to
place a request and to check expiry.

The service reaches the agent subsystem through the runner only. It keeps no
clock and mints nothing by itself: the time, lease ids and lease tokens come
from callables handed in, so a test drives it with a fake clock and counted
ids.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable, Collection, Mapping, Sequence
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
from . import egress as egress_classes
from . import accounts, console, expiry, ledger, preempt, runner, teamlease
from .config import CONSOLE, Pool, PoolConfig
from .teams import Team

# The ways a lease ends; each is a transition kind and the state it leaves.
ENDINGS: tuple[str, ...] = tuple(kind for kind in TRANSITION_KINDS if kind != "granted")

# The time now, timezone-aware. Passed in, so a test hands over a fake one.
Clock = Callable[[], datetime]


class UnknownPool(SpecfloError):
    """A request for a pool the configuration does not declare."""


class UnknownTeam(SpecfloError):
    """A request for a team the configuration does not declare."""


class UnknownLease(SpecfloError):
    """A lease id the store holds no lease under."""


class IdleLimitError(SpecfloError):
    """An idle limit the pool does not allow."""


class NoFreeMember(SpecfloError):
    """A pool with no slot or no member free for one more lease. ``out`` is
    the active leases the request was judged against, for a waiting request
    that may take an idle one; none when an earlier request is served first."""

    def __init__(self, message: str, out: Sequence[Lease] = ()) -> None:
        super().__init__(message)
        self.out = tuple(out)


class EgressRefused(SpecfloError):
    """A pool with no member under the request's egress ceiling. It never will
    have one, so the request is refused at once and does not wait."""


class ClosedAccount(SpecfloError):
    """A pool whose members under the request's egress ceiling all run through
    closed accounts. No lease ending would cure it, so the request is refused
    at once and does not wait; the message names each account and when it
    reopens."""


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
    # The standing entries that are live now: what the project agents take
    # outside the pool, which the ledger counts beside the lease rows. The
    # daemon hands the reader in; without one nothing stands.
    standing: Callable[[], tuple[ledger.Standing, ...]] = tuple
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
                    read = [
                        (lease, runner.status(ledger.agent_of(lease)))
                        for lease in store.list_leases(state="active")
                    ]
                due = [lease.id for lease in expiry.due(read, now)]
                return [self.end_lease(lease_id, "expired") for lease_id in due]
            finally:
                self._checking = False

    def read(self, leases: Sequence[Lease]) -> list[expiry.Read]:
        """*leases* as the expiry rule reads them: each row with the status of
        its member's agent host. For whoever must know which member keeps a
        team before it ends the team's leases (see ``teamlease``)."""
        self.expire_due()
        return [(lease, runner.status(ledger.agent_of(lease))) for lease in leases]

    def swap(
        self,
        config: PoolConfig,
        in_the_way: Callable[[PoolConfig, PoolConfig, PoolStore], Sequence[SpecfloError]],
    ) -> tuple[SpecfloError, ...]:
        """Put *config* in force in place of the configuration held now, as
        one assignment in a turn of its own: every request after it is served
        under *config*, and no lease that is out is touched.

        *in_the_way* is handed the configuration in force, *config* and the
        store, inside that turn, and answers with what forbids the swap now.
        With anything in the way nothing is swapped, and that is returned;
        nothing is returned for a swap that was made.
        """
        self.expire_due()
        with self._turn, self.open_store() as store:
            found = tuple(in_the_way(self.config, config, store))
            if not found:
                self.config = config
        return found

    def grant(
        self,
        pool_name: str,
        *,
        holder_label: str,
        cwd: Path | str,
        idle_limit: int | None = None,
        waiting_id: str | None = None,
        egress: str | None = None,
        pinned: str | None = None,
        project: str | None = None,
        team_lease_id: str | None = None,
    ) -> Grant:
        """Lease a free member of the pool *pool_name* to *holder_label*.

        The member's pi runs in *cwd*. *idle_limit* is in seconds; without
        one the pool's default applies. *waiting_id* is the id of the
        request's own waiting row, when it has one: the requests that wait
        ahead of that row go first, all of them when there is none, and a
        grant takes the row out. *egress* is the most open class of member
        the request takes, no-train without one; the definition's own class
        stands over it, and so does *pinned*, the class the requesting
        project pins on its record, when there is one. *project* is that
        project, for a refusal to name. *team_lease_id* makes the lease one
        member of that team: the team's request gave way to those before it
        and its waiting row is the team grant's to take out, so neither is
        done again for a member. A request that has a waiting row and finds
        no room takes the idle leases that stand in its way, when there are
        such, and is granted in the same call.

        Raises ``UnknownPool``, ``IdleLimitError`` for a limit above the
        pool's maximum, ``egress.UnknownClass``, ``EgressRefused`` for a pool
        with no member under the ceiling, ``ClosedAccount`` for one whose
        members under it all run through closed accounts, ``NoFreeMember``
        also when an earlier request is served first, and what the runner
        raises for a member that does not start; the lease written for it is
        then ended.
        """
        self.expire_due()
        with self._turn, self.open_store() as store:
            # Read inside the turn, as all that follows is: a swap of the
            # configuration comes before this grant or after it.
            pool = self._pool(pool_name)
            idle_limit = _idle_limit(pool, idle_limit)
            definition = next(d for d in self.config.definitions if d.name == pool.definition)
            # Placed before the queue is looked at: a request refused for
            # good is told so whoever waits ahead of it, and never joins them.
            try:
                placed = self._place(pool.name, store, egress, pinned, project)
            except NoFreeMember as full:
                self._take_idle(full, store, waiting_id, lambda gone: self._place(
                    pool.name, store, egress, pinned, project, leave_out=gone
                ))
                placed = self._place(pool.name, store, egress, pinned, project)
            if team_lease_id is None:
                self._give_way(f"pool '{pool.name}'", store, waiting_id)
                if waiting_id is not None:
                    store.remove_waiting(waiting_id)
            lease_id, token = self.mint_id(), self.mint_token()
            now = _text(self.clock())
            store.add_lease(Lease(
                id=lease_id, team_lease_id=team_lease_id,
                holder_hash=hash_token(token), holder_label=holder_label,
                member=placed.member.name, pool=pool.name, resources=placed.resources,
                acquired=now, last_activity=now, idle_limit=idle_limit, state="active",
            ))
            try:
                if placed.member.kind == CONSOLE:
                    # A console's process is its developer's: the lease is
                    # bound on the attached host and nothing is started.
                    agent = runner.bind_console(
                        placed.agent, pool_token=self.pool_token, lease_token=token
                    )
                else:
                    # The runner names the agent for the member it is handed, so a
                    # further lease on a member is handed over under its own name.
                    agent = runner.start(
                        definition, replace(placed.member, name=placed.agent),
                        self.config.accounts, cwd=cwd, pool_token=self.pool_token,
                        lease_token=token, config_root=self.config_root, environ=self.environ,
                    )
            except Exception as exc:
                self.end_lease(lease_id, "released", cause=f"member did not start: {exc}")
                raise
            store.record_transition(
                Transition(id=0, lease_id=lease_id, kind="granted", time=now, cause="requested"),
                expect="active",
            )
        return Grant(lease_id=lease_id, agent=agent, token=token)

    def grant_team(
        self,
        team_name: str,
        *,
        holder_label: str,
        cwd: Path | str,
        idle_limit: int | None = None,
        waiting_id: str | None = None,
        egress: str | None = None,
        pinned: str | None = None,
        project: str | None = None,
    ) -> teamlease.TeamGrant:
        """Lease every role member of the team *team_name* to *holder_label*,
        all or nothing, under one team lease id.

        The rest is what ``grant`` takes, and each member is granted by it:
        every member's pi runs in *cwd*, and the egress ceiling stands over
        each member's pool as it does over a single request. *idle_limit* is
        checked against every pool of the team before anything is granted.
        A team that waits takes idle leases as a single request does.

        Raises ``UnknownTeam``, ``IdleLimitError``, and for any role what
        ``grant`` raises for its pool; ``NoFreeMember`` names the team, the
        role and what is full, and nothing was written. Raises
        ``TeamNeverFits`` for a team that would not fit with no lease out. A
        member that does not start raises what the runner raised, after every
        member granted before it has ended.
        """
        self.expire_due()
        with self._turn:
            team = self._team(team_name)
            limits = teamlease.idle_limits(
                [self._pool(role.pool) for role in team.roles], idle_limit, _idle_limit
            )
            with self.open_store() as store:
                try:
                    self._place_team(team, store, egress, pinned, project)
                except NoFreeMember as full:
                    self._never_fits(team, store, egress, pinned, project)
                    self._take_idle(full, store, waiting_id, lambda gone: self._place_team(
                        team, store, egress, pinned, project, leave_out=gone
                    ))
                self._give_way(f"team '{team.name}'", store, waiting_id)
                if waiting_id is not None:
                    store.remove_waiting(waiting_id)
            # Granted in the order they were placed, and no one else's turn
            # comes in between, so each member fits as it did when it was placed.
            team_lease_id = f"team-{self.mint_id()}"
            members: list[teamlease.MemberGrant] = []
            try:
                for role in teamlease.slots(team):
                    grant = self.grant(
                        role.pool, holder_label=holder_label, cwd=cwd,
                        idle_limit=limits[role.pool], egress=egress, pinned=pinned,
                        project=project, team_lease_id=team_lease_id,
                    )
                    members.append(teamlease.MemberGrant(
                        role=role.name, pool=role.pool, lease_id=grant.lease_id,
                        agent=grant.agent, token=grant.token,
                    ))
            except Exception:
                # The member that did not start has ended in its own grant.
                try:
                    teamlease.end_members(
                        self, [member.lease_id for member in members], "released",
                        cause=teamlease.ROLLED_BACK,
                    )
                except SpecfloError:
                    # A member that does not stop has ended all the same, and
                    # what is told is why the team did not start.
                    pass
                raise
        return teamlease.TeamGrant(team_lease_id=team_lease_id, members=tuple(members))

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
        next request, and the former holder's next verb is told the kind. A
        console's process is its developer's and is not stopped: its lease
        ends on the host, and the host runs on. So does a host under the
        member's name that the pool did not start, which a grant refused for
        that reason ends its lease on: the runner stops its own hosts only.

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
            ended = store.get_lease(lease_id)
            # A console's process is never the pool's to stop; any other
            # member's host aborts a running turn itself before it stops pi.
            told = dict(
                pool_token=self.pool_token, request_id=request_id, holder=ended.holder_hash
            )
            if console.leased(ended, self.config, store.list_consoles()):
                runner.release_console(ledger.agent_of(ended), kind, **told)
            else:
                runner.stop(ledger.agent_of(ended), kind, **told)
        return Ended(lease_id=lease_id, state=kind, cause=cause, time=ending.time)

    def _pool(self, name: str) -> Pool:
        for pool in self.config.pools:
            if pool.name == name:
                return pool
        declared = ", ".join(p.name for p in self.config.pools) or "none"
        raise UnknownPool(f"pool '{name}' is not declared; the declared pools: {declared}.")

    def _team(self, name: str) -> Team:
        for team in self.config.teams:
            if team.name == name:
                return team
        declared = ", ".join(t.name for t in self.config.teams) or "none"
        raise UnknownTeam(f"team '{name}' is not declared; the declared teams: {declared}.")

    def _give_way(self, asked: str, store: PoolStore, waiting_id: str | None) -> None:
        """Refuse a request for *asked*, a pool or a team as a refusal names
        it, while a request that came before it fits.

        The requests before it are the rows ahead of its own, *waiting_id*,
        and every row when it has none. One that does not fit is passed over.
        """
        for ahead in store.list_waiting():
            if ahead.id == waiting_id:
                return
            if self._fits(ahead, store):
                earlier = (
                    f"pool '{ahead.pool}'" if ahead.team is None else f"team '{ahead.team}'"
                )
                raise NoFreeMember(
                    f"{asked}: a request that came earlier, for {earlier}, is served first."
                )

    def _take_idle(
        self,
        full: NoFreeMember,
        store: PoolStore,
        waiting_id: str | None,
        place: Callable[[Collection[str]], object],
    ) -> None:
        """End the idle leases that stand in the way of the request that
        waits under the row *waiting_id*, or raise *full*, the refusal it got.

        *place* places the request with the leases of the ids it is handed
        left out, and raises ``NoFreeMember`` when it does not fit even so.
        Which leases may be taken, and the fewest of them to take, is the
        rule in ``preempt``; a team goes whole. Nothing is ended for a
        request with no waiting row, when no leases that may be taken make
        room, and when what would be taken would serve a request that
        arrived before this one. Each lease taken ends as preempted, with
        *waiting_id* as the request that took it.
        """
        if waiting_id is None:
            raise full
        ahead: list[WaitingRequest] = []
        for row in store.list_waiting():
            if row.id == waiting_id:
                break
            ahead.append(row)
        else:
            raise full

        def fits(gone: Collection[str]) -> bool:
            try:
                place(gone)
            except NoFreeMember:
                return False
            return True

        read = [(lease, runner.status(ledger.agent_of(lease))) for lease in full.out]
        taken = preempt.fewest(preempt.units(read, self.config.pools, self.clock()), fits)
        gone = {lease.id for unit in taken for lease in unit.leases}
        if not taken or any(self._fits(earlier, store, gone) for earlier in ahead):
            raise full
        for unit in taken:
            ids = [lease.id for lease in unit.leases]
            if unit.team_lease_id is None:
                self.end_lease(ids[0], "preempted", request_id=waiting_id)
            else:
                teamlease.end_members(self, ids, "preempted", request_id=waiting_id)

    def _fits(
        self, request: WaitingRequest, store: PoolStore, leave_out: Collection[str] = ()
    ) -> bool:
        """Could the waiting *request* be granted now? One that names no
        declared pool or team could not. It is placed under the egress class
        its row keeps, the one it named or none, and the pin its row keeps,
        as it is when it asks again; a team is placed all at once. With
        *leave_out*, could it be with the leases of those ids gone?"""
        try:
            if request.team is not None:
                self._place_team(
                    self._team(request.team), store, request.egress, request.pinned,
                    leave_out=leave_out,
                )
            else:
                self._place(
                    request.pool, store, request.egress, request.pinned, leave_out=leave_out
                )
        except (NoFreeMember, EgressRefused, ClosedAccount, UnknownTeam):
            return False
        return True

    def _place_team(
        self,
        team: Team,
        store: PoolStore,
        asked: str | None = None,
        pinned: str | None = None,
        project: str | None = None,
        alone: bool = False,
        leave_out: Collection[str] = (),
    ) -> list[ledger.Placement]:
        """Where every member of *team* fits now, all at once: each is placed
        with those placed before it counted as if they were out. Nothing is
        written. The rest is what ``_place`` takes, and what it raises is
        raised for the first member that does not fit; ``NoFreeMember`` names
        the team and the role besides."""
        placed: list[ledger.Placement] = []
        beside: list[Lease] = []
        for role in teamlease.slots(team):
            try:
                placement = self._place(
                    role.pool, store, asked, pinned, project, also=beside, alone=alone,
                    leave_out=leave_out,
                )
            except NoFreeMember as full:
                raise NoFreeMember(
                    f"team '{team.name}' does not fit at once, and it is leased all or "
                    f"nothing: no room for its role '{role.name}' - {full}", full.out,
                ) from full
            placed.append(placement)
            beside.append(teamlease.placed_lease(role.pool, placement))
        return placed

    def _never_fits(
        self,
        team: Team,
        store: PoolStore,
        asked: str | None,
        pinned: str | None,
        project: str | None,
    ) -> None:
        """Refuse *team* for good when it would not fit with no lease out and
        no project agent alive: a request for it would wait for ever."""
        try:
            self._place_team(team, store, asked, pinned, project, alone=True)
        except NoFreeMember as never:
            raise teamlease.TeamNeverFits(
                f"{never} That is so with no other lease out at all, so no lease ending "
                "makes room for the team, and the request does not wait."
            ) from never

    def _place(
        self,
        pool_name: str,
        store: PoolStore,
        asked: str | None = None,
        pinned: str | None = None,
        project: str | None = None,
        also: Sequence[Lease] = (),
        alone: bool = False,
        leave_out: Collection[str] = (),
    ) -> ledger.Placement:
        """Where a request for the pool *pool_name* that names the egress
        class *asked*, or none, fits now, as the ledger answers from the
        active lease rows. *also* are leases counted with the rows: the
        members of a team placed before this one and not yet written. With
        *alone* they are all that is counted, no row and no standing entry,
        which says whether a team could ever fit. *leave_out* are the ids of
        lease rows that are not counted: the leases a waiting request may
        take, to say whether it fits with them gone. *pinned* is the class
        the requesting project *project* pins, when there is one. The accounts the store holds as
        closed, and that are not open again by the service's clock, go to the
        ledger with their reopen times, and so do the consoles that no agent
        is attached to now, or whose attached host is gone, which it passes
        over; a placement on a console is on the agent attached to it (see
        ``console``). Raises ``NoFreeMember``,
        ``EgressRefused`` for a pool with no member under the request's
        ceiling, and ``ClosedAccount`` for one whose members under it all run
        through closed accounts."""
        accepts = [
            (definition.name, definition.egress)
            for pool in self.config.pools if pool.name == pool_name
            for definition in self.config.definitions if definition.name == pool.definition
        ]
        limits = [accepted for _, accepted in accepts]
        if pinned is not None:
            limits.append(pinned)
        ceiling = egress_classes.ceiling(asked, limits)
        now = self.clock()
        closed = {
            record.name: record.reopen
            for record in store.list_accounts() if not accounts.is_open(record, now)
        }
        out = [] if alone else [
            lease for lease in store.list_leases(state="active") if lease.id not in leave_out
        ]
        consoles = store.list_consoles()
        # A host that is gone has no status, and its console serves no one.
        hosts = {row.agent: runner.status(row.agent) for row in consoles}
        try:
            return console.placement(ledger.place(
                self.config, [*out, *also],
                ledger.Request(pool=pool_name, egress=egress_classes.within(ceiling)),
                standing=() if alone else self.standing(), closed=closed,
                # An attach cures a console with no agent, so with *alone* it counts.
                unmatched=() if alone else console.unmatched(self.config, consoles, hosts),
            ), consoles)
        except ledger.NoRoom as full:
            raise NoFreeMember(str(full), out) from full
        except ledger.AccountClosed as shut:
            raise ClosedAccount(str(shut)) from shut
        except ledger.NoMemberAllowed as never:
            named = "no class, which stands for" if asked is None else "the class"
            named_limits = "".join(
                f", and definition '{name}' accepts '{accepted}'" for name, accepted in accepts
            )
            if pinned is not None:
                named_limits += f", and project '{project}' pins '{pinned}'"
            raise EgressRefused(
                f"{never} The request's egress ceiling is '{ceiling}': it names {named} "
                f"'{egress_classes.ceiling(asked, [])}'{named_limits}; the strictest of them "
                "stands, and no member more open than it is leased."
            ) from never


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
