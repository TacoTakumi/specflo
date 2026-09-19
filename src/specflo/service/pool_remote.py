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

from ..daemon.products import DaemonClient

POOL_PATH = "/api/pool"
# A lease is made by a POST here; the verbs on one lease live below it.
LEASES_PATH = POOL_PATH + "/leases"

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


class RemotePool(DaemonClient):
    """The pool verbs over HTTP to the daemon at ``url``."""

    def request(
        self,
        pool: str,
        *,
        cwd: str,
        idle_limit: int | None = None,
        label: str | None = None,
    ) -> LeaseGrant:
        """Ask for a member of *pool*, to run in *cwd* on the daemon's host.

        *idle_limit* is in seconds; without one the pool's default applies.
        *label* is what the pool shows a person as the holder.
        """
        body: dict = {"pool": pool, "cwd": cwd}
        if idle_limit is not None:
            body["idle_limit"] = idle_limit
        if label is not None:
            body["label"] = label
        granted = self._request("POST", LEASES_PATH, json=body)
        return LeaseGrant(
            lease_id=granted["lease_id"], agent=granted["agent"], token=granted["token"]
        )
