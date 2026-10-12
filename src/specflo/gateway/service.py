"""The gateway service: a research run for each trigger, and the one bot comment that tells of it.

:class:`GatewayService` is what the daemon's gateway loop calls at each poll
(its :meth:`~GatewayService.poll` is the loop's poll step). It polls every
configured forge through a client of that forge's kind and starts one run per
trigger the poll finds, each in an asyncio task of its own, so the runs of
different issues overlap and a poll never waits for a run. An issue has one
run at a time: a trigger on an issue whose run is in progress is ignored.
Either way the trigger is marked handled in the store, so it is not found
again.

A run reads the issue and its comments, fills a scratch dir with the cleaned
issue text (:func:`clean.build_input`), a clone of the issue's repo and,
when the service was given a wiki, the wiki configuration, and runs the
research agent there (:func:`run.research`). It tells the issue how it goes
in one comment of the bot's, edited at each step:

- while the pool has no member free, that the run waits for a free agent;
- once a member is leased, that research started, with the UTC time;
- at the end, the cleaned report (:func:`clean.clean_report`), or a short
  note that the run failed or ran out of time, with none of the agent's
  output in it. Why it failed goes to the daemon's log.

The comment's id is kept in the store, so a later run of the issue edits the
same comment. When that comment is gone, deleted on the forge, a new one is
posted and its id kept instead. A forge call for the comment that fails is
logged, and the run goes on.

Each forge kind is a :class:`ForgeKind`: how to make its client, how to poll
it, and the credential to clone its repos with. :data:`KINDS` holds the kinds
the service knows; a forge of a kind it does not know is skipped and logged.
The service keeps one client per forge for as long as it runs, since a run
goes on using it after the poll that started it. :meth:`~GatewayService.aclose`
cancels every run, waits for each to end and closes the clients; the daemon
calls it when it stops.

The wiki's URL and reader token come from the gateway configuration's wiki
section, read by the daemon: a service made without them writes no wiki
configuration.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..errors import SpecfloError
from ..pool.waiting import WAIT_MAX
from . import run, scratch
from .clean import build_input, clean_report
from .config import GITEA, Forge, GatewayConfig, read_credential
from .forge import ForgeClient, ForgeError
from .gitea import GiteaClient
from .poll import Trigger, poll_gitea
from .store import GatewayStore

__all__ = [
    "GITEA_KIND", "KINDS", "LEASE_WAIT", "WAITING_NOTE", "ForgeKind", "GatewayService",
    "clean_report", "failure_note", "started_note",
]

#: Seconds a run waits for a free agent before it gives up: as long as the
#: pool lets any request wait.
LEASE_WAIT = WAIT_MAX

#: What the bot comment says while the run waits for a free agent.
WAITING_NOTE = "Waiting for a free agent. Research starts when one is free."

_log = logging.getLogger(__name__)


def started_note(when: datetime) -> str:
    """What the bot comment says once the run's agent is leased at *when*."""
    return f"Research started at {_utc(when)}."


def failure_note(when: datetime, fault: Exception) -> str:
    """What the bot comment says when the run ended at *when* with *fault*.

    It names what kind of ending it was and never what the fault says, which
    may quote the agent or the forge.
    """
    if isinstance(fault, run.RunTimeout):
        what = f"Research did not finish in time and was stopped at {_utc(when)}."
    elif isinstance(fault, run.NoFreeAgent):
        what = f"No research agent became free; research gave up at {_utc(when)}."
    else:
        what = f"Research failed at {_utc(when)}."
    return f"{what} The cause is in the gateway's log. Label the issue again to retry."


@dataclass(frozen=True)
class ForgeKind:
    """What the service needs of one kind of forge.

    ``client`` makes a client of a forge; ``poll`` polls a forge through its
    client for triggers; ``clone_token`` is the credential to clone a repo of
    the forge with, or None for a clone that needs none.
    """

    client: Callable[[Forge], ForgeClient]
    poll: Callable[[Forge, ForgeClient, GatewayStore], Awaitable[list[Trigger]]]
    clone_token: Callable[[Forge, ForgeClient, str], Awaitable[str | None]]


