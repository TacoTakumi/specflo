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
raise it unchanged. A request naming a wrong argument is a 422. Anything
else that fails inside the service is a 500 whose detail names the operation
and the kind of failure; the traceback goes to the daemon's log, never to
the client.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import typing
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..config import load_config
from ..errors import SpecfloError
from ..projects import validate_slug
from ..service import wire
from ..service.local import LocalProjectService
from .auth import IDENTITIES, identity_for
from .products import PRODUCTS_PATH, Products
from .store import open_store
from .workitems import WORK_ITEMS_PATH, WorkItems

WHOAMI_PATH = "/whoami"
AUDIT_FILENAME = "audit.jsonl"

_log = logging.getLogger(__name__)

# The operations that change nothing; every other operation is a mutation
# and leaves an audit record.
READ_OPERATIONS = frozenset({
    "load_project", "list_projects", "has_artifact", "validate_artifact",
    "index_exists", "index_rule_line", "plan_warnings", "resolution_notes",
    "execution_graph", "active_dependents", "list_tasks", "plan_progress",
    "frontier", "task_brief", "current_task_id", "milestone_progress",
    "milestone_detail", "list_pools", "build_checkpoint", "build_status",
    "show_document", "export_project", "review_scope", "review_prompt",
    "list_decisions",
})
MUTATING_OPERATIONS = frozenset(wire.OPERATIONS) - READ_OPERATIONS
# Derived writes render what a mutation already recorded (the checkpoint,
# the index, the banners), so they leave no audit record of their own: one
# user action is one record.
DERIVED_OPERATIONS = frozenset({"write_checkpoint", "write_index", "stamp_banners"})
AUDITED_OPERATIONS = MUTATING_OPERATIONS - DERIVED_OPERATIONS
# Operations that create a project directory run under the root lock whatever
# they are scoped to, so two creations of one slug cannot both pass the
# existence check.
CREATING_OPERATIONS = frozenset({"create_project", "import_project"})


def lock_slug(operation: wire.Operation, kwargs: dict) -> str | None:
    """The project whose lock ``operation`` runs under; None for the root lock."""
    if not operation.slug_scoped or operation.name in CREATING_OPERATIONS:
        return None
    return kwargs.get("slug")


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


def audit(root: Path, identity: str, operation: str, project: str | None, minted) -> None:
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


def _changed(operation: wire.Operation, result) -> bool:
    """Whether a mutation changed anything.

    An operation that answers ``(value, flag)`` with a boolean flag reports
    it itself: a brainstorm that was already started, or an execution mode
    already set, is a no-op and no user action, so it leaves no record.
    """
    returns = operation.returns
    if typing.get_origin(returns) is tuple and typing.get_args(returns)[1:] == (bool,):
        return bool(result[1])
    return True


# The mutations the project's agent hears of: its stop on an advance out of
# the chat phase, and the takeover on a take.
AGENT_HOOKS = frozenset({"advance_project", "take_gate"})


def _agent_hook(name: str, root: Path, identity: str, slug: str | None, result) -> None:
    """What the project's agent hears of a mutation that succeeded."""
    # The seat module builds on this one's lock and audit, so it is reached
    # here, not at import.
    from . import seat

    if name == "advance_project":
        if seat.release_on_advance(root, result) is not None:
            audit(root, identity, "stop_agent", slug, None)
    elif name == "take_gate":
        seat.announce_take(root, result)


