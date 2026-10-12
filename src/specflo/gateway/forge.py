"""What the gateway asks of a forge, the same for each kind.

The gateway watches issues on Gitea and on GitHub, and asks both the same few
things: which issues with the trigger label changed since the last poll, what
an issue and its comments say, who put a label on it and when, and to post
and edit the bot's comment. :class:`ForgeClient` is that interface, and each
kind has a client that implements it (:mod:`specflo.gateway.gitea` for
Gitea). Every call is a coroutine, since the gateway runs on the daemon's
event loop.

A repo is named ``owner/name``, as the configuration writes it, and users by
their numeric id on the forge, since a login can be renamed. Every time is
timezone-aware. A failed call raises :class:`ForgeError`, which names the
call and the status and never a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from ..errors import SpecfloError


class ForgeError(SpecfloError):
    """A forge call that failed. ``status`` is the HTTP status the forge
    answered with, or None when it did not answer."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Issue:
    """An issue as the forge has it now. ``url`` is its page on the forge."""

    repo: str
    number: int
    title: str
    body: str
    author_id: int
    labels: tuple[str, ...]
    updated_at: datetime
    url: str


@dataclass(frozen=True)
class Comment:
    """A comment on an issue. ``url`` is its place on the issue's page."""

    id: int
    author_id: int
    body: str
    created_at: datetime
    updated_at: datetime
    url: str


@dataclass(frozen=True)
class LabelEvent:
    """A label put on an issue (``added``) or taken off it, by whom and when."""

    label: str
    actor_id: int
    at: datetime
    added: bool


@runtime_checkable
class ForgeClient(Protocol):
    """One forge, asked with the bot's credential."""

    async def labelled_issues(
        self, repo: str, label: str, since: datetime | None = None
    ) -> list[Issue]:
        """The open issues of *repo* with *label* that changed at or after *since*."""
        ...

    async def issue(self, repo: str, number: int) -> Issue:
        ...

    async def comments(self, repo: str, number: int) -> list[Comment]:
        """The comments on the issue, oldest first."""
        ...

    async def label_events(self, repo: str, number: int) -> list[LabelEvent]:
        """The label events of the issue's timeline, oldest first."""
        ...

    async def create_comment(self, repo: str, number: int, body: str) -> Comment:
        ...

    async def edit_comment(self, repo: str, comment_id: int, body: str) -> Comment:
        ...

    async def find_comment(self, repo: str, comment_id: int) -> Comment | None:
        """Comment *comment_id* of *repo*, or None when it was deleted."""
        ...

    def clone_url(self, repo: str) -> str:
        """The URL to clone *repo* from, with no credential in it."""
        ...

    async def aclose(self) -> None:
        ...