async def _gitea_token(forge: Forge, client: ForgeClient, repo: str) -> str | None:
    # The bot's token, read afresh as each API call reads it.
    return None if forge.token_file is None else read_credential(forge.token_file)


GITEA_KIND = ForgeKind(client=GiteaClient, poll=poll_gitea, clone_token=_gitea_token)

#: The forge kinds the service knows, by the configuration's name for each.
KINDS: dict[str, ForgeKind] = {GITEA: GITEA_KIND}

#: How a clone is made: :func:`scratch.clone`'s signature.
Clone = Callable[..., Awaitable[None]]


class GatewayService:
    """The runs started by the triggers of the forges, with *pool* to lease
    research agents from and *store* to remember what was handled.

    *kinds* replaces :data:`KINDS`, *clone* the clone, and *clock* the time
    the comment names, for a test. *wiki_url* and *wiki_token* name the wiki
    the agent may read; without both, no wiki configuration is written.
    *limit* is the agent's time for its prompt, *lease_wait* the time a run
    waits for a free agent and *lease_retry* the pause between two asks.
    """

    def __init__(
        self,
        store: GatewayStore,
        pool: run.AgentPool,
        *,
        kinds: Mapping[str, ForgeKind] | None = None,
        wiki_url: str | None = None,
        wiki_token: str | None = None,
        clone: Clone = scratch.clone,
        limit: float = run.RUN_LIMIT,
        lease_wait: float = LEASE_WAIT,
        lease_retry: float = run.LEASE_RETRY,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.pool = pool
        self.kinds = dict(KINDS if kinds is None else kinds)
        self.wiki_url = wiki_url
        self.wiki_token = wiki_token
        self.clone = clone
        self.limit = limit
        self.lease_wait = lease_wait
        self.lease_retry = lease_retry
        self.clock = clock
        self._clients: dict[Forge, ForgeClient] = {}
        self._runs: dict[str, asyncio.Task] = {}
        self._unknown: set[str] = set()
        self._closed = False

    @property
    def runs(self) -> dict[str, asyncio.Task]:
        """The runs in progress, by the issue each is of."""
        return dict(self._runs)

    async def poll(self, config: GatewayConfig) -> None:
        """Poll every forge of *config* and start a run for each trigger found."""
        for forge in config.forges:
            kind = self.kinds.get(forge.kind)
            if kind is None:
                if forge.name not in self._unknown:
                    self._unknown.add(forge.name)
                    _log.warning(
                        "gateway: forge '%s' is of kind '%s', which the gateway cannot "
                        "poll yet; skipped", forge.name, forge.kind,
                    )
                continue
            try:
                client = self._client(forge, kind)
                triggers = await kind.poll(forge, client, self.store)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one forge must not keep the others from a poll
                _log.exception("gateway poll of forge '%s' failed", forge.name)
                continue
            for trigger in triggers:
                self._dispatch(forge, kind, client, trigger)

    async def join(self) -> None:
        """Wait until no run is in progress."""
        while self._runs:
            await asyncio.gather(*self._runs.values(), return_exceptions=True)

    async def aclose(self) -> None:
        """Cancel every run, wait for each to end, and close the forge clients."""
        self._closed = True
        tasks = list(self._runs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - the others are closed all the same
                _log.exception("a gateway forge client did not close cleanly")

    def _client(self, forge: Forge, kind: ForgeKind) -> ForgeClient:
        client = self._clients.get(forge)
        if client is None:
            client = self._clients[forge] = kind.client(forge)
        return client

    def _dispatch(self, forge: Forge, kind: ForgeKind, client: ForgeClient, trigger: Trigger) -> None:
        key = _where(trigger)
        self.store.mark_handled(trigger)
        if key in self._runs:
            _log.info("gateway: label on %s ignored, a run of it is in progress", key)
            return
        if self._closed:
            return
        task = asyncio.create_task(
            self._run(forge, kind, client, trigger), name=f"gateway run {key}"
        )
        self._runs[key] = task

        def done(finished: asyncio.Task) -> None:
            if self._runs.get(key) is finished:
                del self._runs[key]

        task.add_done_callback(done)

    async def _run(
        self, forge: Forge, kind: ForgeKind, client: ForgeClient, trigger: Trigger
    ) -> None:
        comment = _BotComment(self.store, client, trigger)
        try:
            report = await self._research(forge, kind, client, trigger, comment)
        except asyncio.CancelledError:
            raise
        except SpecfloError as exc:
            _log.warning("gateway: research on %s failed: %s", _where(trigger), exc)
            await comment.say(failure_note(self.clock(), exc))
            return
        except Exception as exc:  # noqa: BLE001 - the issue is told all the same
            _log.exception("gateway: research on %s failed", _where(trigger))
            await comment.say(failure_note(self.clock(), exc))
            return
        await comment.say(clean_report(report))

    async def _research(
        self,
        forge: Forge,
        kind: ForgeKind,
        client: ForgeClient,
        trigger: Trigger,
        comment: _BotComment,
    ) -> str:
        repo, number = trigger.repo, trigger.number
        issue = await client.issue(repo, number)
        comments = await client.comments(repo, number)
        text = build_input(
            issue.title, issue.body, [(c.author_id, c.body) for c in comments], forge.operator_id
        )
        async with scratch.scratch_dir() as directory:
            (directory / scratch.INPUT_FILE).write_text(text, encoding="utf-8")
            if self.wiki_url is not None and self.wiki_token is not None:
                scratch.write_wiki_config(directory, self.wiki_url, self.wiki_token)
            await self.clone(
                client.clone_url(repo), Path(directory) / scratch.CLONE_DIR,
                token=await kind.clone_token(forge, client, repo),
            )
            return await run.research(
                self.pool, forge, directory,
                limit=self.limit,
                label=f"{run.HOLDER_LABEL} {_where(trigger)}",
                wait=self.lease_wait,
                retry_every=self.lease_retry,
                on_waiting=lambda: comment.say(WAITING_NOTE),
                on_granted=lambda: comment.say(started_note(self.clock())),
            )


class _BotComment:
    """The bot's one comment on the issue of *trigger*, posted or edited."""

    def __init__(self, store: GatewayStore, client: ForgeClient, trigger: Trigger) -> None:
        self.store = store
        self.client = client
        self.trigger = trigger
        self.id: int | None = None

    async def say(self, body: str) -> None:
        """Make the comment say *body*. A forge that refuses is logged."""
        try:
            await self._say(body)
        except ForgeError as exc:
            _log.warning(
                "gateway: the comment on %s could not be written: %s", _where(self.trigger), exc
            )

    async def _say(self, body: str) -> None:
        trigger = self.trigger
        if self.id is None:
            # The comment of an earlier run, unless it was deleted since.
            known = self.store.comment_id(trigger.forge, trigger.repo, trigger.number)
            if known is not None and await self.client.find_comment(trigger.repo, known):
                self.id = known
        if self.id is not None:
            try:
                await self.client.edit_comment(trigger.repo, self.id, body)
                return
            except ForgeError as exc:
                # A comment deleted while the run went on is posted afresh.
                if exc.status != 404:
                    raise
        posted = await self.client.create_comment(trigger.repo, trigger.number, body)
        self.id = posted.id
        self.store.set_comment(trigger.forge, trigger.repo, trigger.number, posted.id)


def _where(trigger: Trigger) -> str:
    return f"{trigger.forge}:{trigger.repo}#{trigger.number}"


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
