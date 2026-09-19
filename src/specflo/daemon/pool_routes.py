"""The daemon's pool routes: the agent pool over HTTP, behind the token guard.

The pool is the daemon's, not a project's: its configuration is the pool
directory under the daemon root, its leases are rows in the daemon's store,
and its members run on the daemon's host. One pool service stands for the
whole process, made when the application is and kept on ``app.state.pool``;
a root with no pool directory has none, and every route here then refuses.

The routes are written out, like the product routes. Each one runs as the
identity behind its bearer token, and one that changes the pool appends an
audit record with that identity and the lease it acted on. The service takes
its own turns, so a route holds no lock of the daemon's.

A refusal from the pool is a 400 carrying its message. A member that does not
start or stop is a 502: what the agent CLI said of it can name paths on this
host, so that goes to the daemon's log and the response names the pool only.
No route here carries anything a member wrote.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..errors import SpecfloError
from ..pool.cli_admin import pool_dir
from ..pool.config import load_pool_config
from ..pool.runner import RunnerError
from ..pool.service import PoolService
from ..service.pool_remote import LEASES_PATH
from .poolstore import open_pool_store
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


def open_pool(root: Path) -> PoolService | None:
    """The pool service of the daemon root *root*; None when it declares no pool."""
    directory = pool_dir(root)
    if not directory.is_dir():
        return None
    config, errors = load_pool_config(directory)
    if errors:
        for error in errors:
            _log.warning("pool configuration: %s", error)
        return None
    return PoolService(
        config=config,
        open_store=lambda: open_pool_store(root),
        pool_token=pool_token(root),
        config_root=Path(root) / PI_CONFIG_DIRNAME,
    )


def pool_service(request: Request) -> PoolService:
    """The pool a request is served by; a refusal when the daemon has none."""
    service = getattr(request.app.state, "pool", None)
    if service is None:
        raise HTTPException(status_code=400, detail="No pool is configured on this daemon.")
    return service


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


@router.post(LEASES_PATH)
def lease_request(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
    service: PoolService = Depends(pool_service),
) -> dict:
    """Grant a lease on a free member of the named pool, started in the named directory."""
    fields = _body(body, required=("pool", "cwd"), optional=("idle_limit", "label"))
    pool = _text(fields, "pool")
    cwd = _text(fields, "cwd")
    idle_limit = _seconds(fields, "idle_limit")
    holder_label = _holder_label(identity, _text(fields, "label"))
    if pool is None or cwd is None:
        raise _invalid("Fields 'pool' and 'cwd' must be strings that are not empty.")
    # The member runs on this host, so the directory is one of this host's.
    if not os.path.isabs(cwd) or not os.path.isdir(cwd):
        raise HTTPException(
            status_code=400,
            detail=f"The working directory '{cwd}' is not a directory on the daemon's host; "
            "a member starts in an absolute path that is there.",
        )
    try:
        grant = service.grant(pool, holder_label=holder_label, cwd=cwd, idle_limit=idle_limit)
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
    audit(request.app.state.root, identity, "lease_request", None, grant.lease_id)
    # The one time the token is sent: the store and the audit record hold no copy.
    return {"result": {"lease_id": grant.lease_id, "agent": grant.agent, "token": grant.token}}