def _service(root: Path, identity: str) -> LocalProjectService:
    """The local service a request runs as: its identity as actor, its projects hosted."""
    return LocalProjectService(root, load_config(root), actor=identity, hosted=True)


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
        service = _service(root, identity)
        slug = kwargs.get("slug") if operation.slug_scoped else None
        # The slug is the one part of a request that becomes a path under the
        # daemon root, so it is checked here for every operation that names
        # one, before any lock is taken and before the service sees it.
        if operation.slug_scoped:
            try:
                validate_slug(slug)
            except SpecfloError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        with project_lock(root, lock_slug(operation, kwargs)):
            try:
                result = getattr(service, operation.name)(**kwargs)
            except SpecfloError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                _log.exception("%s failed on the daemon", operation.name)
                raise HTTPException(
                    status_code=500,
                    detail=f"{operation.name} failed on the daemon: {type(exc).__name__}.",
                )
            if operation.name in AUDITED_OPERATIONS and _changed(operation, result):
                project = slug or getattr(result, "slug", None)
                audit(root, identity, operation.name, project, _minted(result, kwargs))
            if operation.name in AGENT_HOOKS:
                _agent_hook(operation.name, root, identity, slug, result)
        # The boundary: what leaves here describes the daemon's projects, so a
        # path is written relative to the root and the host's layout stays home.
        return {"result": wire.encode(result, root)}

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


# --- products: rows in the state store, not artifacts of a project -----------
# The product routes are written out rather than generated: products are not
# a ProjectService operation, and their store is opened per request because a
# SQLite connection belongs to the thread that opened it.


