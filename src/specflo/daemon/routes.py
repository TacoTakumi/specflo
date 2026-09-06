"""The daemon's routes: one per ProjectService operation, behind the token guard.

Each route decodes its request by the operation's parameters, runs the local
service on the daemon root, and answers with the encoded result. Every
operation on one project runs under that project's lock, so concurrent
mutations are serialized and never mint a duplicate id or lose an entry; the
operations that address the root rather than a project share a root lock.

Every request runs as the identity behind its token: the service is built
with that identity as its actor, so an entry added through the daemon
carries an Actor line, and every mutation that succeeds appends one audit
record (time, identity, project, operation, minted id) to the daemon root.

A refusal from the service is a 400 carrying its message, so a client can
raise it unchanged. A request naming a wrong argument is a 422.
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..config import load_config
from ..errors import SpecfloError
from ..service import wire
from ..service.local import LocalProjectService
from .auth import IDENTITIES, identity_for

WHOAMI_PATH = "/whoami"
AUDIT_FILENAME = "audit.jsonl"

# The operations that change nothing; every other operation is a mutation
# and leaves an audit record.
READ_OPERATIONS = frozenset({
    "load_project", "list_projects", "has_artifact", "validate_artifact",
    "index_exists", "index_rule_line", "plan_warnings", "resolution_notes",
    "execution_graph", "active_dependents", "list_tasks", "plan_progress",
    "frontier", "task_brief", "current_task_id", "milestone_progress",
    "milestone_detail", "list_pools", "build_checkpoint", "build_status",
    "show_document",
})
MUTATING_OPERATIONS = frozenset(wire.OPERATIONS) - READ_OPERATIONS


def current_identity(request: Request) -> str:
    """The identity behind the request's bearer token; 401 without a valid one."""
    scheme, _, secret = request.headers.get("Authorization", "").partition(" ")
    identity = None
    if scheme.lower() == "bearer":
        identity = identity_for(request.app.state.root, secret.strip())
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="A bearer token bound to one of "
            + " or ".join(IDENTITIES)
            + " is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity


router = APIRouter(dependencies=[Depends(current_identity)])


@router.get(WHOAMI_PATH)
def whoami(identity: str = Depends(current_identity)) -> dict:
    return {"identity": identity}


# One lock per project (keyed by root and slug) and one per root for the
# operations that address no project; created on first use.
_locks: dict[tuple[str, str | None], threading.RLock] = {}
_registry = threading.Lock()


def project_lock(root: Path, slug: str | None) -> threading.RLock:
    """The lock every operation on ``slug`` under ``root`` runs under."""
    key = (str(root), slug)
    with _registry:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
    return lock


def _audit(root: Path, identity: str, operation: str, project: str | None, minted) -> None:
    """Append one record for a mutation that succeeded."""
    record = {
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "identity": identity,
        "project": project,
        "operation": operation,
        "id": minted,
    }
    with open(root / AUDIT_FILENAME, "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")


def _minted(result, kwargs: dict):
    """The id a mutation minted, or the one it acted on; None when neither."""
    minted = getattr(result, "id", None)
    return minted if isinstance(minted, str) else kwargs.get("task_id") or kwargs.get("milestone_id")


def _handler(operation: wire.Operation):
    def handle(
        request: Request,
        body: dict = Body(default_factory=dict),
        identity: str = Depends(current_identity),
    ) -> dict:
        try:
            kwargs = wire.decode_args(operation, body)
        except wire.WireError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        root = request.app.state.root
        service = LocalProjectService(root, load_config(root), actor=identity)
        slug = kwargs.get("slug") if operation.slug_scoped else None
        with project_lock(root, slug):
            try:
                result = getattr(service, operation.name)(**kwargs)
            except SpecfloError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if operation.name in MUTATING_OPERATIONS:
                project = slug or getattr(result, "slug", None)
                _audit(root, identity, operation.name, project, _minted(result, kwargs))
        return {"result": wire.encode(result)}

    handle.__name__ = operation.name
    handle.__doc__ = getattr(getattr(LocalProjectService, operation.name), "__doc__", None)
    return handle


for _operation in wire.OPERATIONS.values():
    router.add_api_route(
        wire.route_path(_operation.name),
        _handler(_operation),
        methods=["POST"],
        name=_operation.name,
    )
