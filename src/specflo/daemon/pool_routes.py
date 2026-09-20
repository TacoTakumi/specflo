"""The daemon's pool routes: the agent pool over HTTP, behind the token guard.

The pool is the daemon's, not a project's: its configuration is the pool
directory under the daemon root, its leases are rows in the daemon's store,
and its members run on the daemon's host. One pool service stands for the
whole process, made when the application is and kept on ``app.state.pool``;
a root with no pool directory has none, and every route here then refuses.
A pool directory with faults gives none either: the faults are kept on
``app.state.pool_errors``, and every route here answers with all of them.

The pool directory is read when the daemon starts, and again when the daemon
is asked to (``reload_pool``): by the reload route, which the developer
identity alone is served, and by whatever else in this process has changed
the directory. The directory is checked whole, as at the start. One that
stands is handed to the service, which puts it in force as one configuration
for every request from then on; a daemon that had no pool for the faults of
its directory gets its service then, in the same process. One with faults
changes nothing: the service grants on under the last configuration that
stood, and the faults are kept on ``app.state.pool_errors`` until a reload
passes, where the pool's status tells them. So the reload route does not
wait for a pool that stands, as every other route here does, and answers a
directory that does not stand as they do: a 400 with every fault.

A lease that is out ends as its member's kind says: a process the pool
started is stopped, a developer's console is left running, and which it is
the pool knows from the configuration alone. So a directory that declares
the member of an active lease no more, or as the other kind, is not put in
force, in any part: that is a fault of the reload, with the member and the
lease named, and it passes once the lease has ended. A pool or a team that
is declared no more stands in no one's way: its leases are given back as
ever, and a request that waits for it is refused at its next look.

Agent names are one namespace on this host, and a developer's host that was
attached to a console knows the pool's token. A member the pool starts under
the name of such a host would find it there at its first grant and stop it as
its own. So a directory that declares one is not put in force either, with
the member, the agent and its slot named, and it passes once the member has
another name. No reload stands before the directory the daemon starts on, so
the same is asked where the pool opens, and such a directory gives no pool.

The routes are written out, like the product routes. Each one runs as the
identity behind its bearer token, and one that changes the pool appends an
audit record with that identity and the lease it acted on. The service takes
its own turns, so a route holds no lock of the daemon's.

A refusal from the pool is a 400 carrying its message. A member that does not
start or stop is a 502: what the agent CLI said of it can name paths on this
host, so that goes to the daemon's log and the response names the pool only.
No route here carries anything a member wrote.

A request the pool has no room for may wait, for as long as its body says
and no longer than ``waiting.WAIT_MAX``; one that asks for more is refused
before the pool is looked at. It waits in its own route, which is async:
between two looks at the pool it holds no thread, and each look runs in a
worker thread, so a request that waits for an hour stalls no other. The route
also sees its client go away, and a request no one waits for any more leaves
the queue. The rows a stopped daemon left are cleared when the pool opens.

A member takes seconds to start, and the client may go away in them. The
token of a lease granted then is one no one ever read, so the lease is ended
before it is answered: as any lease ends, with a cause that says the
requester went away, and its slots are free to the next request. The ending
stops a process, which takes time too, so it runs in a thread apart from the
route: it is not awaited, and a route that is cancelled still ends its lease.

A client may say that it reads notices. The answer to its request, when the
request has to wait, is then sent as it comes, a JSON object to a line: first
that it waits, on what, in which place and for how long, and at the end of
the wait the result. The status of such an answer is sent with its first
line, so a refusal that ends the wait is its last line, with the status and
the detail it has anywhere else. A request that is granted or refused at its
first look is answered as it always was, whoever asks.

A request may name the project it is made from, by its slug and no more. The
egress class that project pins is read here, from this daemon's own record of
the project, and stands over the request as one more limit. A request has no
field to say a pin with, so no client widens one; a project this daemon does
not hold is refused, not served as a request with no pin.

A request names a pool or a team, one of the two. A team's grant is answered
with the team lease id and, for each role member, its lease, its agent and
its token. The team lease id is given back on the route a lease is, and ends
every member lease; a token of any member lease proves the team's holder. One
member lease of a team is not released by itself: that is refused with the
team lease id named. No route here takes anything from one member to another.

A developer's console is attached to its slot, and detached, by the
developer identity and no other: a requester or an agent that asks is
answered 403. The agent attached is a host that runs on this host, named in
the body, and both acts are audited under the slot.

A lease is its holder's, and the holder is whoever presents the lease token:
the bearer token says which identity asks, not which orchestrator. So a
release ends an active lease for that token only, and the listing answers for
the tokens in its body and no others. What stands for a token in the store is
its hash, and neither goes back in a response or into the audit record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..config import load_config
from ..errors import SpecfloError
from ..pool import console, egress, ledger, standing, teamlease, waiting
from ..pool.cli_admin import pool_dir
from ..pool.config import ConfigError, PoolConfig, load_pool_config
from ..pool.runner import ConsoleBusy, RunnerError
from ..pool.service import Grant, PoolService, hash_token
from ..projects import load_project, validate_slug
from ..service.pool_remote import (
    CONSOLE_ATTACH_PATH,
    CONSOLE_DETACH_PATH,
    HELD_PATH,
    LEASES_PATH,
    RELOAD_PATH,
    STATUS_PATH,
    WAITING_MEDIA_TYPE,
)
from . import seat
from .poolstore import Lease, PoolStore, open_pool_store
from .routes import audit, current_identity

# The daemon's own credential on its members' hosts, kept under the root so
# that a restarted daemon can still end the leases it finds in its store.
POOL_TOKEN_FILENAME = "pool-token"
# Where the pi configuration directories of hosted members are generated.
PI_CONFIG_DIRNAME = "pool-piconfig"

# The longest holder label a request may carry.
LABEL_MAX = 80

# What every route here answers on a daemon whose root declares no pool.
NO_POOL = "No pool is configured on this daemon."

_log = logging.getLogger(__name__)


# --- the pool of one daemon ---------------------------------------------------


def pool_token(root: Path) -> str:
    """The pool token of the daemon root *root*, minted on first use.

    Unlike a bearer token it is kept as it is, not as a hash: the daemon
    presents it, it does not check it. The file is this user's alone.
    """
    path = Path(root) / POOL_TOKEN_FILENAME
    if not path.is_file():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(secrets.token_urlsafe(32) + "\n")
    return path.read_text(encoding="utf-8").strip()


def open_pool(root: Path) -> tuple[PoolService | None, tuple[ConfigError, ...]]:
    """The pool service of the daemon root *root*, and the faults that stand in its way.

    A root that declares no pool has neither. A pool directory is checked
    whole, as ``pool validate`` checks it; one with faults gives no service
    and every fault, and the daemon serves its projects all the same.
    """
    directory = pool_dir(root)
    if not directory.is_dir():
        return None, ()
    config, errors = load_pool_config(directory)
    if not errors:
        with open_pool_store(root) as store:
            errors = attached_names(config, store)
    if errors:
        for error in errors:
            _log.warning("pool configuration: %s", error)
        return None, tuple(errors)
    service = PoolService(
        config=config,
        open_store=lambda: open_pool_store(root),
        pool_token=pool_token(root),
        config_root=Path(root) / PI_CONFIG_DIRNAME,
        # The project agents of this root that serve now, asked of agent
        # discovery at each request: they take a model or an account slot.
        standing=lambda: standing.entries(
            root, seat.agent_mapping(root), lambda slug: seat.liveness(root, slug).alive
        ),
    )
    # A request waits on a connection to the daemon that wrote its row: the
    # rows found now were left by one that stopped.
    waiting.forget_all(service)
    return service, ()


def pool_service(request: Request) -> PoolService:
    """The pool a request is served by. Every pool route depends on this, so
    it is where a daemon with no pool refuses: with every fault of a pool
    configuration that did not stand, or because no pool is configured."""
    service = getattr(request.app.state, "pool", None)
    if service is not None:
        return service
    errors = getattr(request.app.state, "pool_errors", ())
    if errors:
        # One string, a fault to a line: a client prints a 400's detail as it is.
        raise HTTPException(
            status_code=400,
            detail="\n".join([
                "The pool is off: the pool configuration on this daemon is not valid. "
                "Correct what follows and reload it with `specflo serve pool reload`; the "
                "daemon need not be started again.",
                *(str(error) for error in errors),
            ]),
        )
    raise HTTPException(status_code=400, detail=NO_POOL)


def held_members(old: PoolConfig, new: PoolConfig, store: PoolStore) -> list[ConfigError]:
    """What forbids *new* in place of *old* now: each active lease whose member
    *new* declares no more, or as another kind than *old* does.

    Such a lease could not end as it began. The ending of a lease stops a
    process the pool started and leaves a console running, and tells the two
    apart by the configuration in force when the lease ends.
    """
    before = {member.name: member.kind for member in old.members}
    after = {member.name: member.kind for member in new.members}
    faults = []
    for lease in store.list_leases(state="active"):
        kind = before.get(lease.member)
        if kind is None or after.get(lease.member) == kind:
            # A member *old* does not declare either is no worse off under *new*.
            continue
        now = after.get(lease.member)
        now = "declared no more" if now is None else f"of kind '{now}'"
        faults.append(ConfigError(
            new.path, f"member '{lease.member}'", "kind",
            f"{now}, and lease '{lease.id}' is out on it as a member of kind '{kind}'; a "
            "lease ends as its member's kind says, so give the lease back or let it end, "
            "then reload.",
        ))
    return faults


def attached_names(new: PoolConfig, store: PoolStore) -> list[ConfigError]:
    """What else forbids *new*: each member it starts under the name of an
    agent that was attached to a console, whatever the slot's state now.

    The host of that agent is its developer's, and it knows the pool's token
    from the attach. A grant on the member would find it under the member's
    name and stop it as the member's.
    """
    return [
        ConfigError(
            new.path, f"member '{member.name}'", "name",
            f"the pool would start its host as '{row.agent}', the name of the agent that was "
            f"attached to console '{row.slot}': that host is its developer's and knows the "
            "pool's token, and a grant on the member would stop it as the member's. Declare "
            "the member under another name, then reload.",
        )
        for member, row in console.started_under(new, store.list_consoles())
    ]


def _in_the_way(old: PoolConfig, new: PoolConfig, store: PoolStore) -> list[ConfigError]:
    """All that forbids *new* in place of *old* now, for the service's swap."""
    return held_members(old, new, store) + attached_names(new, store)


