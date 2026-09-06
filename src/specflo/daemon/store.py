"""The daemon's state store: one interface, SQLite behind it.

Products and their work items are rows, not artifacts: they belong to the
daemon rather than to one project, so they live in the state store under
the daemon root instead of in a markdown file. Everything that reads
or writes them does so through the :class:`Store` interface below and
nothing else. SQLite is the first backend and the one every daemon root
holds; another backend (PostgreSQL, say) implements the same interface and
the product verbs, the work item verbs, and the routes are none the wiser.

Standard library only: sqlite3 ships with Python, so the store works
wherever the daemon root does, with or without the serve extra. A store is
opened per unit of work and closed after it; a SQLite connection belongs to
the thread that opened it, and the daemon answers requests on several.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Protocol

from . import STATE_STORE_FILENAME


@dataclasses.dataclass(frozen=True)
class Product:
    """One product as the store holds it."""

    name: str
    slug: str
    repo: str | None
    vision: str
    created: str


@dataclasses.dataclass(frozen=True)
class WorkItem:
    """One work item as the store holds it; ``id`` is minted by the store."""

    id: int
    product: str
    title: str
    kind: str
    issue: str | None
    dev_path: str
    status: str
    created: str
    piece: str | None = None
    project: str | None = None


class Conflict(Exception):
    """A write that would duplicate a key the store keeps unique."""


class Store(Protocol):
    """What a state store backend provides; every backend implements all of it."""

    def add_product(self, product: Product) -> None:
        """Insert ``product``; raises :class:`Conflict` on a taken slug."""

    def get_product(self, slug: str) -> Product | None:
        """The product called ``slug``, or None."""

    def list_products(self) -> list[Product]:
        """Every product, in slug order."""

    def set_product_vision(self, slug: str, vision: str) -> Product | None:
        """Replace the vision of ``slug``; the updated product, or None if unknown."""

    def add_work_item(self, item: WorkItem) -> WorkItem:
        """Insert ``item`` under a minted id, whatever id it carries; the item as stored."""

    def get_work_item(self, item_id: int) -> WorkItem | None:
        """The work item numbered ``item_id``, or None."""

    def list_work_items(
        self,
        *,
        product: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[WorkItem]:
        """Every work item matching the filters given, in id order."""

    def set_work_item_status(self, item_id: int, status: str) -> WorkItem | None:
        """Replace the status of ``item_id``; the updated item, or None if unknown."""

    def set_work_item_project(self, item_id: int, slug: str) -> WorkItem | None:
        """Record ``slug`` as the project of ``item_id``; the updated item, or None if unknown."""

    def add_piece(self, product: str, name: str) -> None:
        """Declare piece ``name`` on ``product``; raises :class:`Conflict` if declared."""

    def list_pieces(self, product: str) -> list[str]:
        """The pieces declared on ``product``, in declaration order."""

    def remove_piece(self, product: str, name: str) -> bool:
        """Drop piece ``name`` from ``product``; whether there was one."""

    def close(self) -> None:
        """Release the backend's resources."""

    def __enter__(self) -> Store: ...

    def __exit__(self, *exc_info) -> None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    slug    TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    repo    TEXT,
    vision  TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    product  TEXT NOT NULL REFERENCES products(slug),
    title    TEXT NOT NULL,
    kind     TEXT NOT NULL,
    issue    TEXT,
    dev_path TEXT NOT NULL,
    status   TEXT NOT NULL,
    created  TEXT NOT NULL,
    piece    TEXT,
    project  TEXT
);
CREATE TABLE IF NOT EXISTS pieces (
    product TEXT NOT NULL REFERENCES products(slug),
    name    TEXT NOT NULL,
    PRIMARY KEY (product, name)
);
"""

# Columns added after their table's first shape, as (table, column, type):
# CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a store
# opened from before the column gains it here.
ADDED_COLUMNS = (("work_items", "piece", "TEXT"), ("work_items", "project", "TEXT"))

_WORK_ITEM_COLUMNS = "id, product, title, kind, issue, dev_path, status, created, piece, project"


class SqliteStore:
    """The :class:`Store` on a SQLite file; the schema is created on open."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        with self.connection:
            self.connection.executescript(SCHEMA)
            for table, column, kind in ADDED_COLUMNS:
                present = {row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
                if column not in present:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def add_product(self, product: Product) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO products (slug, name, repo, vision, created)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (product.slug, product.name, product.repo, product.vision, product.created),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(product.slug) from exc

    def get_product(self, slug: str) -> Product | None:
        row = self.connection.execute(
            "SELECT name, slug, repo, vision, created FROM products WHERE slug = ?", (slug,)
        ).fetchone()
        return _product(row) if row is not None else None

    def list_products(self) -> list[Product]:
        rows = self.connection.execute(
            "SELECT name, slug, repo, vision, created FROM products ORDER BY slug"
        ).fetchall()
        return [_product(row) for row in rows]

    def set_product_vision(self, slug: str, vision: str) -> Product | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE products SET vision = ? WHERE slug = ?", (vision, slug)
            ).rowcount
        return self.get_product(slug) if changed else None

    def add_work_item(self, item: WorkItem) -> WorkItem:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO work_items"
                " (product, title, kind, issue, dev_path, status, created, piece, project)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.product, item.title, item.kind, item.issue,
                    item.dev_path, item.status, item.created, item.piece, item.project,
                ),
            )
        return dataclasses.replace(item, id=cursor.lastrowid)

    def get_work_item(self, item_id: int) -> WorkItem | None:
        row = self.connection.execute(
            f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items WHERE id = ?", (item_id,)
        ).fetchone()
        return _work_item(row) if row is not None else None

    def list_work_items(
        self,
        *,
        product: str | None = None,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[WorkItem]:
        clauses, values = [], []
        for column, value in (("product", product), ("status", status), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT {_WORK_ITEM_COLUMNS} FROM work_items{where} ORDER BY id", values
        ).fetchall()
        return [_work_item(row) for row in rows]

    def set_work_item_status(self, item_id: int, status: str) -> WorkItem | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE work_items SET status = ? WHERE id = ?", (status, item_id)
            ).rowcount
        return self.get_work_item(item_id) if changed else None

    def set_work_item_project(self, item_id: int, slug: str) -> WorkItem | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE work_items SET project = ? WHERE id = ?", (slug, item_id)
            ).rowcount
        return self.get_work_item(item_id) if changed else None

    def add_piece(self, product: str, name: str) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO pieces (product, name) VALUES (?, ?)", (product, name)
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(name) from exc

    def list_pieces(self, product: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT name FROM pieces WHERE product = ? ORDER BY rowid", (product,)
        ).fetchall()
        return [row["name"] for row in rows]

    def remove_piece(self, product: str, name: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "DELETE FROM pieces WHERE product = ? AND name = ?", (product, name)
            ).rowcount
        return bool(changed)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _product(row: sqlite3.Row) -> Product:
    return Product(
        name=row["name"],
        slug=row["slug"],
        repo=row["repo"],
        vision=row["vision"],
        created=row["created"],
    )


def _work_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(**{column: row[column] for column in _WORK_ITEM_COLUMNS.split(", ")})


def open_store(root: Path) -> Store:
    """The store a daemon root holds, opened for one unit of work."""
    return SqliteStore(Path(root) / STATE_STORE_FILENAME)
