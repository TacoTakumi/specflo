"""Signing in to GitHub as the forge's GitHub App, with narrow tokens kept in memory.

A GitHub forge names its App's id and the file with the App's private key.
The key signs a short-lived JWT, and the JWT finds the App's installation on
a watched repo. An installation token then does the work on that repo, and
each token the gateway asks for is held to what the gateway needs: write on
issues, read on contents, and the one repo. The App may hold more on the
installation; a token never does.

Tokens, JWTs and the key live only in memory: githubkit keeps tokens in a
memory cache of this App's own, renews each before it runs out, and the
response cache is off, so nothing is written to a file. A fault names the
forge, the repo and the HTTP status, and never a secret; it does not chain
the githubkit fault, whose request still carries the Authorization header.

Every call is async (githubkit's ``async_*`` methods and ``arequest``), as
everything on the daemon's event loop is.

:class:`GitHubClient` is the gateway's :class:`~specflo.gateway.forge.ForgeClient`
for a GitHub forge: the same calls as the Gitea client, each made in a
session of the App on the call's repo. It also reads a repo's issue events
feed for the poll (:meth:`GitHubClient.repo_label_events`), with the ETag of
the feed's last answer, which the client keeps in memory per repo: when the
feed has not changed GitHub answers 304, which costs an authenticated
request no rate limit, and the poll asks that repo nothing more. A restart
starts with no ETag and reads the feed once in full.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from githubkit import AppAuthStrategy, GitHub
from githubkit.cache import MemCacheStrategy
from githubkit.exception import GitHubException, RequestError, RequestFailed, RequestTimeout
from githubkit.response import Response

from ..errors import SpecfloError
from .config import Forge, read_credential
from .forge import Comment, ForgeError, Issue, LabelEvent

#: What each installation token may do, and nothing more: the gateway reads
#: issues and posts on them, and reads the repo's files.
TOKEN_PERMISSIONS: dict[str, str] = {"issues": "write", "contents": "read"}

#: What the token a run clones its repo with may do: read the repo's files.
CLONE_PERMISSIONS: dict[str, str] = {"contents": "read"}

#: How many items the client asks for per page, GitHub's most.
PER_PAGE = 100

#: The issue events of a label put on an issue and taken off it.
LABELED = "labeled"
UNLABELED = "unlabeled"


class GitHubApp:
    """The GitHub App of the forge *forge*, signed in with the key in its key file.

    The key is read once, here, through :func:`read_credential`, so a key file
    with the wrong mode or no key in it is refused before any call. *transport*
    stands in for the network in a test.
    """

    def __init__(self, forge: Forge, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if forge.app_id is None or forge.key_file is None:
            raise SpecfloError(f"forge '{forge.name}': not a GitHub App forge.")
        key = read_credential(forge.key_file)
        _check_key(forge, key)
        self.forge = forge
        self._app = GitHub(
            AppAuthStrategy(app_id=forge.app_id, private_key=key),
            base_url=forge.url,
            async_transport=transport,
            # Tokens stay in a memory cache of this App's own, never the
            # process-wide default, and no response is cached anywhere.
            cache_strategy=MemCacheStrategy(),
            http_cache=False,
        )
        self._installations: dict[str, int] = {}

    @asynccontextmanager
    async def session(self, repo: str) -> AsyncIterator[GitHub]:
        """A client on *repo* with an installation token held to issues and contents.

        The client's connections close when the block ends. A GitHub fault in
        the block, the token request included, comes out as a ForgeError
        that names the forge, the repo and the status and holds no secret.
        """
        if repo not in self.forge.repos:
            raise ForgeError(f"forge '{self.forge.name}': {repo} is not a watched repo.")
        try:
            installation_id = await self._installation_id(repo)
            client = self._app.with_auth(self._app.auth.as_installation(
                installation_id,
                repositories=[repo.split("/", 1)[1]],
                permissions=TOKEN_PERMISSIONS,
            ))
            async with client:
                yield client
        except (GitHubException, httpx.HTTPError) as exc:
            raise ForgeError(
                f"forge '{self.forge.name}': {repo}: {_problem(exc)}", _status(exc)
            ) from None

    async def clone_token(self, repo: str) -> str:
        """A fresh installation token on *repo* that may only read its contents.

        A run's clone is handed this token and no other, so the git process
        holds nothing that could write to the forge. It is not kept here.
        """
        if repo not in self.forge.repos:
            raise ForgeError(f"forge '{self.forge.name}': {repo} is not a watched repo.")
        try:
            installation_id = await self._installation_id(repo)
            response = await self._app.rest.apps.async_create_installation_access_token(
                installation_id,
                repositories=[repo.split("/", 1)[1]],
                permissions=CLONE_PERMISSIONS,
            )
        except (GitHubException, httpx.HTTPError) as exc:
            raise ForgeError(
                f"forge '{self.forge.name}': {repo}: the clone token: {_problem(exc)}",
                _status(exc),
            ) from None
        token = response.json().get("token")
        if not isinstance(token, str) or not token:
            raise ForgeError(f"forge '{self.forge.name}': {repo}: GitHub gave no clone token.")
        return token

    async def _installation_id(self, repo: str) -> int:
        """The id of the App's installation on *repo*, asked once and then kept."""
        if repo not in self._installations:
            owner, name = repo.split("/", 1)
            response = await self._app.rest.apps.async_get_repo_installation(owner, name)
            installation_id = response.json().get("id")
            if isinstance(installation_id, bool) or not isinstance(installation_id, int):
                raise ForgeError(
                    f"forge '{self.forge.name}': {repo}: GitHub named no installation of the App."
                )
            self._installations[repo] = installation_id
        return self._installations[repo]


