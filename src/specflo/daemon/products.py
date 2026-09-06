"""Products: the things work items and projects belong to.

A product has a name, a slug, an optional repo location, a vision text, and
zero or more declared pieces: the deployable parts it is made of (web,
admin, mobile, ...). A work item may target a declared piece and nothing
else; a product that declares none takes items with no target. The
roadmap is a read view over all of it: the vision, then the backlog in
order; nothing is ever written for it.
It lives in the daemon's state store, never under a checkout, so the verbs
here run where the store is. The daemon runs :class:`Products` on its own
store, in-process, behind the product routes; a CLI client runs
:class:`RemoteProducts`, which sends each verb to those routes and rebuilds
the same values, raising the same ``SpecfloError`` on a refusal. A command
cannot tell the two apart, the same way it cannot tell the local and remote
ProjectService apart.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from urllib.parse import quote

import httpx

from ..errors import SpecfloError
from ..projects import slugify
from ..service import wire
from ..service.remote import response_detail
from .store import Conflict, Product, Store, WorkItem

PRODUCTS_PATH = "/api/products"

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclasses.dataclass(frozen=True)
class Roadmap:
    """A read view over one product: its vision, then its backlog in order."""

    product: Product
    items: list[WorkItem]


def validate_piece_name(name: str) -> str:
    """``name``, or a refusal: a piece is named like a slug."""
    if not _SLUG.match(name or ""):
        raise SpecfloError(
            f"Invalid piece name {name!r}: use lowercase letters, digits and -, e.g. web."
        )
    return name


def product_slug(name: str, slug: str | None) -> str:
    """The slug a product is filed under: ``slug`` as given, or derived from ``name``."""
    if slug is not None:
        if not _SLUG.match(slug):
            raise SpecfloError(
                f"Invalid product slug {slug!r}: use lowercase letters, digits and -,"
                " e.g. my-thing."
            )
        return slug
    try:
        return slugify(name)
    except SpecfloError:
        raise SpecfloError(f"Cannot derive a product slug from {name!r}; pass --slug.")


class Products:
    """The product verbs on one :class:`Store`."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def add(
        self,
        name: str,
        *,
        slug: str | None = None,
        repo: str | None = None,
        today: str | None = None,
    ) -> Product:
        """Add a product; refuses a blank name, a bad slug, and a taken slug."""
        name = name.strip()
        if not name:
            raise SpecfloError("A product needs a name.")
        product = Product(
            name=name,
            slug=product_slug(name, slug),
            repo=(repo.strip() or None) if repo is not None else None,
            vision="",
            created=today or datetime.date.today().isoformat(),
        )
        try:
            self.store.add_product(product)
        except Conflict:
            raise SpecfloError(f"Product {product.slug!r} already exists.")
        return product

    def list(self) -> list[Product]:
        """Every product, in slug order."""
        return self.store.list_products()

    def show(self, slug: str) -> Product:
        """The product called ``slug``; refuses an unknown one."""
        product = self.store.get_product(slug)
        if product is None:
            raise SpecfloError(_unknown(slug))
        return product

    def set_vision(self, slug: str, vision: str) -> Product:
        """Replace the vision of ``slug``; refuses an unknown one."""
        product = self.store.set_product_vision(slug, vision)
        if product is None:
            raise SpecfloError(_unknown(slug))
        return product

    def list_pieces(self, slug: str) -> list[str]:
        """The pieces declared on ``slug``, in declaration order."""
        self.show(slug)
        return self.store.list_pieces(slug)

    def add_piece(self, slug: str, name: str) -> list[str]:
        """Declare ``name`` on ``slug``; the pieces declared after. Refuses a repeat."""
        self.show(slug)
        name = validate_piece_name(name)
        try:
            self.store.add_piece(slug, name)
        except Conflict:
            raise SpecfloError(f"Piece {name!r} is already declared on {slug!r}.")
        return self.store.list_pieces(slug)

    def remove_piece(self, slug: str, name: str) -> list[str]:
        """Drop ``name`` from ``slug``; the pieces left. A targeted piece stays."""
        self.show(slug)
        if name not in self.store.list_pieces(slug):
            raise SpecfloError(f"No piece {name!r} on {slug!r}.")
        targeting = [
            item.id for item in self.store.list_work_items(product=slug) if item.piece == name
        ]
        if targeting:
            raise SpecfloError(
                f"Piece {name!r} is targeted by work item "
                + ", ".join(str(item_id) for item_id in targeting)
                + "; retarget them first."
            )
        self.store.remove_piece(slug, name)
        return self.store.list_pieces(slug)

    def roadmap(self, slug: str) -> Roadmap:
        """The product's vision, then its work items in backlog order; read, never written."""
        product = self.show(slug)
        return Roadmap(product=product, items=self.store.list_work_items(product=slug))


def _unknown(slug: str) -> str:
    return f"No product {slug!r}. Run `specflo product list` to see the ones there are."


class DaemonClient:
    """What a client of the daemon's row routes shares: URL, token, one request shape.

    ``client`` lets a caller supply the HTTP client (a test drives the daemon
    in-process through one); otherwise one is opened against ``url``. A 200
    carries the result; a refusal comes back as the ``SpecfloError`` the
    daemon's verb raised, so a command cannot tell remote from in-process.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.client = (
            client if client is not None else httpx.Client(base_url=self.url, timeout=timeout)
        )
        self.client.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise SpecfloError(f"Cannot reach the remote at {self.url}: {exc}") from exc
        if response.status_code == 200:
            return response.json()["result"]
        detail = response_detail(response)
        if response.status_code == 401:
            raise SpecfloError(f"The remote at {self.url} refused the token: {detail}")
        if response.status_code in (400, 422):
            raise SpecfloError(detail)
        raise SpecfloError(
            f"The remote at {self.url} answered {response.status_code}"
            f" to {method} {path}: {detail}"
        )


class RemoteProducts(DaemonClient):
    """The product verbs over HTTP to the daemon at ``url``."""

    def add(
        self, name: str, *, slug: str | None = None, repo: str | None = None
    ) -> Product:
        body = {"name": name}
        if slug is not None:
            body["slug"] = slug
        if repo is not None:
            body["repo"] = repo
        return self._product(self._request("POST", PRODUCTS_PATH, json=body))

    def list(self) -> list[Product]:
        return [self._product(item) for item in self._request("GET", PRODUCTS_PATH)]

    def show(self, slug: str) -> Product:
        return self._product(self._request("GET", f"{PRODUCTS_PATH}/{quote(slug, safe='')}"))

    def set_vision(self, slug: str, vision: str) -> Product:
        return self._product(
            self._request("PUT", f"{PRODUCTS_PATH}/{quote(slug, safe='')}/vision", json={"vision": vision})
        )

    def list_pieces(self, slug: str) -> list[str]:
        return self._request("GET", f"{PRODUCTS_PATH}/{quote(slug, safe='')}/pieces")

    def add_piece(self, slug: str, name: str) -> list[str]:
        return self._request("POST", f"{PRODUCTS_PATH}/{quote(slug, safe='')}/pieces", json={"name": name})

    def remove_piece(self, slug: str, name: str) -> list[str]:
        return self._request(
            "DELETE", f"{PRODUCTS_PATH}/{quote(slug, safe='')}/pieces/{quote(name, safe='')}"
        )

    def roadmap(self, slug: str) -> Roadmap:
        return wire.decode(
            self._request("GET", f"{PRODUCTS_PATH}/{quote(slug, safe='')}/roadmap"), Roadmap
        )

    @staticmethod
    def _product(encoded) -> Product:
        return wire.decode(encoded, Product)