def reload_pool(app, identity: str) -> tuple[ConfigError, ...]:
    """Read the pool directory of the daemon application *app* again, as
    *identity*, and put it in force; the faults that kept it from that, none
    for a reload that passed.

    The faults are kept on ``app.state.pool_errors`` either way. With any,
    ``app.state.pool`` is what it was: no pool for a daemon that had none,
    and the service under its last valid configuration otherwise. A reload
    that passed is audited under *identity*. A root with no pool directory
    and no pool has nothing to read, and nothing changes.
    """
    root = app.state.root
    service = getattr(app.state, "pool", None)
    if service is None:
        if not pool_dir(root).is_dir():
            return ()
        # No configuration ever stood here, so the pool opens as at the start.
        app.state.pool, faults = open_pool(root)
    else:
        config, errors = load_pool_config(pool_dir(root))
        for error in errors:
            _log.warning("pool configuration: %s", error)
        faults = tuple(errors) or service.swap(config, _in_the_way)
    app.state.pool_errors = faults
    if not faults:
        audit(root, identity, "pool_reload", None, None)
    return faults


router = APIRouter(dependencies=[Depends(current_identity)])


# --- what a body may hold -----------------------------------------------------


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _body(body, *, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    """The request body's fields; 422 for an unknown field or a missing one."""
    if not isinstance(body, dict):
        raise _invalid("The request body must be a JSON object.")
    unknown = sorted(set(body) - set(required) - set(optional))
    if unknown:
        raise _invalid(f"Unknown field(s) {', '.join(unknown)}.")
    missing = [name for name in required if name not in body]
    if missing:
        raise _invalid(f"Missing field(s) {', '.join(missing)}.")
    return dict(body)


def _text(fields: dict, name: str) -> str | None:
    value = fields.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"Field {name!r} must be a string that is not empty.")
    return value


