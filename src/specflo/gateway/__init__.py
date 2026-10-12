"""The issue gateway: the daemon's link to the forges where issues are labelled.

When the operator labels an issue on a watched forge, the gateway sees it,
starts a research agent from the pool and posts the agent's cleaned report on
the issue. It is the only part of specflo that talks to a forge, and the agent
never holds a forge credential.

The gateway runs as a task of the daemon, and only on a root with a gateway
configuration (:mod:`specflo.gateway.config`). :class:`Gateway` is the loop of
that task: it runs one poll step at once and then once per ``poll_every``.
The step is whatever the daemon hands it, so a test hands it a stub. A step
that fails is logged and the loop goes on, since the next poll is another
try; cancelling the task, as the daemon does when it stops, ends the loop
wherever it is.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .config import GatewayConfig

#: One poll of every configured forge.
PollStep = Callable[[GatewayConfig], Awaitable[None]]

_log = logging.getLogger(__name__)


async def poll_nothing(config: GatewayConfig) -> None:
    """The daemon's poll step until the forge clients are built: it asks no forge."""


class Gateway:
    """The gateway's loop over *poll*, for the configuration *config*."""

    def __init__(
        self,
        config: GatewayConfig,
        poll: PollStep,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.poll = poll
        self.sleep = sleep

    async def run(self) -> None:
        while True:
            try:
                await self.poll(self.config)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one failed poll must not end the gateway
                # A forge that does not answer now may answer at the next poll.
                _log.exception("gateway poll failed")
            await self.sleep(self.config.poll_every)
