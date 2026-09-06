"""Work items: the product-level entries a backlog is made of.

A work item belongs to one product and carries a title, a kind, an optional
issue link, a dev path, a status, and optionally the product piece it
targets, which must be one the product declares. Kind is free text: four
values are offered and any other is accepted, since what a product calls
its work is its own business. The dev path is not: it names how the item gets built (a
full specflo project, one prompt, or a cyclical handoff) and anything else
is refused. Status is a fixed set too, so a backlog can be filtered by it.

Like products, work items live in the daemon's state store and the verbs
run where the store is: :class:`WorkItems` in-process behind the daemon's
routes, :class:`RemoteWorkItems` over HTTP from a CLI client.
"""

from __future__ import annotations

import datetime

from ..errors import SpecfloError
from ..service import wire
from .products import DaemonClient, Products
from .store import Store, WorkItem

WORK_ITEMS_PATH = "/api/workitems"

KINDS = ("fix", "roadmap", "idea", "issue")
DEV_PATHS = ("full", "one-prompt", "cyclical")
STATUSES = ("open", "in-progress", "done", "dropped")
DEFAULT_KIND = KINDS[0]
DEFAULT_DEV_PATH = DEV_PATHS[0]
DEFAULT_STATUS = STATUSES[0]


def validate_dev_path(dev_path: str) -> str:
    """``dev_path``, or a refusal naming the three there are."""
    if dev_path not in DEV_PATHS:
        raise SpecfloError(
            f"Invalid dev path {dev_path!r}: expected one of " + ", ".join(DEV_PATHS) + "."
        )
    return dev_path


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
            issue=(issue.strip() or None) if issue is not None else None,
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

    @staticmethod
    def _item(encoded) -> WorkItem:
        return wire.decode(encoded, WorkItem)
