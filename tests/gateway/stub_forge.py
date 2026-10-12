"""Stub forges for the gateway's tests: an in-memory forge behind an httpx transport.

A stub holds repos, issues, comments and label events in memory and answers
the forge's HTTP API from them, through :class:`httpx.MockTransport`, so no
socket is opened and nothing is left running. It records every request it
gets, with its headers, so a test can check what the gateway sent; and it
refuses a request without the token it was made with, as the forge would.

A test seeds a stub through the methods :class:`StubForge` declares, which
say what happened on the forge (an issue was opened, labelled, commented on)
and not how one forge's API spells it, so a test of the comment flow can run
against each kind of stub. :class:`StubGitea` answers Gitea's v1 API and
:class:`StubGitHub` GitHub's REST API as a GitHub App's installation sees it.

The stub pages its lists ``max_items`` at a time, fewer than a real forge
does, so a client that reads only the first page is caught.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

#: When a stub's clock starts.
EPOCH = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Seen:
    """One request a stub received."""

    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    body: object = None


@dataclass
class _Issue:
    repo: str
    number: int
    title: str
    body: str
    author_id: int
    labels: list[str]
    created_at: datetime
    updated_at: datetime
    state: str = "open"
    pull: bool = False


@dataclass
class _Comment:
    id: int
    repo: str
    number: int
    author_id: int
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass
class _LabelEvent:
    id: int
    repo: str
    number: int
    label: str
    actor_id: int
    at: datetime
    added: bool


@dataclass
class StubForge:
    """What every stub forge holds and how a test seeds it.

    ``bot_id`` is the user the token stands for: what the gateway posts is
    posted as that user. ``now`` is the stub's clock, the time of whatever
    happens next; :meth:`tick` moves it on.
    """

    token: str
    bot_id: int = 1000
    max_items: int = 2
    now: datetime = EPOCH
    requests: list[Seen] = field(default_factory=list)
    repos: set[str] = field(default_factory=set)
    issues: dict[tuple[str, int], _Issue] = field(default_factory=dict)
    comments: dict[int, _Comment] = field(default_factory=dict)
    label_events: list[_LabelEvent] = field(default_factory=list)
    _next_id: int = 500

    def transport(self) -> httpx.MockTransport:
        """A transport that hands each request to this stub."""
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        raise NotImplementedError

    def record(self, request: httpx.Request) -> Seen:
        body = json.loads(request.content) if request.content else None
        seen = Seen(
            method=request.method,
            path=request.url.path,
            params=dict(request.url.params),
            headers=dict(request.headers),
            body=body,
        )
        self.requests.append(seen)
        return seen

    # Seeding: what happened on the forge.

    def tick(self, seconds: float = 60) -> datetime:
        self.now = self.now + timedelta(seconds=seconds)
        return self.now

    def add_issue(
        self, repo: str, number: int, *, title: str = "An issue", body: str = "",
        author_id: int = 7, labels: tuple[str, ...] = (), state: str = "open",
        pull: bool = False,
    ) -> None:
        """Open issue *number* in *repo*, with *labels* on it from the start."""
        self.repos.add(repo)
        self.issues[(repo, number)] = _Issue(
            repo, number, title, body, author_id, list(labels), self.now, self.now,
            state=state, pull=pull,
        )

    def label(self, repo: str, number: int, label: str, actor_id: int) -> None:
        """*actor_id* puts *label* on the issue, now."""
        self._label_event(repo, number, label, actor_id, added=True)

    def unlabel(self, repo: str, number: int, label: str, actor_id: int) -> None:
        """*actor_id* takes *label* off the issue, now."""
        self._label_event(repo, number, label, actor_id, added=False)

    def add_comment(self, repo: str, number: int, author_id: int, body: str) -> int:
        """*author_id* comments *body* on the issue, now; the comment's id."""
        issue = self.issues[(repo, number)]
        comment = _Comment(self._id(), repo, number, author_id, body, self.now, self.now)
        self.comments[comment.id] = comment
        issue.updated_at = self.now
        return comment.id

    def delete_comment(self, comment_id: int) -> None:
        del self.comments[comment_id]

    def comment_body(self, comment_id: int) -> str | None:
        """What comment *comment_id* says now, or None when it is gone."""
        comment = self.comments.get(comment_id)
        return None if comment is None else comment.body

    def _label_event(self, repo: str, number: int, label: str, actor_id: int, added: bool) -> None:
        issue = self.issues[(repo, number)]
        if added and label not in issue.labels:
            issue.labels.append(label)
        if not added and label in issue.labels:
            issue.labels.remove(label)
        issue.updated_at = self.now
        self.label_events.append(
            _LabelEvent(self._id(), repo, number, label, actor_id, self.now, added)
        )

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id


