"""The pool's state store: leases and the records around them, SQLite behind one interface.

A lease is a row, like a product or a work item: it belongs to the daemon
rather than to one project, so it lives in the same SQLite file under the
daemon root, in tables of its own beside the ones :mod:`specflo.daemon.store`
keeps. Everything the pool remembers between requests is here and is reached
through the :class:`PoolStore` interface below and nothing else: leases, the
transitions they went through, the requests that wait, what is known of each
provider account, the model reloads seen on members, and which agent host is
attached to which console slot. What is declared rather than remembered
(pools, members, accounts, teams) is the pool configuration's, not the
store's.

The store keeps no clock and makes no decision. Every time is a value the
caller passes in, as ISO 8601 text like the agent host's status file, so a
test drives it with a fake clock; whether a lease has expired, a closed
account has reopened or a waiting request's time is up is for the reader to
compute from what is stored.

Standard library only, and opened per unit of work like the state store: a
SQLite connection belongs to the thread that opened it, and the daemon
answers requests on several.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Protocol, Self

from . import STATE_STORE_FILENAME
from .store import Conflict

LEASE_STATES = ("active", "released", "expired", "preempted")
TRANSITION_KINDS = ("granted", "released", "expired", "preempted")

# The state a lease is in after a transition of each kind; an ending kind
# names its own end state.
_STATE_AFTER = {
    "granted": "active",
    "released": "released",
    "expired": "expired",
    "preempted": "preempted",
}


@dataclasses.dataclass(frozen=True)
class Resource:
    """One thing a lease took: a ``kind`` (pool, member, account, model) and its name."""

    kind: str
    name: str


@dataclasses.dataclass(frozen=True)
class Lease:
    """One lease as the store holds it; the caller mints ``id``.

    ``holder_hash`` is the hash of the token issued at grant, never the token;
    ``holder_label`` is who asked, for a person to read. ``idle_limit`` is in
    seconds. Leases of one team share a ``team_lease_id``.
    """

    id: str
    team_lease_id: str | None
    holder_hash: str
    holder_label: str
    member: str
    pool: str
    resources: tuple[Resource, ...]
    acquired: str
    last_activity: str
    idle_limit: int
    state: str


@dataclasses.dataclass(frozen=True)
class Transition:
    """One thing that happened to a lease; ``id`` is minted by the store."""

    id: int
    lease_id: str
    kind: str
    time: str
    cause: str


@dataclasses.dataclass(frozen=True)
class WaitingRequest:
    """One request that waits, for a pool or for a team; the caller mints ``id``.

    After ``arrived`` comes what the request said of itself that decides
    where it fits, as it was said: ``egress`` is the egress class the request
    named, None when it named none. It is not the request's ceiling, which
    also stands under limits the request does not set and is worked out by
    whoever reads the row. ``pinned`` is one of those limits: the egress class
    the requesting project pinned when the request arrived, None when no
    project stood behind the request or it pinned none. ``until`` is the time
    the request's wait is up, after which its asker waits no more; None is no
    such time. A row from before a value was kept reads as None.
    """

    id: str
    pool: str | None
    team: str | None
    holder_label: str
    arrived: str
    egress: str | None = None
    pinned: str | None = None
    until: str | None = None


@dataclasses.dataclass(frozen=True)
class AccountFigures:
    """What the last read of an account's provider key said, or why it failed."""

    usage: float | None
    limit: float | None
    remaining: float | None
    free_requests: int | None
    read_at: str
    read_error: str | None


@dataclasses.dataclass(frozen=True)
class Account:
    """What the store knows of one provider account; ``figures`` is None until a read."""

    name: str
    closed: bool
    reopen: str | None
    figures: AccountFigures | None


@dataclasses.dataclass(frozen=True)
class Reload:
    """One reload of a leased member's model; ``id`` is minted by the store."""

    id: int
    member: str
    model: str
    time: str


@dataclasses.dataclass(frozen=True)
class ConsoleAttachment:
    """The agent host attached to one console slot; a draining slot takes no new lease."""

    slot: str
    agent: str
    attached: str
    draining: bool = False


