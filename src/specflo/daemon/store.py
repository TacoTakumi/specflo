"""The daemon's state store: one interface, SQLite behind it.

Products and their work items are rows, not artifacts: they belong to the
daemon rather than to one project, so they live in the state store under
the daemon root instead of in a markdown file. Everything that reads
or writes them does so through the :class:`Store` interface below and
nothing else. SQLite is the first backend and the one every daemon root
holds; another backend (PostgreSQL, say) implements the same interface and
the product verbs, the work item verbs, and the routes are none the wiser.

Named users, the tokens minted for them and their browser sessions are
held here the same way and for the same reason: no SQL on those tables
lives anywhere but in a backend. A token's owner is a user name or the
built-in agent, which has no user row, so tokens carry no foreign key to
users; removing a user drops their tokens and sessions in the same
transaction instead.

Standard library only: sqlite3 ships with Python, so the store works
wherever the daemon root does, with or without the serve extra. A store is
opened per unit of work and closed after it; a SQLite connection belongs to
the thread that opened it, and the daemon answers requests on several.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Protocol, Self

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


# The roles a user may hold and the ways a session may have been opened.
ROLES = ("developer", "requester")
SESSION_METHODS = ("password", "proxy")


@dataclasses.dataclass(frozen=True)
class User:
    """One named user; a None ``password_hash`` is a proxy-only account."""

    name: str
    role: str
    password_hash: str | None
    created: str


@dataclasses.dataclass(frozen=True)
class Token:
    """One bearer token as the store holds it: never the secret, only its hash.

    ``owner`` is a user name or the built-in agent; ``name`` is the token's
    label, unique per owner; ``last4`` is the tail of the secret, enough to
    tell two tokens apart in a listing.
    """

    owner: str
    name: str
    hash: str
    last4: str
    created: str
    last_used: str | None = None


@dataclasses.dataclass(frozen=True)
class Session:
    """One browser session, keyed by the hash of its cookie secret.

    ``expires`` and ``written`` are epoch seconds; ``written`` is when the
    row was last written, so a caller can skip a write that would change
    little.
    """

    hash: str
    user: str
    method: str
    expires: float
    written: float


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

    def add_user(self, user: User) -> None:
        """Insert ``user``; raises :class:`Conflict` on a taken name.

        A name with capitals or a role outside :data:`ROLES` raises ValueError.
        """

    def get_user(self, name: str) -> User | None:
        """The user called ``name``, or None."""

    def list_users(self) -> list[User]:
        """Every user, in name order."""

    def set_user_password(self, name: str, password_hash: str | None) -> User | None:
        """Replace or clear the password hash of ``name``; the updated user, or None if unknown."""

    def set_user_role(self, name: str, role: str) -> User | None:
        """Replace the role of ``name``; the updated user, or None if unknown."""

    def remove_user(self, name: str) -> bool:
        """Drop ``name`` with its tokens and sessions in one transaction; whether there was one."""

    def add_token(self, token: Token) -> None:
        """Insert ``token``; raises :class:`Conflict` on a taken hash or owner and name."""

    def list_tokens(self, owner: str | None = None) -> list[Token]:
        """The tokens of ``owner``, or every token, in owner then name order."""

    def find_token(self, token_hash: str) -> Token | None:
        """The token whose secret hashes to ``token_hash``, or None."""

    def revoke_token(self, owner: str, name: str) -> bool:
        """Drop the token ``name`` of ``owner``; whether there was one."""

    def touch_token(self, token_hash: str, when: str) -> bool:
        """Record ``when`` as the last use of ``token_hash``; whether there was one."""

    def add_session(self, session: Session) -> None:
        """Insert ``session``; raises :class:`Conflict` on a taken hash.

        A method outside :data:`SESSION_METHODS` or an unknown user raises ValueError.
        """

    def get_session(self, session_hash: str) -> Session | None:
        """The session keyed by ``session_hash``, or None."""

    def update_session(
        self, session_hash: str, *, expires: float, written: float
    ) -> Session | None:
        """Move the expiry and write time of ``session_hash``; the updated session, or None."""

    def remove_session(self, session_hash: str) -> bool:
        """Drop the session keyed by ``session_hash``; whether there was one."""

    def remove_user_sessions(self, user: str, *, keep: str | None = None) -> int:
        """Drop every session of ``user`` but the one keyed by ``keep``; how many went."""

    def remove_expired_sessions(self, now: float) -> int:
        """Drop every session whose expiry is at or before ``now``; how many went."""

    def close(self) -> None:
        """Release the backend's resources."""

    def __enter__(self) -> Self: ...

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
CREATE TABLE IF NOT EXISTS users (
    name          TEXT PRIMARY KEY,
    role          TEXT NOT NULL,
    password_hash TEXT,
    created       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    hash      TEXT PRIMARY KEY,
    owner     TEXT NOT NULL,
    name      TEXT NOT NULL,
    last4     TEXT NOT NULL,
    created   TEXT NOT NULL,
    last_used TEXT,
    UNIQUE (owner, name)
);
CREATE TABLE IF NOT EXISTS sessions (
    hash    TEXT PRIMARY KEY,
    user    TEXT NOT NULL REFERENCES users(name),
    method  TEXT NOT NULL,
    expires REAL NOT NULL,
    written REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions (user);
"""

# Columns added after their table's first shape, as (table, column, type):
# CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a store
# opened from before the column gains it here.
ADDED_COLUMNS = (("work_items", "piece", "TEXT"), ("work_items", "project", "TEXT"))

_WORK_ITEM_COLUMNS = "id, product, title, kind, issue, dev_path, status, created, piece, project"
_USER_COLUMNS = "name, role, password_hash, created"
_TOKEN_COLUMNS = "owner, name, hash, last4, created, last_used"
_SESSION_COLUMNS = "hash, user, method, expires, written"


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

    def add_user(self, user: User) -> None:
        if user.name != user.name.lower():
            raise ValueError(f"A user name is lowercase: {user.name!r}.")
        _check_role(user.role)
        try:
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO users ({_USER_COLUMNS}) VALUES (?, ?, ?, ?)",
                    (user.name, user.role, user.password_hash, user.created),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(user.name) from exc

    def get_user(self, name: str) -> User | None:
        row = self.connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users WHERE name = ?", (name,)
        ).fetchone()
        return _user(row) if row is not None else None

    def list_users(self) -> list[User]:
        rows = self.connection.execute(
            f"SELECT {_USER_COLUMNS} FROM users ORDER BY name"
        ).fetchall()
        return [_user(row) for row in rows]

    def set_user_password(self, name: str, password_hash: str | None) -> User | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE users SET password_hash = ? WHERE name = ?", (password_hash, name)
            ).rowcount
        return self.get_user(name) if changed else None

    def set_user_role(self, name: str, role: str) -> User | None:
        _check_role(role)
        with self.connection:
            changed = self.connection.execute(
                "UPDATE users SET role = ? WHERE name = ?", (role, name)
            ).rowcount
        return self.get_user(name) if changed else None

    def remove_user(self, name: str) -> bool:
        # One transaction: a user is never left half removed, with their
        # row gone and a token still able to act for them, or the reverse.
        with self.connection:
            self.connection.execute("DELETE FROM tokens WHERE owner = ?", (name,))
            self.connection.execute("DELETE FROM sessions WHERE user = ?", (name,))
            changed = self.connection.execute(
                "DELETE FROM users WHERE name = ?", (name,)
            ).rowcount
        return bool(changed)

    def add_token(self, token: Token) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO tokens ({_TOKEN_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        token.owner, token.name, token.hash,
                        token.last4, token.created, token.last_used,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"{token.owner}/{token.name}") from exc

    def list_tokens(self, owner: str | None = None) -> list[Token]:
        where, values = (" WHERE owner = ?", (owner,)) if owner is not None else ("", ())
        rows = self.connection.execute(
            f"SELECT {_TOKEN_COLUMNS} FROM tokens{where} ORDER BY owner, name", values
        ).fetchall()
        return [_token(row) for row in rows]

    def find_token(self, token_hash: str) -> Token | None:
        row = self.connection.execute(
            f"SELECT {_TOKEN_COLUMNS} FROM tokens WHERE hash = ?", (token_hash,)
        ).fetchone()
        return _token(row) if row is not None else None

    def revoke_token(self, owner: str, name: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "DELETE FROM tokens WHERE owner = ? AND name = ?", (owner, name)
            ).rowcount
        return bool(changed)

    def touch_token(self, token_hash: str, when: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE tokens SET last_used = ? WHERE hash = ?", (when, token_hash)
            ).rowcount
        return bool(changed)

    def add_session(self, session: Session) -> None:
        if session.method not in SESSION_METHODS:
            raise ValueError(
                f"Unknown session method {session.method!r}: expected one of "
                + ", ".join(SESSION_METHODS) + "."
            )
        try:
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO sessions ({_SESSION_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                    (session.hash, session.user, session.method, session.expires, session.written),
                )
        except sqlite3.IntegrityError as exc:
            # The user column refers to a user row; a session for no user is
            # a caller's mistake, not a duplicate.
            if "FOREIGN KEY" in str(exc):
                raise ValueError(f"No user {session.user!r} to open a session for.") from exc
            raise Conflict(session.hash) from exc

    def get_session(self, session_hash: str) -> Session | None:
        row = self.connection.execute(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE hash = ?", (session_hash,)
        ).fetchone()
        return _session(row) if row is not None else None

    def update_session(
        self, session_hash: str, *, expires: float, written: float
    ) -> Session | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE sessions SET expires = ?, written = ? WHERE hash = ?",
                (expires, written, session_hash),
            ).rowcount
        return self.get_session(session_hash) if changed else None

    def remove_session(self, session_hash: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "DELETE FROM sessions WHERE hash = ?", (session_hash,)
            ).rowcount
        return bool(changed)

    def remove_user_sessions(self, user: str, *, keep: str | None = None) -> int:
        with self.connection:
            return self.connection.execute(
                "DELETE FROM sessions WHERE user = ? AND hash IS NOT ?", (user, keep)
            ).rowcount

    def remove_expired_sessions(self, now: float) -> int:
        with self.connection:
            return self.connection.execute(
                "DELETE FROM sessions WHERE expires <= ?", (now,)
            ).rowcount

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
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


def _user(row: sqlite3.Row) -> User:
    return User(**{column: row[column] for column in _USER_COLUMNS.split(", ")})


def _token(row: sqlite3.Row) -> Token:
    return Token(**{column: row[column] for column in _TOKEN_COLUMNS.split(", ")})


def _session(row: sqlite3.Row) -> Session:
    return Session(**{column: row[column] for column in _SESSION_COLUMNS.split(", ")})


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}: expected one of " + ", ".join(ROLES) + ".")


def open_store(root: Path) -> Store:
    """The store a daemon root holds, opened for one unit of work."""
    return SqliteStore(Path(root) / STATE_STORE_FILENAME)
