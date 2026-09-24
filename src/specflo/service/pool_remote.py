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

A request may name a team instead of a pool. Its grant is the team lease id
and, for each role member, an ordinary lease with its agent and its token.
The team lease id is given back where a lease is, and that ends every member
lease.

A developer's console is a slot of the pool that serves only while an agent
host that runs on the daemon's host is attached to it. The attach and the
detach are asked of the daemon here, and the daemon serves them to the
developer identity alone.

The pool's configuration is the pool directory on the daemon's host, which
an admin edits by hand. The daemon is asked here to read it again, and serves
that to the developer identity alone; a directory that does not stand is
refused with every fault in it, and the daemon serves on as it did.

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
from .remote import daemon_result, malformed

POOL_PATH = "/api/pool"
# A lease is made by a POST here; the verbs on one lease live below it.
LEASES_PATH = POOL_PATH + "/leases"
# The leases held by the tokens a client presents. The tokens go in a body,
# so the question is a POST: a credential is never part of a URL.
HELD_PATH = LEASES_PATH + "/held"
# What each pool has out, with no word of who holds it.
STATUS_PATH = POOL_PATH + "/status"
# A console slot is attached and detached here. The slot and the agent go in
# the body, so neither path names one.
CONSOLE_ATTACH_PATH = POOL_PATH + "/consoles/attach"
CONSOLE_DETACH_PATH = POOL_PATH + "/consoles/detach"
# The daemon reads its pool directory again here. The directory is the
# daemon's own, so the request names nothing.
RELOAD_PATH = POOL_PATH + "/reload"

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
class TeamMember:
    """One role member of a granted team: an ordinary lease on a member of
    the role's pool, the agent to drive and its token."""

    role: str
    pool: str
    lease_id: str
    agent: str
    # The credential itself: it is stored for the agent verbs, never shown.
    token: str = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class TeamLeaseGrant:
    """A granted team request: the one id the team is given back under, and
    its members in the order of the team's roles."""

    team_lease_id: str
    members: tuple[TeamMember, ...]


@dataclasses.dataclass(frozen=True)
class WaitNotice:
    """What a request that has to wait is told at once. It names the pool the
    request asked for, or the team."""

    # What is full, in the pool's own words.
    full: str
    # Among the requests that wait for the pool, in arrival order; 1 is the
    # next. A team's request stands among all that wait.
    place: int
    # The longest the request waits, in seconds.
    wait: int
    pool: str | None = None
    team: str | None = None


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
    # The id the lease is given back under when it is one member of a team.
    team_lease_id: str | None = None


@dataclasses.dataclass(frozen=True)
class LeaseEnd:
    """How a lease ended. ``held`` says the token presented was its holder's."""

    lease_id: str
    state: str
    held: bool


def _record(kind, fields):
    """A *kind* record built from the keys of *fields* it knows. A key of a
    later release is let go; a missing one, or fields that are no object,
    raise ``TypeError``."""
    if not isinstance(fields, dict):
        raise TypeError(f"{kind.__name__} is not an object")
    known = {field.name for field in dataclasses.fields(kind)}
    return kind(**{key: value for key, value in fields.items() if key in known})


def _team_grant(granted) -> TeamLeaseGrant:
    if not isinstance(granted, dict):
        raise TypeError("the team grant is not an object")
    members = granted["members"]
    if not isinstance(members, list):
        raise TypeError("the team's members are not a list")
    return TeamLeaseGrant(
        team_lease_id=granted["team_lease_id"],
        members=tuple(_record(TeamMember, member) for member in members),
    )


def _held(answered) -> list[HeldLease | None]:
    if not isinstance(answered, list):
        raise TypeError("the held leases are not a list")
    return [None if lease is None else _record(HeldLease, lease) for lease in answered]


def _keys(*names: str):
    """A decode that takes an answer object with each of *names*, and only
    those keys."""

    def decode(answer) -> dict:
        if not isinstance(answer, dict):
            raise TypeError("the answer is not an object")
        return {name: answer[name] for name in names}

    return decode


