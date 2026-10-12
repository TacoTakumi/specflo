"""One research run: a lease from the pool, one prompt with a limit, and the report.

:func:`research` takes a scratch dir that :func:`scratch.prepare` filled. It
leases a member of the forge's pool with the scratch dir as the member's
working directory, sends it one prompt and waits for its run to settle, no
longer than ``RUN_LIMIT`` (half an hour unless the caller says otherwise).
The member works by the shipped ``rfc-researcher`` definition: it reads
``input.md`` and writes ``report.md`` beside it. Then the run reads
``report.md`` and returns its text. A report that is missing, empty, too
large or not a plain file fails the run with :class:`RunError`, as does a
pool that grants no member, an agent that fails or one that does not finish
in time.

The lease ends however the run ends: with the report, a failure, the limit or
the run's cancellation. An ending that fails is logged, since the lease has
ended all the same and the run's own outcome is what the caller acts on. A
run cancelled while its lease is being granted ends the lease as soon as the
grant comes back, so no member is left to a run that is gone.

What the member is handed is the scratch dir, its environment and the
prompt, and none of them holds a forge credential: the forge's clone has been
made already, with the credential in git's environment only. The environment
is :func:`scratch.wiki_environment`, which points ``awiki`` at the scratch
dir's wiki configuration and its reader token.

The pool is reached through :class:`AgentPool`, so a test hands in a stub.
:class:`ServicePool` is the daemon's: it calls the pool service in this
process and talks to the leased agent's host over its socket, each in a
worker thread, since both block and the gateway runs on the daemon's event
loop. The pool service takes no environment for one lease: what a member
starts with is the definition's allow list read from the daemon's own
environment. So the adapter cannot hand the wiki variable to the member, and
the prompt names it, with its value, for the agent to set on each ``awiki``
command.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..agent.client import HostUnreachableError, connect
from ..errors import SpecfloError
from . import scratch
from .config import Forge

#: The report the agent writes, in the scratch dir beside ``input.md``.
REPORT_FILE = "report.md"

#: Seconds the agent may take over its prompt before the run is cut off.
RUN_LIMIT = 30 * 60

#: The most bytes of a report read; a larger one fails the run.
REPORT_MAX = 1024 * 1024

#: Who holds a gateway lease, as the pool's records name it.
HOLDER_LABEL = "issue gateway"

#: Why a lease ended, as the pool records it.
FINISHED = "the research run finished"
FAILED = "the research run failed"
TIMED_OUT = "the research agent did not finish in time"
CANCELLED = "the research run was cancelled"

#: Seconds the adapter waits for the agent host's answer to one request.
_REQUEST_TIMEOUT = 10.0

_log = logging.getLogger(__name__)

# The endings that run on after their run was cancelled, held until they end.
_endings: set[asyncio.Task] = set()


class RunError(SpecfloError):
    """A research run that gave no report. The message names no credential."""


@dataclass(frozen=True)
class Leased:
    """A granted lease: its id, the agent to drive and the holder's token."""

    lease_id: str
    agent: str
    token: str = field(repr=False)


class AgentPool(Protocol):
    """The pool a run leases its agent from."""

    async def lease(
        self, pool: str, *, cwd: Path, label: str, env: Mapping[str, str]
    ) -> Leased:
        """Lease a member of *pool* whose agent works in *cwd* with *env* set.

        Raises :class:`RunError` when the pool grants none.
        """

    async def prompt(self, leased: Leased, text: str, *, timeout: float) -> None:
        """Send *text* to the agent of *leased* and wait, *timeout* seconds at
        most, for its run to settle. Raises :class:`RunError` when it fails."""

    async def end(self, leased: Leased, cause: str) -> None:
        """End *leased* for *cause*."""


def research_prompt(directory: Path, env: Mapping[str, str]) -> str:
    """The one prompt of a run in the scratch dir *directory*, with *env* named."""
    directory = Path(directory)
    lines = [
        f"Research the issue in {directory / scratch.INPUT_FILE}, as your instructions say,",
        f"and write your report to {directory / REPORT_FILE}.",
    ]
    if env:
        lines += [
            "",
            "Run every awiki command with these variables set in front of it, for example",
            "`VAR=value awiki search ...`:",
            *(f"- {name}={value}" for name, value in env.items()),
        ]
    return "\n".join(lines) + "\n"


async def research(
    pool: AgentPool,
    forge: Forge,
    directory: Path,
    *,
    limit: float = RUN_LIMIT,
    label: str = HOLDER_LABEL,
) -> str:
    """Run the research agent in the scratch dir *directory*; the text of its report.

    The agent is leased from *forge*'s pool under the holder label *label*
    and given *limit* seconds for its one prompt. Raises :class:`RunError`
    for a run that gave no report.
    """
    directory = Path(directory)
    env = scratch.wiki_environment(directory)
    leased = await _lease(pool, forge.pool, directory, label, env)
    cause = FAILED
    try:
        try:
            await asyncio.wait_for(
                pool.prompt(leased, research_prompt(directory, env), timeout=limit), limit
            )
        except TimeoutError:
            cause = TIMED_OUT
            raise RunError(f"the research agent did not finish in {limit:g} seconds.") from None
        except asyncio.CancelledError:
            cause = CANCELLED
            raise
        cause = FINISHED
    finally:
        await _end(pool, leased, cause)
    return read_report(directory)


