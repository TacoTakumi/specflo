"""The Gitea client: the gateway's calls to a Gitea forge, made as the bot.

Every call goes to Gitea's v1 API under the forge's base URL through httpx's
AsyncClient, with the bot account's token in the ``Authorization`` header.
The token is read from the forge's token file at each request, through
:func:`specflo.gateway.config.read_credential`, so a token the operator
rotates is used at the next call and a file opened up to others is refused
before anything is sent. The client never holds the token in an attribute,
and no fault names it: a failed call is a :class:`ForgeError` with the
method, the API path and the status. Redirects are not followed, so the
token goes to the configured forge only.

Gitea pages its lists. The client asks for :data:`PAGE_LIMIT` at a time and
reads pages until one comes back empty or ``X-Total-Count`` says it has all,
since a forge may hand out fewer per page than asked.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..errors import SpecfloError
from .config import Forge, read_credential
from .forge import Comment, ForgeError, Issue, LabelEvent

#: The API's path under the forge's base URL.
API_PATH = "/api/v1"

#: How many items the client asks for per page.
PAGE_LIMIT = 50

#: Seconds before a call that gets no answer fails.
TIMEOUT = 30.0

#: The timeline entry type of a label put on or taken off an issue.
LABEL_EVENT = "label"

__all__ = ["ForgeError", "GiteaClient", "parse_time"]


class _TokenAuth(httpx.Auth):
    """Puts the bot token from *token_file* on each request, read afresh."""

    def __init__(self, token_file: Path) -> None:
        self.token_file = token_file

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"token {read_credential(self.token_file)}"
        yield request


class GiteaClient:
    """The Gitea forge *forge*, asked with its bot token.

    *transport* replaces the network, for a test's stub forge. Close the
    client with :meth:`aclose`, or use it as an async context manager.
    """

    def __init__(self, forge: Forge, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if forge.token_file is None:
            raise ForgeError(f"forge '{forge.name}': no token_file for the bot's token.")
        self.forge = forge
        self._http = httpx.AsyncClient(
            base_url=forge.url + API_PATH,
            auth=_TokenAuth(forge.token_file),
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def labelled_issues(
        self, repo: str, label: str, since: datetime | None = None
    ) -> list[Issue]:
        params = {"labels": label, "type": "issues", "state": "open"}
        if since is not None:
            params["since"] = format_time(since)
        found = await self._pages(f"/repos/{_repo(repo)}/issues", params)
        return [_issue(repo, data) for data in found]

    async def issue(self, repo: str, number: int) -> Issue:
        return _issue(repo, await self._call("GET", f"/repos/{_repo(repo)}/issues/{int(number)}"))

    async def comments(self, repo: str, number: int) -> list[Comment]:
        # Gitea gives an issue's comments in one list, oldest first.
        found = await self._call("GET", f"/repos/{_repo(repo)}/issues/{int(number)}/comments")
        return [_comment(data) for data in found]

    async def label_events(self, repo: str, number: int) -> list[LabelEvent]:
        found = await self._pages(f"/repos/{_repo(repo)}/issues/{int(number)}/timeline", {})
        return [
            _label_event(data) for data in found
            if data.get("type") == LABEL_EVENT and data.get("label")
        ]

    async def create_comment(self, repo: str, number: int, body: str) -> Comment:
        path = f"/repos/{_repo(repo)}/issues/{int(number)}/comments"
        return _comment(await self._call("POST", path, json={"body": body}))

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> Comment:
        path = f"/repos/{_repo(repo)}/issues/comments/{int(comment_id)}"
        return _comment(await self._call("PATCH", path, json={"body": body}))

    async def find_comment(self, repo: str, comment_id: int) -> Comment | None:
        path = f"/repos/{_repo(repo)}/issues/comments/{int(comment_id)}"
        response = await self._send("GET", path)
        # Gitea answers 204 for an id that is a timeline event, not a comment.
        if response.status_code in (204, 404):
            return None
        return _comment(_json(response, "GET", path))

    def clone_url(self, repo: str) -> str:
        parts = urlsplit(self.forge.url)
        host = parts.netloc.rpartition("@")[2]
        path = f"{parts.path.rstrip('/')}/{_repo(repo)}.git"
        return urlunsplit((parts.scheme, host, path, "", ""))

    async def _pages(self, path: str, params: dict[str, str]) -> list[dict]:
        found: list[dict] = []
        page = 1
        while True:
            response = await self._send(
                "GET", path, params={**params, "page": str(page), "limit": str(PAGE_LIMIT)}
            )
            items = _json(response, "GET", path)
            if not isinstance(items, list):
                raise ForgeError(f"Gitea GET {path}: the answer is not a list.")
            found += items
            total = response.headers.get("X-Total-Count", "")
            if not items or (total.isdigit() and len(found) >= int(total)):
                return found
            page += 1

    async def _call(self, method: str, path: str, **kwargs: object) -> object:
        return _json(await self._send(method, path, **kwargs), method, path)

    async def _send(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            return await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # The message of an httpx fault names the URL, which holds no
            # credential; the token is only ever in a header.
            raise ForgeError(f"Gitea {method} {path}: {type(exc).__name__}: {exc}") from None
        # The token file's faults, raised in the auth flow before anything is
        # sent. read_credential's own names the file and never its content.
        except SpecfloError as exc:
            raise ForgeError(f"Gitea {method} {path}: the bot token: {exc}") from None
        except UnicodeDecodeError:
            raise ForgeError(
                f"Gitea {method} {path}: the bot token: {self.forge.token_file}: not UTF-8."
            ) from None
        except OSError as exc:
            raise ForgeError(
                f"Gitea {method} {path}: the bot token: {self.forge.token_file}: "
                f"cannot be read ({exc.strerror})."
            ) from None


def _json(response: httpx.Response, method: str, path: str) -> object:
    if not response.is_success:
        raise ForgeError(
            f"Gitea {method} {path}: HTTP {response.status_code}{_message(response)}",
            response.status_code,
        )
    try:
        return response.json()
    except ValueError:
        raise ForgeError(f"Gitea {method} {path}: the answer is not JSON.") from None


def _message(response: httpx.Response) -> str:
    """Gitea's own word on a fault, when it gave one."""
    try:
        data = response.json()
    except ValueError:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return f" ({message})" if isinstance(message, str) and message else ""