def _seconds(fields: dict, name: str) -> int | None:
    value = fields.get(name)
    if value is None:
        return None
    # A boolean is an integer to Python and a mistake here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"Field {name!r} must be a whole number of seconds.")
    return value


def _holder_label(identity: str, label: str | None) -> str:
    """Who holds a lease, for a person to read: the identity that asked, and
    what the client calls itself when it says."""
    if label is None:
        return identity
    label = label.strip()
    if len(label) > LABEL_MAX or not label.isprintable():
        raise _invalid(
            f"Field 'label' must be printable text of at most {LABEL_MAX} characters."
        )
    return f"{label} ({identity})"


def _pinned(root: Path, slug: str | None) -> str | None:
    """The egress class the project *slug* pins, as this daemon's own record of
    the project says now; None for a request that names no project, and for a
    project that pins none. A project this daemon does not hold, or whose
    record cannot be read, is refused: its request is never served unpinned."""
    if slug is None:
        return None
    try:
        project = load_project(root, load_config(root), validate_slug(slug))
    except SpecfloError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The project the request is made from cannot be held to its egress pin. {exc}",
        )
    return project.egress or None


def _tokens(fields: dict, name: str) -> list[str]:
    value = fields[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(f"Field {name!r} must be a list of strings.")
    return value


def _refused(exc: SpecfloError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _member_failed(asked: str, exc: RunnerError) -> HTTPException:
    """What a request on *asked*, a pool or a team as a person reads it, is
    answered with when a member did not start or stop."""
    _log.warning("a request for %s: %s", asked, exc)
    return HTTPException(
        status_code=502,
        detail=f"A member could not be started or stopped for the request on {asked}; "
        "the daemon's log has the cause.",
    )


# --- leases -------------------------------------------------------------------


# Why a lease ends whose requester did not stay for its answer. A page shows a
# cause up to its first ": ", so this has none.
REQUESTER_GONE = "the requester went away before it was answered"


class _ClientGone(Exception):
    """The client of a request closed its connection before it was answered."""


def _look(asked: waiting.Waiting, root: Path, identity: str) -> Grant | teamlease.TeamGrant | None:
    """One look at *asked* for *identity*, in a worker thread. A grant is
    audited there and then, so a lease this route made has its record whatever
    becomes of the route. A team's grant is one act, audited under the team
    lease id."""
    grant = asked.attempt()
    if grant is not None:
        team = isinstance(grant, teamlease.TeamGrant)
        granted = grant.team_lease_id if team else grant.lease_id
        audit(root, identity, "lease_request", None, granted)
    return grant


async def _granted(
    request: Request, identity: str, asked: waiting.Waiting
) -> Grant | teamlease.TeamGrant:
    """The grant of *asked*, looked for until there is one, the request's time
    is up or its client is gone. However the wait ends, the request leaves the
    queue."""
    root = request.app.state.root
    try:
        while True:
            grant = await run_in_threadpool(_look, asked, root, identity)
            if grant is not None:
                return grant
            if await request.is_disconnected():
                raise _ClientGone()
            await asyncio.sleep(waiting.POLL_INTERVAL)
    finally:
        # Not awaited: a route that is cancelled still takes its row out.
        asked.leave()


def _first_look(
    asked: waiting.Waiting, root: Path, identity: str
) -> tuple[Grant | teamlease.TeamGrant | None, int | None]:
    """The grant of *asked* if it fits now, or its place among those that wait."""
    grant = _look(asked, root, identity)
    return grant, None if grant is not None else asked.place()


def _end_unanswered(service: PoolService, grant: Grant | teamlease.TeamGrant) -> None:
    """End every lease of *grant*, through the one function that ends a lease.
    No one is there to answer, so a member that does not stop is the log's
    matter: its lease has ended all the same."""
    try:
        if isinstance(grant, teamlease.TeamGrant):
            teamlease.end_members(
                service, [member.lease_id for member in grant.members], "released",
                cause=REQUESTER_GONE,
            )
        else:
            service.end_lease(grant.lease_id, "released", cause=REQUESTER_GONE)
    except RunnerError as exc:
        _log.warning("a lease whose requester went away: %s", exc)
    except Exception:
        _log.exception("a lease whose requester went away was not ended")


def _give_back(service: PoolService, grant: Grant | teamlease.TeamGrant) -> None:
    """Give back *grant*, whose requester went away before it was answered.

    The ending stops a process, which may take as long as the runner allows,
    so it runs in a thread and not on the loop. Not awaited: a route that is
    cancelled cannot wait, and its lease is ended all the same.
    """
    asyncio.get_running_loop().run_in_executor(None, _end_unanswered, service, grant)


def _failed(asked: waiting.Waiting, exc: Exception) -> HTTPException:
    """What the request *asked* that ended in *exc* is answered with."""
    if isinstance(exc, _ClientGone):
        # No one reads this: the connection it would go down is closed.
        return HTTPException(status_code=400, detail="The client went away before it was answered.")
    if isinstance(exc, ConsoleBusy):
        # The console was free when it was matched and is not free now: no
        # member failed, and the request is told which console and why.
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RunnerError):
        named = f"pool '{asked.pool}'" if asked.team is None else f"team '{asked.team}'"
        return _member_failed(named, exc)
    if isinstance(exc, SpecfloError):
        return _refused(exc)
    _log.error("lease_request failed on the daemon", exc_info=exc)
    return HTTPException(
        status_code=500,
        detail=f"lease_request failed on the daemon: {type(exc).__name__}.",
    )


def _answer(grant: Grant | teamlease.TeamGrant) -> dict:
    """What a granted request is answered with."""
    # The one time a token is sent: the store and the audit record hold no copy.
    if isinstance(grant, teamlease.TeamGrant):
        return {"result": {
            "team_lease_id": grant.team_lease_id,
            "members": [
                {
                    "role": member.role, "pool": member.pool, "lease_id": member.lease_id,
                    "agent": member.agent, "token": member.token,
                }
                for member in grant.members
            ],
        }}
    return {"result": {"lease_id": grant.lease_id, "agent": grant.agent, "token": grant.token}}


def _line(told: dict) -> bytes:
    return (json.dumps(told) + "\n").encode("utf-8")


class _Waited(StreamingResponse):
    """The answer to a request that waits, for a client that reads notices:
    that it waits, and when the wait is over the result or the refusal. The
    client going away stops the sending. However the sending ends the request
    leaves the queue, and a grant whose line was not sent is given back."""

    def __init__(
        self, request: Request, identity: str, asked: waiting.Waiting, notice: dict
    ) -> None:
        super().__init__(self._lines(request, identity, notice), media_type=WAITING_MEDIA_TYPE)
        self._asked = asked
        # The grant, from when the pool gives it until its line has been sent.
        self._unsent: Grant | teamlease.TeamGrant | None = None

    async def _lines(self, request: Request, identity: str, notice: dict) -> AsyncIterator[bytes]:
        yield _line({"waiting": notice})
        try:
            self._unsent = await _granted(request, identity, self._asked)
        except Exception as exc:
            refused = _failed(self._asked, exc)
            yield _line({"refused": {"status": refused.status_code, "detail": refused.detail}})
            return
        # The member took its time to start, and the client may be gone since.
        if await request.is_disconnected():
            return
        yield _line(_answer(self._unsent))
        # Asked for the line after it, so that one was sent.
        self._unsent = None

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Not left to the lines: a client gone before the first is never
            # asked for one, and lines that are cancelled do not go on.
            self._asked.leave()
            if self._unsent is not None:
                _give_back(self._asked.service, self._unsent)


@router.post(LEASES_PATH, response_model=None)
async def lease_request(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
    service: PoolService = Depends(pool_service),
) -> dict | StreamingResponse:
    """Grant a lease on a free member of the named pool, or on every role
    member of the named team, started in the named directory; a request with
    a time to wait waits that long, and a client that reads notices is told
    at once that it waits."""
    fields = _body(
        body, required=("cwd",),
        optional=("pool", "team", "idle_limit", "label", "wait", "egress", "project"),
    )
    pool = _text(fields, "pool")
    team = _text(fields, "team")
    cwd = _text(fields, "cwd")
    idle_limit = _seconds(fields, "idle_limit")
    wait = _seconds(fields, "wait")
    if wait is not None and wait < 0:
        raise _invalid("Field 'wait' must be a whole number of seconds, 0 or more.")
    if wait is not None and wait > waiting.WAIT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"A request waits {waiting.WAIT_MAX} s at the most, and this one asked to "
            f"wait {wait} s.",
        )
    holder_label = _holder_label(identity, _text(fields, "label"))
    asked_class = _text(fields, "egress")
    if asked_class is not None and asked_class not in egress.EGRESS_CLASSES:
        raise _invalid(
            f"Field 'egress' must be an egress class, and '{asked_class}' is not one; "
            "the classes, strictest first: " + ", ".join(egress.EGRESS_CLASSES) + "."
        )
    if (pool is None) == (team is None):
        raise _invalid("The request names a pool or a team: one of the fields 'pool' and 'team'.")
    if cwd is None:
        raise _invalid("Field 'cwd' must be a string that is not empty.")
    # The member runs on this host, so the directory is one of this host's.
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise HTTPException(
            status_code=400,
            detail=f"The working directory '{cwd}' is not a directory on the daemon's host; "
            "a member starts in an absolute path that is there.",
        )
    # Read once, as the request arrives: the pin it waits under is the one it came under.
    project = _text(fields, "project")
    pinned = await run_in_threadpool(_pinned, request.app.state.root, project)
    asked = waiting.Waiting(
        service, pool, holder_label=holder_label, cwd=cwd, idle_limit=idle_limit, wait=wait or 0,
        egress=asked_class, pinned=pinned, project=project, team=team,
    )
    reads_notices = WAITING_MEDIA_TYPE in request.headers.get("accept", "")
    waits_on = False
    try:
        grant, place = await run_in_threadpool(
            _first_look, asked, request.app.state.root, identity
        )
        if grant is None and reads_notices:
            # Nothing is sent yet, so what is refused above has the status it always had.
            waits_on = True
            named = {"pool": pool} if team is None else {"team": team}
            notice = {**named, "full": asked.full, "place": place, "wait": wait}
            return _Waited(request, identity, asked, notice)
        if grant is None:
            grant = await _granted(request, identity, asked)
    except Exception as exc:
        raise _failed(asked, exc)
    finally:
        # A request that waits on in its lines leaves when they end.
        if not waits_on:
            asked.leave()
    # The member took its time to start, and the client may be gone since.
    if await request.is_disconnected():
        _give_back(service, grant)
        raise _failed(asked, _ClientGone())
    return _answer(grant)