def read_report(directory: Path) -> str:
    """The text of ``report.md`` in *directory*.

    The file is the agent's to write, so a link is not followed, and anything
    but a plain file of at most ``REPORT_MAX`` bytes with more than
    whitespace in it raises :class:`RunError`.
    """
    path = Path(directory) / REPORT_FILE
    try:
        # No link is followed, and a pipe in its place does not block the open.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise RunError(f"the research agent wrote no {REPORT_FILE}.") from None
    except OSError as exc:
        raise RunError(f"the agent's {REPORT_FILE} cannot be read ({exc.strerror}).") from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RunError(f"the agent's {REPORT_FILE} is not a regular file.")
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(REPORT_MAX + 1)
    if len(data) > REPORT_MAX:
        raise RunError(f"the agent's {REPORT_FILE} is larger than {REPORT_MAX} bytes.")
    text = data.decode("utf-8", "replace")
    if not text.strip():
        raise RunError(f"the agent's {REPORT_FILE} is empty.")
    return text


async def _lease(
    pool: AgentPool, name: str, directory: Path, label: str, env: Mapping[str, str]
) -> Leased:
    """The lease of a member of the pool *name*, which ends at once if the run
    is cancelled before the grant comes back."""
    granting = asyncio.ensure_future(pool.lease(name, cwd=directory, label=label, env=env))
    try:
        return await asyncio.shield(granting)
    except asyncio.CancelledError:
        granting.add_done_callback(lambda done: _end_granted(pool, done))
        raise
    except SpecfloError as exc:
        if isinstance(exc, RunError):
            raise
        raise RunError(f"the pool '{name}' gave no research agent: {exc}") from exc


def _end_granted(pool: AgentPool, granting: asyncio.Future) -> None:
    """End the lease a cancelled run's grant came back with, if it came back with one."""
    if granting.cancelled() or granting.exception() is not None:
        return
    _keep(asyncio.ensure_future(_end_logged(pool, granting.result(), CANCELLED)))


async def _end(pool: AgentPool, leased: Leased, cause: str) -> None:
    """End *leased*. The ending runs to its end also when the run is cancelled
    meanwhile, and a fault in it is logged."""
    ending = _keep(asyncio.ensure_future(_end_logged(pool, leased, cause)))
    await asyncio.shield(ending)


async def _end_logged(pool: AgentPool, leased: Leased, cause: str) -> None:
    try:
        await pool.end(leased, cause)
    except Exception:
        _log.exception("the research lease %s did not end cleanly", leased.lease_id)


def _keep(task: asyncio.Future) -> asyncio.Future:
    """Hold *task* until it is done, so that no ending is collected half way."""
    _endings.add(task)
    task.add_done_callback(_endings.discard)
    return task


class ServicePool:
    """The daemon's pool service as a run's :class:`AgentPool`.

    The service grants and ends leases in this process; the leased agent's
    host is driven over its socket with the lease's token, as ``specflo agent
    prompt`` drives it. Both block, so each runs in a worker thread. A wait
    that the run gives up on ends when the lease's ending stops the host or
    at its own limit, whichever is first.
    """

    def __init__(self, service: Any) -> None:
        self.service = service

    async def lease(
        self, pool: str, *, cwd: Path, label: str, env: Mapping[str, str]
    ) -> Leased:
        # The service starts a member with the definition's allow list read
        # from the daemon's environment and takes none for one lease, so
        # *env* is not handed over here: the run's prompt names it.
        try:
            grant = await asyncio.to_thread(
                self.service.grant, pool, holder_label=label, cwd=cwd
            )
        except SpecfloError as exc:
            raise RunError(f"the pool '{pool}' gave no research agent: {exc}") from exc
        return Leased(lease_id=grant.lease_id, agent=grant.agent, token=grant.token)

    async def prompt(self, leased: Leased, text: str, *, timeout: float) -> None:
        await asyncio.to_thread(_prompt_and_wait, leased, text, timeout)

    async def end(self, leased: Leased, cause: str) -> None:
        await asyncio.to_thread(self.service.end_lease, leased.lease_id, "released", cause=cause)


def _prompt_and_wait(leased: Leased, text: str, timeout: float) -> None:
    """Send *text* to the agent of *leased* and block until its run settles."""
    agent = leased.agent
    try:
        with connect(agent, connect_timeout=_REQUEST_TIMEOUT, lease_token=leased.token) as client:
            # The status frame carries the token, which opens the host's
            # event stream to this connection.
            status = client.request({"type": "status"}, timeout=_REQUEST_TIMEOUT)
            if not status.get("success"):
                raise RunError(f"the agent '{agent}' did not answer: {status.get('error')}")
            state = ((status.get("data") or {}).get("status") or {}).get("state")
            if state in ("exited", "stopped"):
                raise RunError(f"the agent '{agent}' is {state}; its pi does not run.")
            response = client.request({"type": "prompt", "message": text}, timeout=_REQUEST_TIMEOUT)
            if not response.get("success"):
                raise RunError(f"the agent '{agent}' refused the prompt: {response.get('error')}")
            failed: str | None = None

            def done(frame: object) -> bool:
                nonlocal failed
                if not isinstance(frame, dict):
                    return False
                if frame.get("type") == "turn_end":
                    # The last turn stands: pi retries some refusals itself.
                    failed = _turn_error(frame)
                return frame.get("type") in ("agent_settled", "process_exit")

            frame = client.read_until(done, timeout=timeout)
    except TimeoutError:
        raise RunError(f"the research agent did not finish in {timeout:g} seconds.") from None
    except HostUnreachableError as exc:
        raise RunError(f"the agent '{agent}' went away: {exc}") from None
    if frame.get("type") == "process_exit":
        raise RunError(f"the agent '{agent}': its pi exited before the run settled.")
    if failed is not None:
        raise RunError(f"the agent's turn ended in an error: {failed}")


def _turn_error(frame: dict) -> str | None:
    """Why the turn that *frame* ends went badly, or None when it ended well."""
    message = frame.get("message")
    if not isinstance(message, dict) or message.get("stopReason") != "error":
        return None
    said = message.get("errorMessage")
    return str(said) if said else "the turn ended in an error"