class RemotePool(DaemonClient):
    """The pool verbs over HTTP to the daemon at ``url``."""

    def _answer(self, method: str, path: str, decode, **kwargs):
        """The daemon's result for *method* on *path*, built by *decode*."""
        return daemon_result(
            self.url,
            f"{method} {path}",
            lambda: self.client.request(method, path, **kwargs),
            decode,
        )

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
        return self._ask(
            {"pool": pool}, lambda granted: _record(LeaseGrant, granted), cwd=cwd,
            idle_limit=idle_limit, label=label, wait=wait, egress=egress, project=project,
            on_waiting=on_waiting,
        )

    def request_team(
        self,
        team: str,
        *,
        cwd: str,
        idle_limit: int | None = None,
        label: str | None = None,
        wait: int | None = None,
        egress: str | None = None,
        project: str | None = None,
        on_waiting: Callable[[WaitNotice], None] | None = None,
    ) -> TeamLeaseGrant:
        """Ask for every role member of *team*, all or nothing, each to run in
        *cwd* on the daemon's host. The rest is what ``request`` takes; the
        team waits as one request and holds nothing while it does."""
        return self._ask(
            {"team": team}, _team_grant, cwd=cwd, idle_limit=idle_limit, label=label,
            wait=wait, egress=egress, project=project, on_waiting=on_waiting,
        )

    def _ask(
        self,
        named: dict,
        decode,
        *,
        cwd: str,
        idle_limit: int | None,
        label: str | None,
        wait: int | None,
        egress: str | None,
        project: str | None,
        on_waiting: Callable[[WaitNotice], None] | None,
    ) -> dict:
        """The daemon's answer to a lease request for what *named* names, a
        pool or a team, built by *decode*."""
        body: dict = {**named, "cwd": cwd}
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
            return self._answer("POST", LEASES_PATH, decode, json=body)
        return self._request_told(body, decode, on_waiting)

    def _request_told(self, body: dict, decode, on_waiting: Callable[[WaitNotice], None]):
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
                    try:
                        told = json.loads(line)
                        if not isinstance(told, dict):
                            raise TypeError("the line is not an object")
                        if "waiting" in told:
                            notice = _record(WaitNotice, told["waiting"])
                        elif "refused" in told:
                            refused = told["refused"]
                            return httpx.Response(
                                refused["status"], json={"detail": refused["detail"]}
                            )
                        else:
                            return httpx.Response(200, json=told)
                    except (ValueError, KeyError, TypeError) as exc:
                        raise malformed(self.url, asked, line) from exc
                    on_waiting(notice)
            raise httpx.RemoteProtocolError("the answer ended before its result")

        asked = f"POST {LEASES_PATH}"
        return daemon_result(self.url, asked, send, decode)

    def held(self, tokens: list[str]) -> list[HeldLease | None]:
        """The lease each of *tokens* holds, in their order; None for a token
        that holds none. The daemon answers for these tokens and no others."""
        return self._answer("POST", HELD_PATH, _held, json={"tokens": list(tokens)})

    def release(self, lease_id: str, *, token: str | None = None) -> LeaseEnd:
        """Give back the lease *lease_id*, as the holder *token* proves.

        A lease that has ended already is reported as it ended, with or
        without a token; an active one is ended for its holder only. A team
        lease id gives back every member lease of the team, as the token of
        any one of them proves; one member lease of a team is refused.
        """
        body = {} if token is None else {"token": token}
        return self._answer(
            "POST", release_path(lease_id), lambda ended: _record(LeaseEnd, ended), json=body
        )

    def status(self) -> list[dict]:
        """Each pool's name, size and leases in use."""
        return self._request("GET", STATUS_PATH)

    def console_attach(self, slot: str, agent: str) -> dict:
        """Attach the agent host *agent*, which runs on the daemon's host, to
        the console *slot*; the slot, the agent and the slot's state."""
        return self._answer(
            "POST", CONSOLE_ATTACH_PATH, _keys("slot", "agent", "state"),
            json={"slot": slot, "agent": agent},
        )

    def console_detach(self, slot: str) -> dict:
        """Detach the console *slot*, which then takes no new lease; the slot
        and its state, draining while a lease is out on it."""
        return self._answer(
            "POST", CONSOLE_DETACH_PATH, _keys("slot", "state"), json={"slot": slot}
        )

    def reload(self) -> dict:
        """Ask the daemon to read its pool directory again and put it in
        force: the daemon's process id, the directory it read, and how many
        definitions, accounts, members, pools and teams stand now."""
        return self._answer(
            "POST", RELOAD_PATH,
            _keys("pid", "directory", "definitions", "accounts", "members", "pools", "teams"),
            json={},
        )