def _fields(body, *, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    """The request body's fields; 422 for an unknown field, a missing one, or a wrong type."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="The request body must be a JSON object.")
    unknown = sorted(set(body) - set(required) - set(optional))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown field(s) {', '.join(unknown)}.")
    missing = [name for name in required if name not in body]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing field(s) {', '.join(missing)}.")
    for name, value in body.items():
        if not isinstance(value, str) and not (value is None and name in optional):
            raise HTTPException(status_code=422, detail=f"Field {name!r} must be a string.")
    return dict(body)


def _refused(exc: SpecfloError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.post(PRODUCTS_PATH)
def product_add(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    fields = _fields(body, required=("name",), optional=("slug", "repo"))
    root = request.app.state.root
    with project_lock(root, None), open_store(root) as store:
        try:
            product = Products(store).add(
                fields["name"], slug=fields.get("slug"), repo=fields.get("repo")
            )
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "product_add", None, product.slug)
    return {"result": wire.encode(product)}


@router.get(PRODUCTS_PATH)
def product_list(request: Request) -> dict:
    with open_store(request.app.state.root) as store:
        return {"result": wire.encode(Products(store).list())}


@router.get(PRODUCTS_PATH + "/{slug}")
def product_show(request: Request, slug: str) -> dict:
    with open_store(request.app.state.root) as store:
        try:
            product = Products(store).show(slug)
        except SpecfloError as exc:
            raise _refused(exc)
    return {"result": wire.encode(product)}


@router.put(PRODUCTS_PATH + "/{slug}/vision")
def product_set_vision(
    request: Request,
    slug: str,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    fields = _fields(body, required=("vision",))
    root = request.app.state.root
    with project_lock(root, None), open_store(root) as store:
        try:
            product = Products(store).set_vision(slug, fields["vision"])
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "product_set_vision", None, slug)
    return {"result": wire.encode(product)}


@router.get(PRODUCTS_PATH + "/{slug}/roadmap")
def product_roadmap(request: Request, slug: str) -> dict:
    with open_store(request.app.state.root) as store:
        try:
            roadmap = Products(store).roadmap(slug)
        except SpecfloError as exc:
            raise _refused(exc)
    return {"result": wire.encode(roadmap)}


PIECES_PATH = PRODUCTS_PATH + "/{slug}/pieces"


@router.get(PIECES_PATH)
def product_pieces(request: Request, slug: str) -> dict:
    with open_store(request.app.state.root) as store:
        try:
            pieces = Products(store).list_pieces(slug)
        except SpecfloError as exc:
            raise _refused(exc)
    return {"result": pieces}


@router.post(PIECES_PATH)
def product_piece_add(
    request: Request,
    slug: str,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    fields = _fields(body, required=("name",))
    root = request.app.state.root
    with project_lock(root, None), open_store(root) as store:
        try:
            pieces = Products(store).add_piece(slug, fields["name"])
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "product_piece_add", None, f"{slug}/{fields['name']}")
    return {"result": pieces}


@router.delete(PIECES_PATH + "/{name}")
def product_piece_remove(
    request: Request, slug: str, name: str, identity: str = Depends(current_identity)
) -> dict:
    root = request.app.state.root
    with project_lock(root, None), open_store(root) as store:
        try:
            pieces = Products(store).remove_piece(slug, name)
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "product_piece_remove", None, f"{slug}/{name}")
    return {"result": pieces}


# --- work items: a product's backlog, rows beside the products ---------------


@router.post(WORK_ITEMS_PATH)
def workitem_add(
    request: Request,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    fields = _fields(
        body, required=("product", "title"), optional=("kind", "issue", "dev_path", "piece")
    )
    root = request.app.state.root
    with project_lock(root, None), open_store(root) as store:
        try:
            item = WorkItems(store).add(
                fields["product"],
                fields["title"],
                kind=fields.get("kind"),
                issue=fields.get("issue"),
                dev_path=fields.get("dev_path"),
                piece=fields.get("piece"),
            )
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "workitem_add", None, str(item.id))
    return {"result": wire.encode(item)}


@router.get(WORK_ITEMS_PATH)
def workitem_list(
    request: Request,
    product: str | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> dict:
    with open_store(request.app.state.root) as store:
        try:
            items = WorkItems(store).list(product=product, status=status, kind=kind)
        except SpecfloError as exc:
            raise _refused(exc)
    return {"result": wire.encode(items)}


@router.get(WORK_ITEMS_PATH + "/{item_id}")
def workitem_show(request: Request, item_id: int) -> dict:
    with open_store(request.app.state.root) as store:
        try:
            item = WorkItems(store).show(item_id)
        except SpecfloError as exc:
            raise _refused(exc)
    return {"result": wire.encode(item)}


@router.put(WORK_ITEMS_PATH + "/{item_id}/status")
def workitem_set_status(
    request: Request,
    item_id: int,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    fields = _fields(body, required=("status",))
    root = request.app.state.root
    with project_lock(root, None), open_store(root) as store:
        try:
            item = WorkItems(store).set_status(item_id, fields["status"])
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "workitem_set_status", None, str(item_id))
    return {"result": wire.encode(item)}


@router.post(WORK_ITEMS_PATH + "/{item_id}/start-project")
def workitem_start_project(
    request: Request,
    item_id: int,
    identity: str = Depends(current_identity),
) -> dict:
    """Spawn the item's project, scaffold its seat, and start its agent: one operation."""
    # The seat module builds on this one's lock and audit, so it is imported
    # where it is used rather than at the top.
    from . import seat

    root = request.app.state.root
    url = getattr(request.app.state, "url", None) or seat.DEFAULT_URL
    pumps = getattr(request.app.state, "pumps", None)
    try:
        started = seat.start_project(
            root, item_id, identity, url=url, subscribe=pumps.subscribe if pumps is not None else None
        )
    except seat.AgentStartError as exc:
        # The CLI's words can name paths under the root; they go to the log,
        # and the response names the agent and the project only.
        _log.warning("start-project for item %s: %s", item_id, exc)
        raise HTTPException(status_code=502, detail=exc.public)
    except SpecfloError as exc:
        raise _refused(exc)
    return {"result": wire.encode(started, root)}


@router.post(WORK_ITEMS_PATH + "/{item_id}/spawn")
def workitem_spawn(
    request: Request,
    item_id: int,
    body: dict = Body(default_factory=dict),
    identity: str = Depends(current_identity),
) -> dict:
    fields = _fields(body, required=(), optional=("name",))
    root = request.app.state.root
    service = _service(root, identity)
    with project_lock(root, None), open_store(root) as store:
        try:
            spawned = WorkItems(store).spawn(item_id, service, name=fields.get("name"))
        except SpecfloError as exc:
            raise _refused(exc)
        audit(root, identity, "workitem_spawn", spawned.project.slug, str(item_id))
    return {"result": wire.encode(spawned, root)}
