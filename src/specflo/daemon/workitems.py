"""Work items: the product-level entries a backlog is made of.

A work item belongs to one product and carries a title, a kind, an optional
issue link, a dev path, a status, and optionally the product piece it
targets, which must be one the product declares. Kind is free text: four
values are offered and any other is accepted, since what a product calls
its work is its own business. The dev path is not: it names how the item gets built (a
full specflo project, one prompt, or a cyclical handoff) and anything else
is refused. Status is a fixed set too, so a backlog can be filtered by it.

A full-path item spawns exactly one specflo project, hosted on the daemon
beside it: the project's front matter records the item, and the item
records the project's slug, so a second spawn is refused by name.

Like products, work items live in the daemon's state store and the verbs
run where the store is: :class:`WorkItems` in-process behind the daemon's
routes, :class:`RemoteWorkItems` over HTTP from a CLI client.
"""

from __future__ import annotations

import dataclasses
import datetime
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ..errors import SpecfloError
from ..projects import Project
from ..service import wire
from ..service.protocol import ProjectService
from .products import DaemonClient, Products
from .store import Store, WorkItem

WORK_ITEMS_PATH = "/api/workitems"

KINDS = ("fix", "roadmap", "idea", "issue")
DEV_PATHS = ("full", "one-prompt", "cyclical")
STATUSES = ("open", "in-progress", "done", "dropped")
DEFAULT_KIND = KINDS[0]
FULL_DEV_PATH = DEV_PATHS[0]
DEFAULT_DEV_PATH = FULL_DEV_PATH
DEFAULT_STATUS = STATUSES[0]


@dataclasses.dataclass(frozen=True)
class Spawned:
    """What a spawn made: the item as it now reads, the project, and its first artifact."""

    item: WorkItem
    project: Project
    brainstorm: Path


def validate_dev_path(dev_path: str) -> str:
    """``dev_path``, or a refusal naming the three there are."""
    if dev_path not in DEV_PATHS:
        raise SpecfloError(
            f"Invalid dev path {dev_path!r}: expected one of " + ", ".join(DEV_PATHS) + "."
        )
    return dev_path


def validate_issue_link(issue: str | None) -> str | None:
    """``issue`` as a link, None for no link, or a refusal.

    Only http and https links are kept: the link is rendered as one on the
    product page, and any other scheme would run in the browser of whoever
    follows it rather than open an issue.
    """
    if issue is None:
        return None
    issue = issue.strip()
    if not issue:
        return None
    parts = urlparse(issue)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SpecfloError(f"Invalid issue link {issue!r}: expected an http or https URL.")
    return issue


def validate_status(status: str) -> str:
    """``status``, or a refusal naming the statuses there are."""
    if status not in STATUSES:
        raise SpecfloError(
            f"Invalid status {status!r}: expected one of " + ", ".join(STATUSES) + "."
        )
    return status