class PoolStore(Protocol):
    """What a pool store backend provides; every backend implements all of it."""

    def add_lease(self, lease: Lease) -> None:
        """Insert ``lease``; raises :class:`Conflict` on a taken id."""

    def get_lease(self, lease_id: str) -> Lease | None:
        """The lease called ``lease_id``, or None."""

    def list_leases(
        self,
        *,
        state: str | None = None,
        pool: str | None = None,
        member: str | None = None,
        team_lease_id: str | None = None,
    ) -> list[Lease]:
        """Every lease matching the filters given, ended ones too, in grant order."""

    def set_lease_activity(self, lease_id: str, time: str) -> Lease | None:
        """Replace the last activity time of ``lease_id``; the updated lease, or None if unknown."""

    def record_transition(
        self, transition: Transition, *, expect: str | None = None
    ) -> Transition | None:
        """Append ``transition`` and move its lease to the state its kind names, as one write.

        The record as stored, under a minted id. With ``expect``, only when
        the lease is in that state now, so of two callers ending one lease
        one wins. None, and nothing written, when the lease is unknown or
        not in the expected state; an unknown kind is a ValueError.
        """

    def list_transitions(
        self, *, lease_id: str | None = None, limit: int | None = None
    ) -> list[Transition]:
        """The transitions recorded, oldest first; with ``limit``, only the latest that many."""

    def add_waiting(self, request: WaitingRequest) -> None:
        """Insert ``request`` at the back of the queue; raises :class:`Conflict` on a taken id."""

    def list_waiting(self, *, pool: str | None = None) -> list[WaitingRequest]:
        """The requests that wait, in arrival order."""

    def remove_waiting(self, request_id: str) -> bool:
        """Drop ``request_id`` from the queue; whether it was there."""

    def get_account(self, name: str) -> Account | None:
        """What is known of account ``name``, or None if nothing was ever written."""

    def list_accounts(self) -> list[Account]:
        """Every account something was written about, in name order."""

    def set_account_closed(self, name: str, *, reopen: str | None) -> Account:
        """Record ``name`` as closed until ``reopen``; its figures are left alone."""

    def set_account_open(self, name: str) -> Account:
        """Record ``name`` as open, with no reopen time; its figures are left alone."""

    def set_account_figures(self, name: str, figures: AccountFigures) -> Account:
        """Replace the figures of ``name``; open or closed is left alone."""

    def add_reload(self, reload: Reload) -> Reload:
        """Insert ``reload`` under a minted id, whatever id it carries; the record as stored."""

    def list_reloads(self, *, member: str | None = None) -> list[Reload]:
        """The reloads recorded, of one member or of all, oldest first."""

    def set_reload_data_available(self, available: bool) -> None:
        """Record whether the reader of model events is getting them."""

    def reload_data_available(self) -> bool:
        """Whether reload records are being kept; False until a reader says so."""

    def attach_console(self, attachment: ConsoleAttachment) -> None:
        """Insert ``attachment``; raises :class:`Conflict` on a slot that has one."""

    def get_console(self, slot: str) -> ConsoleAttachment | None:
        """The attachment of ``slot``, or None when it is offline."""

    def list_consoles(self) -> list[ConsoleAttachment]:
        """Every attachment, in slot order."""

    def set_console_draining(self, slot: str, draining: bool = True) -> ConsoleAttachment | None:
        """Mark the attachment of ``slot`` draining or not; the updated one, or None if offline."""

    def detach_console(self, slot: str) -> bool:
        """Drop the attachment of ``slot``; whether there was one."""

    def close(self) -> None:
        """Release the backend's resources."""

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc_info) -> None: ...


