"""The agent pool over HTTP: what a client asks of the daemon that holds the pool.

A pool lives on a daemon, like a product: its members run on the daemon's
host and its leases are rows in the daemon's store. A client reaches it
through the routes named here, with the bearer token of its registered
remote. A refusal comes back as the ``SpecfloError`` the pool raised on the
daemon, so a command prints the pool's own words.

This module is the client's side only. It imports nothing of the pool's own
code and no web framework; the daemon's routes take their paths from here.
"""

from __future__ import annotations

import dataclasses

from urllib.parse import quote

from ..daemon.products import DaemonClient

POOL_PATH = "/api/pool"
# A lease is made by a POST here; the verbs on one lease live below it.
LEASES_PATH = POOL_PATH + "/leases"
# The leases held by the tokens a client presents. The tokens go in a body,
# so the question is a POST: a credential is never part of a URL.
HELD_PATH = LEASES_PATH + "/held"
# What each pool has out, with no word of who holds it.
STATUS_PATH = POOL_PATH + "/status"

# How long a client waits for an answer, in seconds. A grant starts the
# member's process before it answers, which the pool gives most of a minute.
REQUEST_TIMEOUT = 120.0


@dataclasses.dataclass(frozen=True)
class LeaseGrant:
    """A granted request: the lease, the agent to drive, and the token that
    lets its holder, and no one else, drive it."""

    lease_id: str
    agent: str
    # The credential itself: it is stored for the agent verbs, never shown.
    token: str = dataclasses.field(repr=False)


def release_path(lease_id: str) -> str:
    """Where the lease *lease_id* is given back."""
    return f"{LEASES_PATH}/{quote(lease_id, safe='')}/release"


@dataclasses.dataclass(frozen=True)
class HeldLease:
    """A lease as its holder is told of it."""

    lease_id: str
    agent: str
    pool: str
    state: str
    acquired: str
    last_activity: str
    # In seconds.
    idle_limit: int


@dataclasses.dataclass(frozen=True)
class LeaseEnd:
    """How a lease ended. ``held`` says the token presented was its holder's."""

    lease_id: str
    state: str
    held: bool


class RemotePool(DaemonClient):
    """The pool verbs over HTTP to the daemon at ``url``."""

    def request(
        self,
        pool: str,
        *,
        cwd: str,
        idle_limit: int | None = None,
        label: str | None = None,
        wait: int | None = None,
    ) -> LeaseGrant:
        """Ask for a member of *pool*, to run in *cwd* on the daemon's host.

        *idle_limit* is in seconds; without one the pool's default applies.
        *label* is what the pool shows a person as the holder. *wait* is how
        many seconds the request may wait for a full pool; without one it is
        refused at once.
        """
        body: dict = {"pool": pool, "cwd": cwd}
        if idle_limit is not None:
            body["idle_limit"] = idle_limit
        if label is not None:
            body["label"] = label
        if wait is not None:
            body["wait"] = wait
        granted = self._request("POST", LEASES_PATH, json=body)
        return LeaseGrant(
            lease_id=granted["lease_id"], agent=granted["agent"], token=granted["token"]
        )

    def held(self, tokens: list[str]) -> list[HeldLease | None]:
        """The lease each of *tokens* holds, in their order; None for a token
        that holds none. The daemon answers for these tokens and no others."""
        answered = self._request("POST", HELD_PATH, json={"tokens": list(tokens)})
        return [None if lease is None else HeldLease(**lease) for lease in answered]

    def release(self, lease_id: str, *, token: str | None = None) -> LeaseEnd:
        """Give back the lease *lease_id*, as the holder *token* proves.

        A lease that has ended already is reported as it ended, with or
        without a token; an active one is ended for its holder only.
        """
        body = {} if token is None else {"token": token}
        ended = self._request("POST", release_path(lease_id), json=body)
        return LeaseEnd(lease_id=ended["lease_id"], state=ended["state"], held=ended["held"])

    def status(self) -> list[dict]:
        """Each pool's name, size and leases in use."""
        return self._request("GET", STATUS_PATH)
