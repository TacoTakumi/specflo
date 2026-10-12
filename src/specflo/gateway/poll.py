"""Finding the label events that start a run: the trigger rule, and the Gitea and GitHub polls.

A run starts when the operator puts the trigger label on an issue. A label is
not authorization by itself, since anyone with triage access can label, so
the rule is on the immutable user id of who labelled:

- the event put the forge's trigger label on the issue, and it is the last
  event of that label on the issue, so the label it put on is there now;
- its actor is the forge's ``operator_id``;
- it is not older than the gateway's first start;
- the store has not handled it, or a newer event on that issue.

:func:`trigger_for` is that rule over the label events of one issue, from
whichever feed a forge gives them, and :class:`Trigger` is what it finds.

:func:`poll_gitea` polls a Gitea forge. For each watched repo it asks for the
open issues with the trigger label that changed at or after the repo's
cursor (the gateway's first start before the first change was seen), reads
each one's timeline for its label events, and moves the cursor to the newest
issue time it saw. Gitea times are whole seconds and the cursor is
inclusive, so the newest issue comes back at the next poll as well; the
store makes sure its event is not handled twice. A repo the forge refuses
is logged and skipped, and its cursor stays, so the next poll asks again.

GitHub has no list of labelled issues by change time, so :func:`poll_github`
reads each watched repo's issue events feed instead, back to the repo's
cursor (the gateway's first start before the first event was seen), groups
its label events by issue and applies the same rule to each issue. The
cursor moves to the newest event of the feed. The client asks the feed with
the ETag of its last answer, and a 304 (nothing changed) ends the poll of
that repo with no other request.

A poll finds triggers and changes nothing on the forge. Marking a trigger
handled, with :meth:`specflo.gateway.store.GatewayStore.mark_handled`, is
up to whoever acts on it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from .config import Forge
from .forge import ForgeClient, ForgeError, LabelEvent
from .store import GatewayStore

if TYPE_CHECKING:
    from .github import GitHubClient

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Trigger:
    """A label event that starts a run: on issue *number* of *repo* on the
    forge named *forge*, put on at *at* by the user *actor_id*."""

    forge: str
    repo: str
    number: int
    at: datetime
    actor_id: int


def trigger_for(
    forge: Forge, repo: str, number: int, events: Iterable[LabelEvent], store: GatewayStore
) -> Trigger | None:
    """The trigger in the label *events* of issue *number*, oldest first, or None."""
    last = None
    for event in events:
        if event.label == forge.label:
            last = event
    if last is None or not last.added or last.actor_id != forge.operator_id:
        return None
    if last.at < store.first_start:
        return None
    found = Trigger(forge=forge.name, repo=repo, number=number, at=last.at, actor_id=last.actor_id)
    return None if store.is_handled(found) else found


async def poll_gitea(forge: Forge, client: ForgeClient, store: GatewayStore) -> list[Trigger]:
    """One poll of every repo of the Gitea forge *forge* through *client*: the triggers found."""
    found: list[Trigger] = []
    for repo in forge.repos:
        try:
            found += await _poll_repo(forge, client, store, repo)
        except ForgeError as exc:
            # One repo the forge refuses, or that is gone, must not keep the
            # others from being polled.
            _log.warning("gateway poll of %s on forge '%s' failed: %s", repo, forge.name, exc)
    return found


async def _poll_repo(
    forge: Forge, client: ForgeClient, store: GatewayStore, repo: str
) -> list[Trigger]:
    since = store.since(forge.name, repo) or store.first_start
    issues = await client.labelled_issues(repo, forge.label, since)
    found: list[Trigger] = []
    for issue in sorted(issues, key=lambda issue: issue.number):
        events = await client.label_events(repo, issue.number)
        trigger = trigger_for(forge, repo, issue.number, events, store)
        if trigger is not None:
            found.append(trigger)
    if issues:
        store.set_since(forge.name, repo, max(issue.updated_at for issue in issues))
    return found


async def poll_github(forge: Forge, client: GitHubClient, store: GatewayStore) -> list[Trigger]:
    """One poll of every repo of the GitHub forge *forge* through *client*: the triggers found."""
    found: list[Trigger] = []
    for repo in forge.repos:
        try:
            found += await _poll_github_repo(forge, client, store, repo)
        except ForgeError as exc:
            _log.warning("gateway poll of %s on forge '%s' failed: %s", repo, forge.name, exc)
    return found


async def _poll_github_repo(
    forge: Forge, client: GitHubClient, store: GatewayStore, repo: str
) -> list[Trigger]:
    since = store.since(forge.name, repo) or store.first_start
    feed = await client.repo_label_events(repo, since)
    if feed is None:
        return []
    found: list[Trigger] = []
    for number, events in sorted(feed.events.items()):
        trigger = trigger_for(forge, repo, number, events, store)
        if trigger is not None:
            found.append(trigger)
    if feed.newest is not None and feed.newest > since:
        store.set_since(forge.name, repo, feed.newest)
    return found