def _time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _user(user_id: int) -> dict:
    return {"id": user_id, "login": f"user{user_id}", "username": f"user{user_id}"}


_API = "/api/v1"
_ISSUES = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues")
_ISSUE = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>\d+)")
_ISSUE_COMMENTS = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>\d+)/comments")
_TIMELINE = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>\d+)/timeline")
_COMMENT = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/comments/(?P<id>\d+)")


@dataclass
class StubGitea(StubForge):
    """A stub of Gitea's v1 API at ``https://gitea.example/api/v1``.

    It answers what the gateway asks: the issues of a repo by label and time,
    an issue, its comments and timeline, and one comment to read, create or
    edit. Each request must carry ``Authorization: token <token>``.
    """

    base_url: str = "https://gitea.example"

    def handle(self, request: httpx.Request) -> httpx.Response:
        seen = self.record(request)
        if seen.headers.get("authorization") != f"token {self.token}":
            return _error(401, "token is required")
        if not seen.path.startswith(_API):
            return _error(404, "not found")
        path = seen.path[len(_API):]
        routes = (
            ("GET", _ISSUES, self._list_issues),
            ("GET", _ISSUE, self._get_issue),
            ("GET", _ISSUE_COMMENTS, self._list_comments),
            ("POST", _ISSUE_COMMENTS, self._create_comment),
            ("GET", _TIMELINE, self._timeline),
            ("GET", _COMMENT, self._get_comment),
            ("PATCH", _COMMENT, self._edit_comment),
        )
        for method, pattern, answer in routes:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            if method != seen.method:
                continue
            if match["repo"] not in self.repos:
                return _error(404, "repo not found")
            return answer(seen, **match.groupdict())
        return _error(404 if seen.method == "GET" else 405, "no such route")

    # Routes.

    def _list_issues(self, seen: Seen, repo: str) -> httpx.Response:
        params = seen.params
        wanted = [name for name in params.get("labels", "").split(",") if name]
        since = _parse_time(params["since"]) if "since" in params else None
        state = params.get("state", "open")
        kind = params.get("type")
        found = [
            issue for issue in self.issues.values()
            if issue.repo == repo
            and all(name in issue.labels for name in wanted)
            and (since is None or issue.updated_at >= since)
            and (state == "all" or issue.state == state)
            and not (kind == "issues" and issue.pull)
            and not (kind == "pulls" and not issue.pull)
        ]
        found.sort(key=lambda issue: issue.updated_at, reverse=True)
        return self._page(seen, [self._issue_json(issue) for issue in found])

    def _get_issue(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        issue = self.issues.get((repo, int(number)))
        if issue is None:
            return _error(404, "issue not found")
        return _json(200, self._issue_json(issue))

    def _list_comments(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        if (repo, int(number)) not in self.issues:
            return _error(404, "issue not found")
        # Gitea gives an issue's comments all at once, oldest first.
        found = sorted(
            (c for c in self.comments.values() if (c.repo, c.number) == (repo, int(number))),
            key=lambda c: c.id,
        )
        return _json(200, [self._comment_json(c) for c in found])

    def _create_comment(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        if (repo, int(number)) not in self.issues:
            return _error(404, "issue not found")
        if not isinstance(seen.body, dict) or not isinstance(seen.body.get("body"), str):
            return _error(422, "body is required")
        comment_id = self.add_comment(repo, int(number), self.bot_id, seen.body["body"])
        return _json(201, self._comment_json(self.comments[comment_id]))

    def _timeline(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        if (repo, int(number)) not in self.issues:
            return _error(404, "issue not found")
        events: list[tuple[int, dict]] = []
        for comment in self.comments.values():
            if (comment.repo, comment.number) == (repo, int(number)):
                events.append((comment.id, {**self._comment_json(comment), "type": "comment"}))
        for event in self.label_events:
            if (event.repo, event.number) == (repo, int(number)):
                events.append((event.id, self._label_json(event)))
        events.sort(key=lambda pair: pair[0])
        return self._page(seen, [event for _, event in events])

    def _get_comment(self, seen: Seen, repo: str, id: str) -> httpx.Response:
        comment = self.comments.get(int(id))
        if comment is None or comment.repo != repo:
            return _error(404, "comment not found")
        return _json(200, self._comment_json(comment))

    def _edit_comment(self, seen: Seen, repo: str, id: str) -> httpx.Response:
        comment = self.comments.get(int(id))
        if comment is None or comment.repo != repo:
            return _error(404, "comment not found")
        if comment.author_id != self.bot_id:
            return _error(403, "not the poster")
        if not isinstance(seen.body, dict) or not isinstance(seen.body.get("body"), str):
            return _error(422, "body is required")
        comment.body = seen.body["body"]
        comment.updated_at = self.now
        return _json(200, self._comment_json(comment))

    # Shapes, as Gitea writes them.

    def _page(self, seen: Seen, items: list) -> httpx.Response:
        page = max(int(seen.params.get("page", 1)), 1)
        limit = min(int(seen.params.get("limit", self.max_items)), self.max_items)
        start = (page - 1) * limit
        return _json(200, items[start:start + limit], {"X-Total-Count": str(len(items))})

    def _issue_url(self, repo: str, number: int) -> str:
        return f"{self.base_url}/{repo}/issues/{number}"

    def _issue_json(self, issue: _Issue) -> dict:
        return {
            "id": 10000 + issue.number,
            "url": f"{self.base_url}{_API}/repos/{issue.repo}/issues/{issue.number}",
            "html_url": self._issue_url(issue.repo, issue.number),
            "number": issue.number,
            "user": _user(issue.author_id),
            "title": issue.title,
            "body": issue.body,
            "labels": [
                {"id": 300 + position, "name": name, "color": "e11d21"}
                for position, name in enumerate(issue.labels)
            ],
            "state": issue.state,
            "comments": sum(
                1 for c in self.comments.values() if (c.repo, c.number) == (issue.repo, issue.number)
            ),
            "created_at": _time(issue.created_at),
            "updated_at": _time(issue.updated_at),
            "pull_request": {"merged": False} if issue.pull else None,
            "repository": {"full_name": issue.repo},
        }

    def _comment_json(self, comment: _Comment) -> dict:
        issue_url = self._issue_url(comment.repo, comment.number)
        return {
            "id": comment.id,
            "html_url": f"{issue_url}#issuecomment-{comment.id}",
            "issue_url": issue_url,
            "pull_request_url": "",
            "user": _user(comment.author_id),
            "original_author": "",
            "original_author_id": 0,
            "body": comment.body,
            "assets": [],
            "created_at": _time(comment.created_at),
            "updated_at": _time(comment.updated_at),
        }

    def _label_json(self, event: _LabelEvent) -> dict:
        # Gitea writes a label event's body "1" when the label went on and
        # "" when it came off.
        return {
            "id": event.id,
            "type": "label",
            "html_url": f"{self._issue_url(event.repo, event.number)}#issuecomment-{event.id}",
            "issue_url": self._issue_url(event.repo, event.number),
            "user": _user(event.actor_id),
            "body": "1" if event.added else "",
            "label": {"id": 300, "name": event.label, "color": "e11d21"},
            "created_at": _time(event.at),
            "updated_at": _time(event.at),
        }


def _json(status: int, data: object, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


def _error(status: int, message: str) -> httpx.Response:
    return _json(status, {"message": message, "url": "https://gitea.example/api/swagger"})


#: The id of the GitHub App a :class:`StubGitHub` knows, and of its installation.
APP_ID = 99
INSTALLATION_ID = 4711

_GH_INSTALLATION = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/installation")
_GH_ACCESS_TOKENS = re.compile(r"/app/installations/(?P<id>\d+)/access_tokens")
_GH_REPO_EVENTS = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/events")
_GH_ISSUE_EVENTS = re.compile(r"/repos/(?P<repo>[^/]+/[^/]+)/issues/(?P<number>\d+)/events")


@functools.cache
def app_key() -> tuple[str, rsa.RSAPublicKey]:
    """A throwaway RSA key for a GitHub App: the private half in PEM, and the public half."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return pem, key.public_key()


@dataclass(frozen=True)
class Answer:
    """A list answer a :class:`StubGitHub` gave: the path, the status and the ETag."""

    path: str
    status: int
    etag: str


@dataclass
class StubGitHub(StubForge):
    """A stub of GitHub's REST API at ``https://api.github.com``, for the App :data:`APP_ID`.

    The App's JWT, checked against :func:`app_key`, finds the installation on
    a repo and asks for installation tokens; each token request's body is
    kept in ``token_bodies``, and the token handed out is ``token``. With that
    token the gateway reads the issues of a repo by label and time, an issue,
    its comments and its events, the repo's issue events feed, and one
    comment to read, create or edit. Lists are paged with a ``Link`` header,
    the repo's events feed newest first, and each list answer carries an
    ``ETag``: a request whose ``If-None-Match`` names the answer's ETag gets
    304 and no body, as GitHub answers. ``answers`` keeps each list answer.
    """

    base_url: str = "https://api.github.com"
    web_url: str = "https://github.com"
    token_bodies: list[dict] = field(default_factory=list)
    answers: list[Answer] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        seen = self.record(request)
        path = seen.path
        authorization = seen.headers.get("authorization", "")
        app_routes = (
            ("GET", _GH_INSTALLATION, self._installation),
            ("POST", _GH_ACCESS_TOKENS, self._access_token),
        )
        for method, pattern, answer in app_routes:
            match = pattern.fullmatch(path)
            if match is not None and method == seen.method:
                if not self._app_signed(authorization):
                    return _gh_error(401, "Bad credentials")
                return answer(seen, **match.groupdict())
        if authorization != f"token {self.token}":
            return _gh_error(401, "Bad credentials")
        routes = (
            ("GET", _GH_REPO_EVENTS, self._repo_events),
            ("GET", _ISSUES, self._list_issues),
            ("GET", _ISSUE, self._get_issue),
            ("GET", _ISSUE_COMMENTS, self._list_comments),
            ("POST", _ISSUE_COMMENTS, self._create_comment),
            ("GET", _GH_ISSUE_EVENTS, self._issue_events),
            ("GET", _COMMENT, self._get_comment),
            ("PATCH", _COMMENT, self._edit_comment),
        )
        for method, pattern, answer in routes:
            match = pattern.fullmatch(path)
            if match is None or method != seen.method:
                continue
            if match["repo"] not in self.repos:
                return _gh_error(404, "Not Found")
            return answer(seen, **match.groupdict())
        return _gh_error(404, "Not Found")

    def _app_signed(self, authorization: str) -> bool:
        scheme, _, token = authorization.partition(" ")
        if scheme != "Bearer":
            return False
        try:
            claims = jwt.decode(token, app_key()[1], algorithms=["RS256"])
        except jwt.PyJWTError:
            return False
        return claims.get("iss") == str(APP_ID)

    # App routes.

    def _installation(self, seen: Seen, repo: str) -> httpx.Response:
        if repo not in self.repos:
            return _gh_error(404, "Not Found")
        return _json(200, {"id": INSTALLATION_ID, "app_id": APP_ID})

    def _access_token(self, seen: Seen, id: str) -> httpx.Response:
        if int(id) != INSTALLATION_ID:
            return _gh_error(404, "Not Found")
        self.token_bodies.append(seen.body if isinstance(seen.body, dict) else {})
        # githubkit keeps the token until this time, so it is the real time
        # and not the stub's clock.
        expires = datetime.now(UTC) + timedelta(hours=1)
        return _json(201, {"token": self.token, "expires_at": _time(expires)})

    # Repo routes.

    def _repo_events(self, seen: Seen, repo: str) -> httpx.Response:
        events = sorted(
            (event for event in self.label_events if event.repo == repo),
            key=lambda event: event.id, reverse=True,
        )
        return self._page(seen, [self._event_json(event, with_issue=True) for event in events])

    def _list_issues(self, seen: Seen, repo: str) -> httpx.Response:
        params = seen.params
        wanted = [name for name in params.get("labels", "").split(",") if name]
        since = _parse_time(params["since"]) if "since" in params else None
        state = params.get("state", "open")
        # GitHub lists pull requests among the issues, sorted by creation time.
        found = [
            issue for issue in self.issues.values()
            if issue.repo == repo
            and all(name in issue.labels for name in wanted)
            and (since is None or issue.updated_at >= since)
            and (state == "all" or issue.state == state)
        ]
        found.sort(key=lambda issue: (issue.created_at, issue.number), reverse=True)
        return self._page(seen, [self._issue_json(issue) for issue in found])

    def _get_issue(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        issue = self.issues.get((repo, int(number)))
        if issue is None:
            return _gh_error(404, "Not Found")
        return _json(200, self._issue_json(issue))

    def _list_comments(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        if (repo, int(number)) not in self.issues:
            return _gh_error(404, "Not Found")
        found = sorted(
            (c for c in self.comments.values() if (c.repo, c.number) == (repo, int(number))),
            key=lambda c: c.id,
        )
        return self._page(seen, [self._comment_json(c) for c in found])

    def _create_comment(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        if (repo, int(number)) not in self.issues:
            return _gh_error(404, "Not Found")
        if not isinstance(seen.body, dict) or not isinstance(seen.body.get("body"), str):
            return _gh_error(422, "Validation Failed")
        comment_id = self.add_comment(repo, int(number), self.bot_id, seen.body["body"])
        return _json(201, self._comment_json(self.comments[comment_id]))

    def _issue_events(self, seen: Seen, repo: str, number: str) -> httpx.Response:
        if (repo, int(number)) not in self.issues:
            return _gh_error(404, "Not Found")
        events = sorted(
            (e for e in self.label_events if (e.repo, e.number) == (repo, int(number))),
            key=lambda event: event.id,
        )
        return self._page(seen, [self._event_json(event) for event in events])

    def _get_comment(self, seen: Seen, repo: str, id: str) -> httpx.Response:
        comment = self.comments.get(int(id))
        if comment is None or comment.repo != repo:
            return _gh_error(404, "Not Found")
        return _json(200, self._comment_json(comment))

    def _edit_comment(self, seen: Seen, repo: str, id: str) -> httpx.Response:
        comment = self.comments.get(int(id))
        if comment is None or comment.repo != repo:
            return _gh_error(404, "Not Found")
        if comment.author_id != self.bot_id:
            return _gh_error(403, "Must have admin rights to Repository.")
        if not isinstance(seen.body, dict) or not isinstance(seen.body.get("body"), str):
            return _gh_error(422, "Validation Failed")
        comment.body = seen.body["body"]
        comment.updated_at = self.now
        return _json(200, self._comment_json(comment))

    # Shapes, as GitHub writes them.

    def _page(self, seen: Seen, items: list) -> httpx.Response:
        page = max(int(seen.params.get("page", 1)), 1)
        limit = min(int(seen.params.get("per_page", self.max_items)), self.max_items)
        start = (page - 1) * limit
        body = items[start:start + limit]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        etag = f'W/"{digest[:32]}"'
        headers = {"ETag": etag}
        if start + limit < len(items):
            query = {**seen.params, "page": str(page + 1)}
            url = httpx.URL(self.base_url + seen.path, params=query)
            headers["Link"] = f'<{url}>; rel="next"'
        status = 304 if seen.headers.get("if-none-match") == etag else 200
        self.answers.append(Answer(seen.path, status, etag))
        if status == 304:
            return httpx.Response(304, headers=headers)
        return _json(200, body, headers)

    def _issue_url(self, repo: str, number: int) -> str:
        return f"{self.web_url}/{repo}/issues/{number}"

    def _issue_json(self, issue: _Issue) -> dict:
        api = f"{self.base_url}/repos/{issue.repo}/issues/{issue.number}"
        data = {
            "id": 10000 + issue.number,
            "node_id": f"I_{issue.number}",
            "url": api,
            "html_url": self._issue_url(issue.repo, issue.number),
            "number": issue.number,
            "state": issue.state,
            "title": issue.title,
            "body": issue.body,
            "user": _user(issue.author_id),
            "labels": [
                {"id": 300 + position, "name": name, "color": "e11d21"}
                for position, name in enumerate(issue.labels)
            ],
            "comments": sum(
                1 for c in self.comments.values()
                if (c.repo, c.number) == (issue.repo, issue.number)
            ),
            "created_at": _time(issue.created_at),
            "updated_at": _time(issue.updated_at),
            "closed_at": None,
        }
        if issue.pull:
            pulls = f"{self.base_url}/repos/{issue.repo}/pulls/{issue.number}"
            data["pull_request"] = {"url": pulls}
        return data

    def _comment_json(self, comment: _Comment) -> dict:
        issue_url = self._issue_url(comment.repo, comment.number)
        return {
            "id": comment.id,
            "node_id": f"IC_{comment.id}",
            "url": f"{self.base_url}/repos/{comment.repo}/issues/comments/{comment.id}",
            "html_url": f"{issue_url}#issuecomment-{comment.id}",
            "issue_url": f"{self.base_url}/repos/{comment.repo}/issues/{comment.number}",
            "user": _user(comment.author_id),
            "body": comment.body,
            "created_at": _time(comment.created_at),
            "updated_at": _time(comment.updated_at),
        }

    def _event_json(self, event: _LabelEvent, with_issue: bool = False) -> dict:
        data = {
            "id": event.id,
            "node_id": f"LE_{event.id}",
            "url": f"{self.base_url}/repos/{event.repo}/issues/events/{event.id}",
            "actor": _user(event.actor_id),
            "event": "labeled" if event.added else "unlabeled",
            "commit_id": None,
            "commit_url": None,
            "created_at": _time(event.at),
            "label": {"name": event.label, "color": "e11d21"},
        }
        if with_issue:
            data["issue"] = self._issue_json(self.issues[(event.repo, event.number)])
        return data


def _gh_error(status: int, message: str) -> httpx.Response:
    return _json(status, {
        "message": message,
        "documentation_url": "https://docs.github.com/rest",
        "status": str(status),
    })