class WorkItems:
    """The work item verbs on one :class:`Store`."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def add(
        self,
        product: str,
        title: str,
        *,
        kind: str | None = None,
        issue: str | None = None,
        dev_path: str | None = None,
        piece: str | None = None,
        today: str | None = None,
    ) -> WorkItem:
        """Add a work item to ``product``'s backlog; refuses an unknown product, dev path, or piece."""
        Products(self.store).show(product)
        title = title.strip()
        if not title:
            raise SpecfloError("A work item needs a title.")
        kind = (DEFAULT_KIND if kind is None else kind).strip()
        if not kind:
            raise SpecfloError(
                "A work item needs a kind; the usual ones are " + ", ".join(KINDS) + "."
            )
        item = WorkItem(
            id=0,
            product=product,
            title=title,
            kind=kind,
            issue=validate_issue_link(issue),
            dev_path=validate_dev_path(DEFAULT_DEV_PATH if dev_path is None else dev_path),
            status=DEFAULT_STATUS,
            created=today or datetime.date.today().isoformat(),
            piece=self._target(product, piece),
        )
        return self.store.add_work_item(item)

    def _target(self, product: str, piece: str | None) -> str | None:
        """``piece`` if ``product`` declares it; None for no target; a refusal otherwise."""
        if piece is None:
            return None
        declared = self.store.list_pieces(product)
        if not declared:
            raise SpecfloError(f"Product {product!r} declares no pieces; drop --piece.")
        if piece not in declared:
            raise SpecfloError(
                f"No piece {piece!r} on {product!r}; declared: " + ", ".join(declared) + "."
            )
        return piece

    def list(
        self,
        *,
        product: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[WorkItem]:
        """Work items in backlog order, narrowed by whichever filters are given."""
        if product is not None:
            Products(self.store).show(product)
        if status is not None:
            validate_status(status)
        return self.store.list_work_items(product=product, status=status, kind=kind)

    def show(self, item_id: int) -> WorkItem:
        """The work item numbered ``item_id``; refuses an unknown one."""
        item = self.store.get_work_item(item_id)
        if item is None:
            raise SpecfloError(_unknown(item_id))
        return item

    def set_status(self, item_id: int, status: str) -> WorkItem:
        """Move ``item_id`` to ``status``; refuses an unknown item or status."""
        item = self.store.set_work_item_status(item_id, validate_status(status))
        if item is None:
            raise SpecfloError(_unknown(item_id))
        return item

    def spawn(
        self, item_id: int, service: ProjectService, *, name: str | None = None
    ) -> Spawned:
        """Create the one project a full-path item gets, on ``service``, linked both ways.

        The project takes the item's title as its name unless ``name`` says
        otherwise and as its summary either way, and records the item and the
        piece it targets; the item records the project's slug. Refuses an
        item on another dev path and one that already has its project.
        """
        item = self.show(item_id)
        if item.dev_path != FULL_DEV_PATH:
            raise SpecfloError(
                f"Work item {item.id} has dev path {item.dev_path!r}; only a"
                f" {FULL_DEV_PATH!r} item spawns a project."
            )
        if item.project is not None:
            raise SpecfloError(
                f"Work item {item.id} already spawned project {item.project!r}."
            )
        project = service.create_project(
            item.title if name is None else name,
            summary=item.title,
            work_item=item.id,
            piece=item.piece,
        )
        try:
            brainstorm, _ = service.start_brainstorm(project.slug)
            service.write_checkpoint(project.slug)
            item = self.store.set_work_item_project(item.id, project.slug)
            if item is None:
                raise SpecfloError(_unknown(item_id))
        except BaseException:
            # A spawn that fails part way leaves nothing behind: the directory
            # goes, so the item stays unlinked and a retry is not refused as
            # a project that already exists.
            shutil.rmtree(project.path, ignore_errors=True)
            raise
        return Spawned(item=item, project=project, brainstorm=brainstorm)


def _unknown(item_id: int) -> str:
    return f"No work item {item_id}. Run `specflo workitem list` to see the ones there are."


class RemoteWorkItems(DaemonClient):
    """The work item verbs over HTTP to the daemon at ``url``."""

    def add(
        self,
        product: str,
        title: str,
        *,
        kind: str | None = None,
        issue: str | None = None,
        dev_path: str | None = None,
        piece: str | None = None,
    ) -> WorkItem:
        body = {"product": product, "title": title}
        for key, value in (
            ("kind", kind), ("issue", issue), ("dev_path", dev_path), ("piece", piece),
        ):
            if value is not None:
                body[key] = value
        return self._item(self._request("POST", WORK_ITEMS_PATH, json=body))

    def list(
        self,
        *,
        product: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[WorkItem]:
        params = {
            key: value
            for key, value in (("product", product), ("status", status), ("kind", kind))
            if value is not None
        }
        return [self._item(item) for item in self._request("GET", WORK_ITEMS_PATH, params=params)]

    def show(self, item_id: int) -> WorkItem:
        return self._item(self._request("GET", f"{WORK_ITEMS_PATH}/{item_id}"))

    def set_status(self, item_id: int, status: str) -> WorkItem:
        return self._item(
            self._request("PUT", f"{WORK_ITEMS_PATH}/{item_id}/status", json={"status": status})
        )

    def spawn(self, item_id: int, *, name: str | None = None) -> Spawned:
        body = {} if name is None else {"name": name}
        encoded = self._request("POST", f"{WORK_ITEMS_PATH}/{item_id}/spawn", json=body)
        return wire.decode(encoded, Spawned)

    @staticmethod
    def _item(encoded) -> WorkItem:
        return wire.decode(encoded, WorkItem)