def _expire_due(service: PoolService) -> None:
    """End the leases that are due before the pool's state is read. A member
    that does not stop is the log's matter: its lease has ended all the same."""
    try:
        service.expire_due()
    except RunnerError as exc:
        _log.warning("an expired lease: %s", exc)


def _holds(lease: Lease, token: str | None) -> bool:
    """Is *token* the one issued when *lease* was granted?"""
    return token is not None and secrets.compare_digest(hash_token(token), lease.holder_hash)


def _held(lease: Lease) -> dict:
    """A lease as its holder is told of it: nothing of the token, nor its hash."""
    return {
        "lease_id": lease.id, "agent": ledger.agent_of(lease), "pool": lease.pool,
        "state": lease.state, "acquired": lease.acquired,
        "last_activity": lease.last_activity, "idle_limit": lease.idle_limit,
        # What its holder gives the lease back under, when it is one of a team.
        "team_lease_id": lease.team_lease_id,
    }


@router.post(HELD_PATH)
def leases_held(
    body: dict = Body(default_factory=dict),
    service: PoolService = Depends(pool_service),
) -> dict:
    """The lease each presented token holds, in the order of the tokens; null
    for a token that holds none. A lease no presented token holds is not told."""
    tokens = _tokens(_body(body, required=("tokens",)), "tokens")
    _expire_due(service)
    with service.open_store() as store:
        leases = store.list_leases()
    # A token is minted for one lease, so a hash names at most one.
    by_holder = {lease.holder_hash: lease for lease in leases}
    held = [by_holder.get(hash_token(token)) for token in tokens]
    return {"result": [None if lease is None else _held(lease) for lease in held]}