@dataclass(frozen=True)
class RepoEvents:
    """The label events of a repo's issue events feed, back to a time.

    ``events`` holds each open issue's label events, oldest first, by the
    issue's number; ``newest`` is the time of the newest event in the feed,
    of any kind, or None when the feed is empty.
    """

    events: dict[int, list[LabelEvent]]
    newest: datetime | None


class GitHubClient:
    """The GitHub forge *forge*, asked as its GitHub App.

    Each call runs in a session of the App on the call's repo
    (:meth:`GitHubApp.session`), so it carries an installation token held to
    that repo. *transport* replaces the network, for a test's stub forge. The
    sessions close their connections as they end, so :meth:`aclose` has
    nothing to close; it is there for the interface.
    """

    def __init__(self, forge: Forge, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.forge = forge
        self.app = GitHubApp(forge, transport=transport)
        #: The ETag of the last answer of each repo's issue events feed.
        self._etags: dict[str, str] = {}

    async def aclose(self) -> None:
        return None

    async def labelled_issues(
        self, repo: str, label: str, since: datetime | None = None
    ) -> list[Issue]:
        owner, name = _split(repo)
        options: dict[str, Any] = {"labels": label, "state": "open"}
        if since is not None:
            options["since"] = _aware(since)
        async with self.app.session(repo) as gh:
            found = await _pages(
                f"GET /repos/{repo}/issues",
                lambda page: gh.rest.issues.async_list_for_repo(
                    owner, name, per_page=PER_PAGE, page=page, **options
                ),
            )
        # GitHub lists pull requests among the issues.
        return [_issue(repo, data) for data in found if not data.get("pull_request")]

    async def issue(self, repo: str, number: int) -> Issue:
        owner, name = _split(repo)
        async with self.app.session(repo) as gh:
            response = await _ask(
                f"GET /repos/{repo}/issues/{int(number)}",
                gh.rest.issues.async_get(owner, name, int(number)),
            )
        return _issue(repo, response.json())

    async def comments(self, repo: str, number: int) -> list[Comment]:
        owner, name = _split(repo)
        async with self.app.session(repo) as gh:
            found = await _pages(
                f"GET /repos/{repo}/issues/{int(number)}/comments",
                lambda page: gh.rest.issues.async_list_comments(
                    owner, name, int(number), per_page=PER_PAGE, page=page
                ),
            )
        return [_comment(data) for data in found]

    async def label_events(self, repo: str, number: int) -> list[LabelEvent]:
        owner, name = _split(repo)
        async with self.app.session(repo) as gh:
            found = await _pages(
                f"GET /repos/{repo}/issues/{int(number)}/events",
                lambda page: gh.rest.issues.async_list_events(
                    owner, name, int(number), per_page=PER_PAGE, page=page
                ),
            )
        return [_label_event(data) for data in found if _is_label_event(data)]

    async def create_comment(self, repo: str, number: int, body: str) -> Comment:
        owner, name = _split(repo)
        async with self.app.session(repo) as gh:
            response = await _ask(
                f"POST /repos/{repo}/issues/{int(number)}/comments",
                gh.rest.issues.async_create_comment(owner, name, int(number), body=body),
            )
        return _comment(response.json())

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> Comment:
        owner, name = _split(repo)
        async with self.app.session(repo) as gh:
            response = await _ask(
                f"PATCH /repos/{repo}/issues/comments/{int(comment_id)}",
                gh.rest.issues.async_update_comment(owner, name, int(comment_id), body=body),
            )
        return _comment(response.json())

    async def find_comment(self, repo: str, comment_id: int) -> Comment | None:
        owner, name = _split(repo)
        try:
            async with self.app.session(repo) as gh:
                response = await _ask(
                    f"GET /repos/{repo}/issues/comments/{int(comment_id)}",
                    gh.rest.issues.async_get_comment(owner, name, int(comment_id)),
                )
        except ForgeError as exc:
            if exc.status == 404:
                return None
            raise
        return _comment(response.json())

    def clone_url(self, repo: str) -> str:
        """The web host's URL of *repo*: ``github.com`` for the public API, and
        the host with the ``/api/v3`` path taken off for GitHub Enterprise."""
        parts = urlsplit(self.forge.url)
        host = parts.netloc.rpartition("@")[2]
        if host.startswith("api."):
            host = host[len("api."):]
        path = parts.path.rstrip("/")
        if path.endswith("/api/v3"):
            path = path[: -len("/api/v3")]
        owner, name = _split(repo)
        return urlunsplit((parts.scheme, host, f"{path}/{owner}/{name}.git", "", ""))

    async def clone_token(self, repo: str) -> str:
        """A token to clone *repo* with, which may only read its contents."""
        return await self.app.clone_token(repo)

    async def repo_label_events(self, repo: str, since: datetime) -> RepoEvents | None:
        """The label events of *repo*'s issue events feed at or after *since*, or
        None when the feed has not changed since this client last read it.

        The feed is newest first. The first page is asked with the ETag of the
        last answer, and the pages after it are read until one reaches back
        past *since* or there is none. The ETag is kept only once every page
        was read, so a read that fails is made again in full.
        """
        owner, name = _split(repo)
        since = _aware(since)
        what = f"GET /repos/{repo}/issues/events"
        found: list[dict] = []
        async with self.app.session(repo) as gh:
            page = 1
            etag = self._etags.get(repo)
            first = await _ask(what, gh.rest.issues.async_list_events_for_repo(
                owner, name, per_page=PER_PAGE, page=page,
                headers={"If-None-Match": etag} if etag else None,
            ))
            if first.status_code == 304:
                return None
            response = first
            while True:
                items = _items(response, what)
                found += items
                reached = any(_time(data["created_at"]) < since for data in items)
                if not items or reached or "next" not in response.raw_response.links:
                    break
                page += 1
                response = await _ask(what, gh.rest.issues.async_list_events_for_repo(
                    owner, name, per_page=PER_PAGE, page=page,
                ))
        if first.headers.get("ETag"):
            self._etags[repo] = first.headers["ETag"]
        events: dict[int, list[tuple[datetime, int, LabelEvent]]] = {}
        for data in found:
            issue = data.get("issue") or {}
            if not _is_label_event(data) or _time(data["created_at"]) < since:
                continue
            if issue.get("pull_request") or issue.get("state", "open") != "open":
                continue
            event = _label_event(data)
            events.setdefault(int(issue["number"]), []).append((event.at, int(data["id"]), event))
        newest = max((_time(data["created_at"]) for data in found), default=None)
        return RepoEvents(
            events={
                number: [event for _, _, event in sorted(entries, key=lambda e: e[:2])]
                for number, entries in events.items()
            },
            newest=newest,
        )


async def _ask(what: str, pending: Awaitable[Response]) -> Response:
    """The answer to the call *what*; a call GitHub refuses is a ForgeError
    that names it and the status. Other faults are the session's to name."""
    try:
        return await pending
    except RequestFailed as exc:
        status = exc.response.status_code
        raise ForgeError(f"GitHub {what}: HTTP {status}{_message(exc.response)}", status) from None


async def _pages(what: str, ask: Any) -> list[dict]:
    """Every item of the list call *what*, asking ``ask(page)`` while GitHub's
    ``Link`` header names a next page."""
    found: list[dict] = []
    page = 1
    while True:
        response = await _ask(what, ask(page))
        items = _items(response, what)
        found += items
        if not items or "next" not in response.raw_response.links:
            return found
        page += 1


def _items(response: Response, what: str) -> list[dict]:
    try:
        items = response.json()
    except ValueError:
        raise ForgeError(f"GitHub {what}: the answer is not JSON.") from None
    if not isinstance(items, list):
        raise ForgeError(f"GitHub {what}: the answer is not a list.")
    return items


def _message(response: Response) -> str:
    """GitHub's own word on a fault, when it gave one."""
    try:
        data = response.json()
    except ValueError:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return f" ({message})" if isinstance(message, str) and message else ""


def _split(repo: str) -> tuple[str, str]:
    owner, _, name = repo.partition("/")
    return owner, name


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time sent to the forge must carry its zone.")
    return value.astimezone(UTC)


def _time(value: str) -> datetime:
    """A time as GitHub writes it, in UTC."""
    return datetime.fromisoformat(value).astimezone(UTC)


def _user_id(user: object) -> int:
    # A deleted account shows as no user at all.
    return int(user.get("id", 0)) if isinstance(user, dict) else 0


def _issue(repo: str, data: dict) -> Issue:
    return Issue(
        repo=repo,
        number=int(data["number"]),
        title=data.get("title") or "",
        body=data.get("body") or "",
        author_id=_user_id(data.get("user")),
        labels=tuple(
            label["name"] if isinstance(label, dict) else str(label)
            for label in data.get("labels") or ()
        ),
        updated_at=_time(data["updated_at"]),
        url=data.get("html_url") or "",
    )


def _comment(data: dict) -> Comment:
    return Comment(
        id=int(data["id"]),
        author_id=_user_id(data.get("user")),
        body=data.get("body") or "",
        created_at=_time(data["created_at"]),
        updated_at=_time(data["updated_at"]),
        url=data.get("html_url") or "",
    )


def _is_label_event(data: dict) -> bool:
    return data.get("event") in (LABELED, UNLABELED) and isinstance(data.get("label"), dict)


def _label_event(data: dict) -> LabelEvent:
    return LabelEvent(
        label=data["label"]["name"],
        actor_id=_user_id(data.get("actor")),
        at=_time(data["created_at"]),
        added=data["event"] == LABELED,
    )


def _check_key(forge: Forge, key: str) -> None:
    """Refuse a key file that holds no RSA private key, without quoting it."""
    try:
        loaded = load_pem_private_key(key.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        loaded = None
    if not isinstance(loaded, rsa.RSAPrivateKey):
        raise SpecfloError(f"{forge.key_file}: not a GitHub App's PEM private key.")


def _status(exc: Exception) -> int | None:
    """The HTTP status GitHub answered the failed call with, or None when it did not answer."""
    while isinstance(exc, RequestError) and isinstance(exc.exc, GitHubException):
        exc = exc.exc
    return exc.response.status_code if isinstance(exc, RequestFailed) else None


def _problem(exc: Exception) -> str:
    """What went wrong with a GitHub call, in words that hold no secret."""
    # A refused token request fails inside the auth flow, and githubkit
    # wraps that fault in another.
    while isinstance(exc, RequestError) and isinstance(exc.exc, GitHubException):
        exc = exc.exc
    if isinstance(exc, RequestFailed):
        return f"GitHub answered HTTP {exc.response.status_code}."
    if isinstance(exc, RequestTimeout):
        return "GitHub did not answer in time."
    if isinstance(exc, RequestError):
        return f"cannot reach GitHub ({type(exc.exc).__name__})."
    return f"the GitHub call failed ({type(exc).__name__})."
