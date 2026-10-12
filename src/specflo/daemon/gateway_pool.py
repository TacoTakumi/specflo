"""The daemon's pool service as the issue gateway's :class:`AgentPool`.

A gateway run leases its research agent through
:class:`specflo.gateway.run.AgentPool`. :class:`ServicePool` is the daemon's:
it calls the pool service in this process and talks to the leased agent's
host over its socket, each in a worker thread, since both block and the
gateway runs on the daemon's event loop. It hands the run's environment to
the grant, which sets it in the member's environment on top of the
definition's allow list.

It lives in the daemon package because it drives the host through the agent
client, which the daemon may import and the gateway package may not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..agent.client import HostUnreachableError, connect
from ..errors import SpecfloError
from ..gateway.run import Leased, NoFreeAgent, RunError
from ..pool.service import NoFreeMember

#: Seconds the adapter waits for the agent host's answer to one request.
_REQUEST_TIMEOUT = 10.0


class ServicePool:
    """The daemon's pool service as a run's :class:`~specflo.gateway.run.AgentPool`.

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
        try:
            grant = await asyncio.to_thread(
                self.service.grant, pool, holder_label=label, cwd=cwd, env=dict(env)
            )
        except NoFreeMember as exc:
            raise NoFreeAgent(f"the pool '{pool}' gave no research agent: {exc}") from exc
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