@router.post(LEASES_PATH + "/{lease_id}/release")
def lease_release(
    lease_id: str,
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
    service: PoolService = Depends(pool_service),
) -> dict:
    """End the lease for the holder its token proves; an ended lease is
    reported as it ended to whoever asks, and nothing changes. A team lease
    id ends every member lease of the team, and one member lease of a team is
    not ended by itself."""
    token = _text(_body(body, required=(), optional=("token",)), "token")
    _expire_due(service)
    with service.open_store() as store:
        lease = store.get_lease(lease_id)
    if lease is None:
        members = teamlease.team_leases(service, lease_id)
        if members:
            return _team_release(request, identity, service, lease_id, members, token)
        raise HTTPException(status_code=400, detail=f"There is no lease '{lease_id}'.")
    held = _holds(lease, token)
    if lease.state != "active":
        return {"result": {"lease_id": lease.id, "state": lease.state, "held": held}}
    if not held:
        raise HTTPException(
            status_code=400,
            detail=f"Lease '{lease_id}' is held by another; only its holder releases it.",
        )
    try:
        teamlease.refuse_member_release(lease)
        ended = service.end_lease(lease_id, "released")
    except RunnerError as exc:
        # The lease has ended all the same, and this identity ended it.
        audit(request.app.state.root, identity, "lease_release", None, lease_id)
        raise _member_failed(f"pool '{lease.pool}'", exc)
    except SpecfloError as exc:
        raise _refused(exc)
    # A lease that ended some other way in between was not released by this call.
    if ended.state == "released":
        audit(request.app.state.root, identity, "lease_release", None, lease_id)
    return {"result": {"lease_id": lease_id, "state": ended.state, "held": True}}


