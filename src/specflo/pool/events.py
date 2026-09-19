"""Model reloads of leased members, read from llama-swap's event stream.

The pool keeps its own leases apart, but a client outside the pool may ask
llama-swap for a model that cannot run beside a leased member's model.
llama-swap then unloads the member's model, and loads it again on the
member's next turn: a slow turn, not a failure. The lease is not concerned,
and nothing here ends, changes or delays one. What the daemon does is read
llama-swap's ``GET /api/events`` and store each such reload against the
member, for the pool's pages to show.

The stream is server-sent events. Every message is an envelope,
``{"type", "data"}``, whose data is JSON text again. A ``modelStatus``
message is a whole snapshot: every configured model with its process state,
sent on each change of one. The other types (log data, activity, in-flight
requests, the interface's configuration, the profile) are passed over. No
message carries a time, so a reload's time is the reader's clock when the
snapshot arrived. The stream does not say for whom a model was unloaded
either: a model under lease that was ready, was unloaded and loads again is a
reload, whoever caused it. Only a lease that stood when the model was
unloaded, and still stands when it loads, has seen one; a model's first load
is not a reload, and a model under no lease stores nothing.

The framing, the snapshot and the question of which leased member a
snapshot concerns are plain functions and a tracker with no I/O, so a test
drives them with a fixture text and a fake clock. The reader is a thin loop
around them: one GET, held open, and after a failure or the end of the
stream a wait that doubles up to a minute. While no stream is being read the
store says reload data is unavailable. The reader sends llama-swap that one
GET and nothing else: no load, no unload, no profile.

Where llama-swap answers is not in the pool configuration, which names the
llama-swap configuration file and no address. It is llama-swap's own default
on this host unless the daemon's environment names another.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime, timezone

import httpx

from ..daemon.poolstore import Lease, PoolStore, Reload
from .config import Member, PoolConfig

EVENTS_PATH = "/api/events"
# Where llama-swap listens when it is told nothing else, and the environment
# variable that names another base URL.
DEFAULT_URL = "http://127.0.0.1:8080"
URL_ENV = "SPECFLO_LLAMA_SWAP_URL"

CONNECT_TIMEOUT = 10.0
# The wait after a failed or ended connection, in seconds: doubled after each
# one up to the longest, and back to the first once a connection is answered.
FIRST_DELAY = 1
MAX_DELAY = 60

MODEL_STATUS = "modelStatus"
READY = "ready"
LOADING = frozenset({"starting", READY})
UNLOADED = frozenset({"stopped", "stopping", "shutdown"})

# The time now, timezone-aware. Passed in, so a test hands over a fake one.
Clock = Callable[[], datetime]
# Each model under lease: its leased members, and the active leases on each.
Leased = Mapping[str, Mapping[str, set[str]]]

_log = logging.getLogger(__name__)


class Framer:
    """Server-sent events, a line at a time: ``feed`` gives a message's data
    at the blank line that ends it, and None for every other line."""

    def __init__(self) -> None:
        self._data: list[str] = []

    def feed(self, line: str) -> str | None:
        line = line.rstrip("\r\n")
        if line:
            # A comment starts with the colon; the event's name and the other
            # fields say nothing the envelope does not.
            name, _, value = line.partition(":")
            if name == "data":
                self._data.append(value.removeprefix(" "))
            return None
        if not self._data:
            return None
        data, self._data = "\n".join(self._data), []
        return data


def model_states(data: str) -> dict[str, str] | None:
    """Each model's state from one message's *data*; None for a message that
    is not a whole model snapshot."""
    try:
        envelope = json.loads(data)
        if not isinstance(envelope, dict) or envelope.get("type") != MODEL_STATUS:
            return None
        models = json.loads(envelope["data"])
        if not isinstance(models, list):
            return None
        return {str(model["id"]): str(model["state"]) for model in models}
    except (ValueError, KeyError, TypeError):
        return None


def leased_models(
    members: Iterable[Member], leases: Iterable[Lease]
) -> dict[str, dict[str, set[str]]]:
    """The models under lease: each local member's model by the active
    *leases* on that member. A hosted member's model is not llama-swap's."""
    models = {m.name: m.model for m in members if m.backing == "local" and m.model}
    leased: dict[str, dict[str, set[str]]] = {}
    for lease in leases:
        model = models.get(lease.member)
        if model is not None and lease.state == "active":
            leased.setdefault(model, {}).setdefault(lease.member, set()).add(lease.id)
    return leased


class ReloadTracker:
    """Which leased members a run of snapshots shows a reload on."""

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        # By model, the leases that stood when it was unloaded.
        self._unloaded: dict[str, set[str]] = {}

    def observe(self, states: Mapping[str, str], leased: Leased) -> list[tuple[str, str]]:
        """Take one snapshot; the reloads it shows, as (member, model)."""
        reloads: list[tuple[str, str]] = []
        for model, state in states.items():
            previous = self._states.get(model)
            members = leased.get(model, {})
            if state in UNLOADED and previous == READY:
                self._unloaded[model] = {lease for ids in members.values() for lease in ids}
            elif state in LOADING and model in self._unloaded:
                saw = self._unloaded.pop(model)
                reloads += [(member, model) for member, ids in members.items() if ids & saw]
        self._states = dict(states)
        return reloads


class Recorder:
    """The stream's lines in, reloads into the pool store.

    ``open_store`` opens the store for one unit of work. The leases are read
    and never written: a reload is a row of its own.
    """

    def __init__(
        self, config: PoolConfig, open_store: Callable[[], PoolStore], *, clock: Clock
    ) -> None:
        self.config = config
        self.open_store = open_store
        self.clock = clock
        self._framer = Framer()
        self._tracker = ReloadTracker()

    def line(self, line: str) -> list[Reload]:
        """Take one line of the stream; the reloads stored for it."""
        data = self._framer.feed(line)
        states = model_states(data) if data is not None else None
        if states is None:
            return []
        now = _text(self.clock())
        with self.open_store() as store:
            leased = leased_models(self.config.members, store.list_leases(state="active"))
            return [
                store.add_reload(Reload(id=0, member=member, model=model, time=now))
                for member, model in self._tracker.observe(states, leased)
            ]

    def connected(self) -> None:
        """A connection is answered: its framing starts clean of what a cut
        one left, and the store says reload data is available."""
        self._framer = Framer()
        self._available(True)

    def disconnected(self) -> None:
        """No stream is being read: the store says reload data is unavailable."""
        self._available(False)

    def _available(self, available: bool) -> None:
        with self.open_store() as store:
            store.set_reload_data_available(available)


class EventsReader:
    """Reads llama-swap's event stream at ``url`` into ``recorder`` until cancelled.

    ``transport`` and ``sleep`` are for a test to hand in; without them the
    reader connects over the network and waits on the event loop.
    """

    def __init__(
        self,
        recorder: Recorder,
        *,
        url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.recorder = recorder
        self.url = url
        self.transport = transport
        self.sleep = sleep

    async def run(self) -> None:
        delay = FIRST_DELAY
        reading = True
        # A quiet llama-swap sends nothing, so reading has no time limit.
        timeout = httpx.Timeout(CONNECT_TIMEOUT, read=None)
        try:
            async with httpx.AsyncClient(
                base_url=self.url, transport=self.transport, timeout=timeout
            ) as client:
                while True:
                    try:
                        async with client.stream("GET", EVENTS_PATH) as response:
                            response.raise_for_status()
                            delay, reading = FIRST_DELAY, True
                            self.recorder.connected()
                            async for line in response.aiter_lines():
                                self.recorder.line(line)
                        problem = "the stream ended"
                    # Whatever went wrong, the next connection is another try.
                    except Exception as exc:
                        problem = str(exc) or type(exc).__name__
                    if reading:
                        # said once for each outage, not once for each try
                        _log.warning("llama-swap events at %s: %s", self.url, problem)
                        reading = False
                    self.recorder.disconnected()
                    await self.sleep(delay)
                    delay = min(delay * 2, MAX_DELAY)
        finally:
            self.recorder.disconnected()


def reader_for(service, *, environ: Mapping[str, str] | None = None) -> EventsReader | None:
    """The reader for the pool *service*; None without a pool, and for a pool
    with no local member, whose models are not llama-swap's."""
    if service is None or not any(m.backing == "local" for m in service.config.members):
        return None
    environ = os.environ if environ is None else environ
    return EventsReader(
        Recorder(service.config, service.open_store, clock=service.clock),
        url=environ.get(URL_ENV) or DEFAULT_URL,
    )


def _text(time: datetime) -> str:
    """*time* as the pool store keeps times: ISO 8601 in UTC, to the millisecond."""
    return time.astimezone(timezone.utc).isoformat(timespec="milliseconds")