def _repo(repo: str) -> str:
    owner, _, name = repo.partition("/")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def format_time(value: datetime) -> str:
    """*value* as the RFC 3339 time Gitea reads, in UTC. A naive time is refused."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a time sent to the forge must carry its zone.")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    """A time as Gitea writes it, in UTC."""
    return datetime.fromisoformat(value).astimezone(UTC)


def _user_id(data: dict) -> int:
    user = data.get("user") or {}
    return int(user.get("id", 0))


def _issue(repo: str, data: dict) -> Issue:
    return Issue(
        repo=repo,
        number=int(data["number"]),
        title=data.get("title") or "",
        body=data.get("body") or "",
        author_id=_user_id(data),
        labels=tuple(label["name"] for label in data.get("labels") or ()),
        updated_at=parse_time(data["updated_at"]),
        url=data.get("html_url") or "",
    )


def _comment(data: dict) -> Comment:
    return Comment(
        id=int(data["id"]),
        author_id=_user_id(data),
        body=data.get("body") or "",
        created_at=parse_time(data["created_at"]),
        updated_at=parse_time(data["updated_at"]),
        url=data.get("html_url") or "",
    )


def _label_event(data: dict) -> LabelEvent:
    # Gitea writes "1" in a label event's body when the label went on, and
    # nothing when it came off.
    return LabelEvent(
        label=data["label"]["name"],
        actor_id=_user_id(data),
        at=parse_time(data["created_at"]),
        added=data.get("body") == "1",
    )