def _team_release(
    request: Request,
    identity: str,
    service: PoolService,
    team_lease_id: str,
    members: list[Lease],
    token: str | None,
) -> dict:
    """Release the team whose leases are *members* for the holder *token*
    proves: whoever holds one member lease holds them all. A team with no
    active member is reported as it ended, and nothing changes."""
    held = any(_holds(member, token) for member in members)
    out = [member for member in members if member.state == "active"]
    if not out:
        return {"result": {"lease_id": team_lease_id, "state": members[-1].state, "held": held}}
    if not held:
        raise HTTPException(
            status_code=400,
            detail=f"Team lease '{team_lease_id}' is held by another; only its holder "
            "releases it.",
        )
    try:
        ended = teamlease.release_team(service, team_lease_id)
    except RunnerError as exc:
        # Every member lease has ended all the same, and this identity ended them.
        audit(request.app.state.root, identity, "lease_release", None, team_lease_id)
        raise _member_failed(f"team lease '{team_lease_id}'", exc)
    except SpecfloError as exc:
        raise _refused(exc)
    # Members that ended some other way in between were not released by this call.
    released = [end for end in ended if end.state == "released"]
    if released:
        audit(request.app.state.root, identity, "lease_release", None, team_lease_id)
    state = "released" if released else ended[-1].state
    return {"result": {"lease_id": team_lease_id, "state": state, "held": True}}


