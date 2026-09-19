"""The daemon's pool routes: the agent pool over HTTP, behind the token guard.

The pool is the daemon's, not a project's: its configuration is the pool
directory under the daemon root, its leases are rows in the daemon's store,
and its members run on the daemon's host. One pool service stands for the
whole process, made when the application is and kept on ``app.state.pool``;
a root with no pool directory has none, and every route here then refuses.
A pool directory with faults gives none either: the faults are kept on
``app.state.pool_errors``, and every route here answers with all of them.

The routes are written out, like the product routes. Each one runs as the
identity behind its bearer token, and one that changes the pool appends an
audit record with that identity and the lease it acted on. The service takes
its own turns, so a route holds no lock of the daemon's.

A refusal from the pool is a 400 carrying its message. A member that does not
start or stop is a 502: what the agent CLI said of it can name paths on this
host, so that goes to the daemon's log and the response names the pool only.
No route here carries anything a member wrote.

A request the pool has no room for may wait, for as long as its body says.
It waits in its own route, which is async: between two looks at the pool it
holds no thread, and each look runs in a worker thread, so a request that
waits for an hour stalls no other. The route also sees its client go away,
and a request no one waits for any more leaves the queue. The rows a stopped
daemon left are cleared when the pool opens.

A lease is its holder's, and the holder is whoever presents the lease token:
the bearer token says which identity asks, not which orchestrator. So a
release ends an active lease for that token only, and the listing answers for
the tokens in its body and no others. What stands for a token in the store is
its hash, and neither goes back in a response or into the audit record.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from ..config import load_config
from ..errors import SpecfloError
from ..pool import egress, ledger, standing, waiting
from ..pool.cli_admin import pool_dir
from ..pool.config import ConfigError, load_pool_config
from ..pool.runner import RunnerError
from ..pool.service import Grant, PoolService, hash_token
from ..projects import load_project, validate_slug
from ..service.pool_remote import HELD_PATH, LEASES_PATH, STATUS_PATH
from . import seat
from .poolstore import Lease, open_pool_store
from .routes import audit, current_identity

# The daemon's own credential on its members' hosts, kept under the root so
# that a restarted daemon can still end the leases it finds in its store.
POOL_TOKEN_FILENAME = "pool-token"
# Where the pi configuration directories of hosted members are generated.
PI_CONFIG_DIRNAME = "pool-piconfig"

# The longest holder label a request may carry.
LABEL_MAX = 80

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
                "Correct what follows and start the daemon again.",
                *(str(error) for error in errors),
            ]),
        )
    raise HTTPException(status_code=400, detail="No pool is configured on this daemon.")


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


def _tokens(fields: dict, name: str) -> list[str]:
    value = fields[name]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _invalid(f"Field {name!r} must be a list of strings.")
    return value


def _refused(exc: SpecfloError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _member_failed(pool: str, exc: RunnerError) -> HTTPException:
    _log.warning("a request for pool %s: %s", pool, exc)
    return HTTPException(
        status_code=502,
        detail=f"A member could not be started or stopped for the request on pool '{pool}'; "
        "the daemon's log has the cause.",
    )


# --- leases -------------------------------------------------------------------


class _ClientGone(Exception):
    """The client of a waiting request closed its connection."""


async def _granted(request: Request, asked: waiting.Waiting) -> Grant:
    """The grant of *asked*, looked for until there is one, the request's time
    is up or its client is gone. However the wait ends, the request leaves the
    queue."""
    try:
        while True:
            grant = await run_in_threadpool(asked.attempt)
            if grant is not None:
                return grant
            if await request.is_disconnected():
                raise _ClientGone()
            await asyncio.sleep(waiting.POLL_INTERVAL)
    finally:
        # Not awaited: a route that is cancelled still takes its row out.
        asked.leave()


@router.post(LEASES_PATH)
async def lease_request(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
    service: PoolService = Depends(pool_service),
) -> dict:
    """Grant a lease on a free member of the named pool, started in the named
    directory; a request with a time to wait waits that long for one."""
    fields = _body(
        body, required=("pool", "cwd"), optional=("idle_limit", "label", "wait", "egress")
    )
    pool = _text(fields, "pool")
    cwd = _text(fields, "cwd")
    idle_limit = _seconds(fields, "idle_limit")
    wait = _seconds(fields, "wait")
    if wait is not None and wait < 0:
        raise _invalid("Field 'wait' must be a whole number of seconds, 0 or more.")
    holder_label = _holder_label(identity, _text(fields, "label"))
    asked_class = _text(fields, "egress")
    if asked_class is not None and asked_class not in egress.EGRESS_CLASSES:
        raise _invalid(
            f"Field 'egress' must be an egress class, and '{asked_class}' is not one; "
            "the classes, strictest first: " + ", ".join(egress.EGRESS_CLASSES) + "."
        )
    if pool is None or cwd is None:
        raise _invalid("Fields 'pool' and 'cwd' must be strings that are not empty.")
    # The member runs on this host, so the directory is one of this host's.
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise HTTPException(
            status_code=400,
            detail=f"The working directory '{cwd}' is not a directory on the daemon's host; "
            "a member starts in an absolute path that is there.",
        )
    asked = waiting.Waiting(
        service, pool, holder_label=holder_label, cwd=cwd, idle_limit=idle_limit, wait=wait or 0,
        egress=asked_class,
    )
    try:
        grant = await _granted(request, asked)
    except _ClientGone:
        # No one reads this: the connection it would go down is closed.
        raise HTTPException(status_code=400, detail="The client went away while it waited.")
    except RunnerError as exc:
        raise _member_failed(pool, exc)
    except SpecfloError as exc:
        raise _refused(exc)
    except Exception as exc:
        _log.exception("lease_request failed on the daemon")
        raise HTTPException(
            status_code=500,
            detail=f"lease_request failed on the daemon: {type(exc).__name__}.",
        )
    await run_in_threadpool(
        audit, request.app.state.root, identity, "lease_request", None, grant.lease_id
    )
    # The one time the token is sent: the store and the audit record hold no copy.
    return {"result": {"lease_id": grant.lease_id, "agent": grant.agent, "token": grant.token}}


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
    reported as it ended to whoever asks, and nothing changes."""
    token = _text(_body(body, required=(), optional=("token",)), "token")
    _expire_due(service)
    with service.open_store() as store:
        lease = store.get_lease(lease_id)
    if lease is None:
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
        ended = service.end_lease(lease_id, "released")
    except RunnerError as exc:
        # The lease has ended all the same, and this identity ended it.
        audit(request.app.state.root, identity, "lease_release", None, lease_id)
        raise _member_failed(lease.pool, exc)
    except SpecfloError as exc:
        raise _refused(exc)
    # A lease that ended some other way in between was not released by this call.
    if ended.state == "released":
        audit(request.app.state.root, identity, "lease_release", None, lease_id)
    return {"result": {"lease_id": lease_id, "state": ended.state, "held": True}}


# --- the pool's state ---------------------------------------------------------


@router.get(STATUS_PATH)
def pool_status(service: PoolService = Depends(pool_service)) -> dict:
    """Each pool's size and how many of its leases are out; no lease is named
    and no holder."""
    _expire_due(service)
    with service.open_store() as store:
        return {"result": [
            {
                "name": pool.name, "size": pool.size,
                "in_use": len(store.list_leases(state="active", pool=pool.name)),
            }
            for pool in service.config.pools
        ]}