# Every table is the pool's own and carries its prefix: opening the pool
# store on a root creates these and touches nothing the state store keeps.
SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_leases (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    id            TEXT NOT NULL UNIQUE,
    team_lease_id TEXT,
    holder_hash   TEXT NOT NULL,
    holder_label  TEXT NOT NULL,
    member        TEXT NOT NULL,
    pool          TEXT NOT NULL,
    resources     TEXT NOT NULL,
    acquired      TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    idle_limit    INTEGER NOT NULL,
    state         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pool_transitions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id TEXT NOT NULL REFERENCES pool_leases(id),
    kind     TEXT NOT NULL,
    time     TEXT NOT NULL,
    cause    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pool_waiting (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT NOT NULL UNIQUE,
    pool         TEXT,
    team         TEXT,
    holder_label TEXT NOT NULL,
    arrived      TEXT NOT NULL,
    egress       TEXT,
    pinned       TEXT,
    until        TEXT
);
CREATE TABLE IF NOT EXISTS pool_accounts (
    name          TEXT PRIMARY KEY,
    closed        INTEGER NOT NULL DEFAULT 0,
    reopen        TEXT,
    usage         REAL,
    spend_limit   REAL,
    remaining     REAL,
    free_requests INTEGER,
    read_at       TEXT,
    read_error    TEXT
);
CREATE TABLE IF NOT EXISTS pool_reloads (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    member TEXT NOT NULL,
    model  TEXT NOT NULL,
    time   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pool_consoles (
    slot     TEXT PRIMARY KEY,
    agent    TEXT NOT NULL,
    attached TEXT NOT NULL,
    draining INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pool_notes (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Columns added after their table's first shape, as (table, column, type):
# CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a store
# opened from before the column gains it here.
ADDED_COLUMNS = (
    ("pool_waiting", "egress", "TEXT"),
    ("pool_waiting", "pinned", "TEXT"),
    ("pool_waiting", "until", "TEXT"),
)

_LEASE_COLUMNS = (
    "id, team_lease_id, holder_hash, holder_label, member, pool,"
    " resources, acquired, last_activity, idle_limit, state"
)
_WAITING_COLUMNS = "id, pool, team, holder_label, arrived, egress, pinned, until"
_ACCOUNT_COLUMNS = (
    "name, closed, reopen, usage, spend_limit, remaining, free_requests, read_at, read_error"
)
_RELOAD_DATA_NOTE = "reload_data_available"


class SqlitePoolStore:
    """The :class:`PoolStore` on a SQLite file; the schema is created on open."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        with self.connection:
            self.connection.executescript(SCHEMA)
            for table, column, kind in ADDED_COLUMNS:
                columns = self.connection.execute(f"PRAGMA table_info({table})")
                if column not in {row["name"] for row in columns}:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def add_lease(self, lease: Lease) -> None:
        resources = json.dumps([[resource.kind, resource.name] for resource in lease.resources])
        try:
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO pool_leases ({_LEASE_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lease.id, lease.team_lease_id, lease.holder_hash, lease.holder_label,
                        lease.member, lease.pool, resources, lease.acquired,
                        lease.last_activity, lease.idle_limit, lease.state,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(lease.id) from exc

    def get_lease(self, lease_id: str) -> Lease | None:
        row = self.connection.execute(
            f"SELECT {_LEASE_COLUMNS} FROM pool_leases WHERE id = ?", (lease_id,)
        ).fetchone()
        return _lease(row) if row is not None else None

    def list_leases(
        self,
        *,
        state: str | None = None,
        pool: str | None = None,
        member: str | None = None,
        team_lease_id: str | None = None,
    ) -> list[Lease]:
        clauses, values = [], []
        for column, value in (
            ("state", state), ("pool", pool), ("member", member), ("team_lease_id", team_lease_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT {_LEASE_COLUMNS} FROM pool_leases{where} ORDER BY seq", values
        ).fetchall()
        return [_lease(row) for row in rows]

    def set_lease_activity(self, lease_id: str, time: str) -> Lease | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE pool_leases SET last_activity = ? WHERE id = ?", (time, lease_id)
            ).rowcount
        return self.get_lease(lease_id) if changed else None

    def record_transition(
        self, transition: Transition, *, expect: str | None = None
    ) -> Transition | None:
        if transition.kind not in _STATE_AFTER:
            raise ValueError(f"unknown transition kind {transition.kind!r}")
        guard, values = "", [_STATE_AFTER[transition.kind], transition.lease_id]
        if expect is not None:
            guard = " AND state = ?"
            values.append(expect)
        # The update comes first: it takes the write lock, so the state it
        # tests is still the state when the record lands.
        with self.connection:
            moved = self.connection.execute(
                f"UPDATE pool_leases SET state = ? WHERE id = ?{guard}", values
            ).rowcount
            if not moved:
                return None
            cursor = self.connection.execute(
                "INSERT INTO pool_transitions (lease_id, kind, time, cause) VALUES (?, ?, ?, ?)",
                (transition.lease_id, transition.kind, transition.time, transition.cause),
            )
        return dataclasses.replace(transition, id=cursor.lastrowid)

    def list_transitions(
        self, *, lease_id: str | None = None, limit: int | None = None
    ) -> list[Transition]:
        where, values = "", []
        if lease_id is not None:
            where = " WHERE lease_id = ?"
            values.append(lease_id)
        tail = ""
        if limit is not None:
            tail = " LIMIT ?"
            values.append(limit)
        rows = self.connection.execute(
            "SELECT id, lease_id, kind, time, cause FROM pool_transitions"
            f"{where} ORDER BY id DESC{tail}",
            values,
        ).fetchall()
        return [
            Transition(
                id=row["id"], lease_id=row["lease_id"], kind=row["kind"],
                time=row["time"], cause=row["cause"],
            )
            for row in reversed(rows)
        ]

    def add_waiting(self, request: WaitingRequest) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO pool_waiting ({_WAITING_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request.id, request.pool, request.team, request.holder_label,
                        request.arrived, request.egress, request.pinned, request.until,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(request.id) from exc

    def list_waiting(self, *, pool: str | None = None) -> list[WaitingRequest]:
        where, values = "", []
        if pool is not None:
            where = " WHERE pool = ?"
            values.append(pool)
        rows = self.connection.execute(
            f"SELECT {_WAITING_COLUMNS} FROM pool_waiting{where} ORDER BY seq", values
        ).fetchall()
        return [
            WaitingRequest(
                id=row["id"], pool=row["pool"], team=row["team"],
                holder_label=row["holder_label"], arrived=row["arrived"], egress=row["egress"],
                pinned=row["pinned"], until=row["until"],
            )
            for row in rows
        ]

    def remove_waiting(self, request_id: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "DELETE FROM pool_waiting WHERE id = ?", (request_id,)
            ).rowcount
        return bool(changed)

    def get_account(self, name: str) -> Account | None:
        row = self.connection.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM pool_accounts WHERE name = ?", (name,)
        ).fetchone()
        return _account(row) if row is not None else None

    def list_accounts(self) -> list[Account]:
        rows = self.connection.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM pool_accounts ORDER BY name"
        ).fetchall()
        return [_account(row) for row in rows]

    def set_account_closed(self, name: str, *, reopen: str | None) -> Account:
        return self._set_account_state(name, 1, reopen)

    def set_account_open(self, name: str) -> Account:
        return self._set_account_state(name, 0, None)

    def _set_account_state(self, name: str, closed: int, reopen: str | None) -> Account:
        with self.connection:
            self.connection.execute(
                "INSERT INTO pool_accounts (name, closed, reopen) VALUES (?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET closed = excluded.closed,"
                " reopen = excluded.reopen",
                (name, closed, reopen),
            )
        return self.get_account(name)

    def set_account_figures(self, name: str, figures: AccountFigures) -> Account:
        with self.connection:
            self.connection.execute(
                "INSERT INTO pool_accounts"
                " (name, usage, spend_limit, remaining, free_requests, read_at, read_error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET usage = excluded.usage,"
                " spend_limit = excluded.spend_limit, remaining = excluded.remaining,"
                " free_requests = excluded.free_requests, read_at = excluded.read_at,"
                " read_error = excluded.read_error",
                (
                    name, figures.usage, figures.limit, figures.remaining,
                    figures.free_requests, figures.read_at, figures.read_error,
                ),
            )
        return self.get_account(name)

    def add_reload(self, reload: Reload) -> Reload:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO pool_reloads (member, model, time) VALUES (?, ?, ?)",
                (reload.member, reload.model, reload.time),
            )
        return dataclasses.replace(reload, id=cursor.lastrowid)

    def list_reloads(self, *, member: str | None = None) -> list[Reload]:
        where, values = "", []
        if member is not None:
            where = " WHERE member = ?"
            values.append(member)
        rows = self.connection.execute(
            f"SELECT id, member, model, time FROM pool_reloads{where} ORDER BY id", values
        ).fetchall()
        return [
            Reload(id=row["id"], member=row["member"], model=row["model"], time=row["time"])
            for row in rows
        ]

    def set_reload_data_available(self, available: bool) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO pool_notes (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_RELOAD_DATA_NOTE, "1" if available else "0"),
            )

    def reload_data_available(self) -> bool:
        row = self.connection.execute(
            "SELECT value FROM pool_notes WHERE key = ?", (_RELOAD_DATA_NOTE,)
        ).fetchone()
        return row is not None and row["value"] == "1"

    def attach_console(self, attachment: ConsoleAttachment) -> None:
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO pool_consoles (slot, agent, attached, draining)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        attachment.slot, attachment.agent,
                        attachment.attached, int(attachment.draining),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(attachment.slot) from exc

    def get_console(self, slot: str) -> ConsoleAttachment | None:
        row = self.connection.execute(
            "SELECT slot, agent, attached, draining FROM pool_consoles WHERE slot = ?", (slot,)
        ).fetchone()
        return _console(row) if row is not None else None

    def list_consoles(self) -> list[ConsoleAttachment]:
        rows = self.connection.execute(
            "SELECT slot, agent, attached, draining FROM pool_consoles ORDER BY slot"
        ).fetchall()
        return [_console(row) for row in rows]

    def set_console_draining(self, slot: str, draining: bool = True) -> ConsoleAttachment | None:
        with self.connection:
            changed = self.connection.execute(
                "UPDATE pool_consoles SET draining = ? WHERE slot = ?", (int(draining), slot)
            ).rowcount
        return self.get_console(slot) if changed else None

    def detach_console(self, slot: str) -> bool:
        with self.connection:
            changed = self.connection.execute(
                "DELETE FROM pool_consoles WHERE slot = ?", (slot,)
            ).rowcount
        return bool(changed)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _lease(row: sqlite3.Row) -> Lease:
    return Lease(
        id=row["id"],
        team_lease_id=row["team_lease_id"],
        holder_hash=row["holder_hash"],
        holder_label=row["holder_label"],
        member=row["member"],
        pool=row["pool"],
        resources=tuple(Resource(kind, name) for kind, name in json.loads(row["resources"])),
        acquired=row["acquired"],
        last_activity=row["last_activity"],
        idle_limit=row["idle_limit"],
        state=row["state"],
    )


def _account(row: sqlite3.Row) -> Account:
    figures = None
    if row["read_at"] is not None:
        figures = AccountFigures(
            usage=row["usage"],
            limit=row["spend_limit"],
            remaining=row["remaining"],
            free_requests=row["free_requests"],
            read_at=row["read_at"],
            read_error=row["read_error"],
        )
    return Account(
        name=row["name"], closed=bool(row["closed"]), reopen=row["reopen"], figures=figures
    )


def _console(row: sqlite3.Row) -> ConsoleAttachment:
    return ConsoleAttachment(
        slot=row["slot"], agent=row["agent"],
        attached=row["attached"], draining=bool(row["draining"]),
    )


def open_pool_store(root: Path) -> PoolStore:
    """The pool store a daemon root holds, opened for one unit of work."""
    return SqlitePoolStore(Path(root) / STATE_STORE_FILENAME)