# --- consoles -----------------------------------------------------------------

# The one identity that attaches and detaches a console.
CONSOLE_IDENTITY = "developer"


def _developer(identity: str, act: str = "A console is attached and detached") -> None:
    """Refuse a route to any identity but the developer's; *act* is what the
    route does, as the refusal says it."""
    if identity != CONSOLE_IDENTITY:
        raise HTTPException(
            status_code=403,
            detail=f"{act} by the {CONSOLE_IDENTITY} identity "
            f"only, and this token is the {identity} identity's.",
        )


def _required(fields: dict, name: str) -> str:
    value = _text(fields, name)
    if value is None:
        raise _invalid(f"Field {name!r} must be a string that is not empty.")
    return value


@router.post(CONSOLE_ATTACH_PATH)
def console_attach(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
    service: PoolService = Depends(pool_service),
) -> dict:
    """Attach the named agent host, which runs on this host, to the named
    console slot; from then on the slot is matched like any member."""
    _developer(identity)
    fields = _body(body, required=("slot", "agent"))
    slot, agent = _required(fields, "slot"), _required(fields, "agent")
    try:
        attached = console.attach(service, slot, agent)
    except SpecfloError as exc:
        raise _refused(exc)
    audit(request.app.state.root, identity, "console_attach", None, slot)
    return {"result": {
        "slot": attached.slot, "agent": attached.agent, "state": console.ATTACHED,
    }}


