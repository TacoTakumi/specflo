"""The agent pool over HTTP: what a client asks of the daemon that holds the pool.

A pool lives on a daemon, like a product: its members run on the daemon's
host and its leases are rows in the daemon's store. A client reaches it
through the routes named here, with the bearer token of its registered
remote. A refusal comes back as the ``SpecfloError`` the pool raised on the
daemon, so a command prints the pool's own words.

A request that has to wait is answered at the end of the wait. A client that
wants to know at once that it waits says that it reads notices; the answer to
a request that waits is then a JSON object to a line: one that says it waits,
and last the result or the refusal. Every other answer is the one object it
always was, under the status it always had.

This module is the client's side only. It imports nothing of the pool's own
code and no web framework; the daemon's routes take their paths from here.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

from urllib.parse import quote

import httpx

from ..daemon.products import DaemonClient
from .remote import daemon_result

POOL_PATH = "/api/pool"
# A lease is made by a POST here; the verbs on one lease live below it.
LEASES_PATH = POOL_PATH + "/leases"
# The leases held by the tokens a client presents. The tokens go in a body,
# so the question is a POST: a credential is never part of a URL.
HELD_PATH = LEASES_PATH + "/held"
# What each pool has out, with no word of who holds it.
STATUS_PATH = POOL_PATH + "/status"

# What a client that reads notices accepts, and what the answer to a request
# that waits is then sent as: a JSON object to a line. The first says
# ``waiting``; the last says ``result``, or ``refused`` with the status and the
# detail the refusal has when nothing was sent before it.
WAITING_MEDIA_TYPE = "application/x-ndjson"

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


@dataclasses.dataclass(frozen=True)
class WaitNotice:
    """What a request that has to wait is told at once."""

    pool: str
    # What is full, in the pool's own words.
    full: str
    # Among the requests that wait for the pool, in arrival order; 1 is the next.
    place: int
    # The longest the request waits, in seconds.
    wait: int


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
        egress: str | None = None,
        project: str | None = None,
        on_waiting: Callable[[WaitNotice], None] | None = None,
    ) -> LeaseGrant:
        """Ask for a member of *pool*, to run in *cwd* on the daemon's host.

        *idle_limit* is in seconds; without one the pool's default applies.
        *label* is what the pool shows a person as the holder. *wait* is how
        many seconds the request may wait for a full pool; without one it is
        refused at once. *egress* is the most open egress class of member the
        request takes; without one the pool's default applies. *project* is
        the slug of the project on this daemon the request is made from; the
        daemon holds the request to the egress class its own record of that
        project pins, which the request cannot say. *on_waiting* is called
        once, as soon as the daemon says the request has to wait; with none
        the daemon is not asked to say so.
        """
        body: dict = {"pool": pool, "cwd": cwd}
        if idle_limit is not None:
            body["idle_limit"] = idle_limit
        if label is not None:
            body["label"] = label
        if wait is not None:
            body["wait"] = wait
        if egress is not None:
            body["egress"] = egress
        if project is not None:
            body["project"] = project
        if on_waiting is None:
            granted = self._request("POST", LEASES_PATH, json=body)
        else:
            granted = self._request_told(body, on_waiting)
        return LeaseGrant(
            lease_id=granted["lease_id"], agent=granted["agent"], token=granted["token"]
        )

    def _request_told(self, body: dict, on_waiting: Callable[[WaitNotice], None]):
        """The result of the lease request *body*, from a daemon asked to say
        when the request waits. What ends an answer in lines is mapped as the
        same answer with no line before it, so a refusal reads the same."""

        def send() -> httpx.Response:
            accept = f"{WAITING_MEDIA_TYPE}, application/json"
            with self.client.stream(
                "POST", LEASES_PATH, json=body, headers={"Accept": accept}
            ) as response:
                if WAITING_MEDIA_TYPE not in response.headers.get("content-type", ""):
                    response.read()
                    return response
                for line in response.iter_lines():
                    told = json.loads(line)
                    if "waiting" in told:
                        on_waiting(WaitNotice(**told["waiting"]))
                    elif "refused" in told:
                        refused = told["refused"]
                        return httpx.Response(
                            refused["status"], json={"detail": refused["detail"]}
                        )
                    else:
                        return httpx.Response(200, json=told)
            raise httpx.RemoteProtocolError("the answer ended before its result")

        return daemon_result(self.url, f"POST {LEASES_PATH}", send)

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