@router.post(CONSOLE_DETACH_PATH)
def console_detach(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
    service: PoolService = Depends(pool_service),
) -> dict:
    """Detach the named console slot: it takes no new lease, and the lease
    that is out on it stands."""
    _developer(identity)
    slot = _required(_body(body, required=("slot",)), "slot")
    try:
        state = console.detach(service, slot)
    except SpecfloError as exc:
        raise _refused(exc)
    audit(request.app.state.root, identity, "console_detach", None, slot)
    return {"result": {"slot": slot, "state": state}}


# --- the pool's configuration ---------------------------------------------------


@router.post(RELOAD_PATH)
def pool_reload(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    """Read the pool directory again and put it in force: the daemon's process
    id, the directory, and how much of each kind stands now. It does not
    depend on a pool that stands, since it is what gives a daemon one."""
    _developer(identity, "The pool configuration is reloaded")
    _body(body, required=())
    app = request.app
    faults = reload_pool(app, identity)
    if faults:
        kept = (
            "The pool stays off" if app.state.pool is None
            else "The configuration that stood before stays in force"
        )
        raise HTTPException(
            status_code=400,
            detail="\n".join([
                "The pool configuration was not reloaded: the pool directory on this daemon "
                f"cannot be put in force. {kept}. Correct what follows and reload again.",
                *(str(fault) for fault in faults),
            ]),
        )
    if app.state.pool is None:
        raise HTTPException(status_code=400, detail=NO_POOL)
    config = app.state.pool.config
    return {"result": {
        "pid": os.getpid(), "directory": str(pool_dir(app.state.root)),
        "definitions": len(config.definitions), "accounts": len(config.accounts),
        "members": len(config.members), "pools": len(config.pools),
        "teams": len(config.teams),
    }}


# --- the pool's state ---------------------------------------------------------


@router.get(STATUS_PATH)
def pool_status(request: Request, service: PoolService = Depends(pool_service)) -> dict:
    """Each pool's size and how many of its leases are out; no lease is named
    and no holder. Beside them, the faults of the pool directory as it was
    last read, when the configuration in force is an earlier one."""
    _expire_due(service)
    errors = [str(error) for error in getattr(request.app.state, "pool_errors", ())]
    with service.open_store() as store:
        return {"errors": errors, "result": [
            {
                "name": pool.name, "size": pool.size,
                "in_use": len(store.list_leases(state="active", pool=pool.name)),
            }
            for pool in service.config.pools
        ]}
