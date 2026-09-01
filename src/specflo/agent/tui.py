"""Managed TUI-transport start (pi-interactive-transport T-07, REQ-04).

Creates the herdr tab/pane in the agent space (workspace overridable per
start), launches the real interactive pi there with the env handshake the
extension's control module reads - agent name, managed flag, pane id, state
dir - and reports success only once the agent's control socket accepts a
connection. On timeout the pane is closed and the failure is distinct.

No host process exists on this path: pi itself, through the specflo
extension, binds the socket and writes the discovery record. This module
only places pi and waits for that to become true.

Stdlib only - the agent subsystem imports nothing from pipeline code.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

from specflo.agent.client import HostUnreachableError, connect
from specflo.agent.herdr import HerdrAdapter, HerdrError, HerdrPlacement
from specflo.agent.statefiles import ENV_STATE_DIR, AgentPaths

#: The handshake keys, exactly the names src/control/ reads.
ENV_AGENT_NAME = "SPECFLO_AGENT_NAME"
ENV_AGENT_MANAGED = "SPECFLO_AGENT_MANAGED"
ENV_AGENT_PANE = "SPECFLO_AGENT_PANE"

#: Start-timeout override, seconds; the CLI's default applies when unset.
ENV_START_TIMEOUT = "SPECFLO_AGENT_START_TIMEOUT"

#: The interactive pi the pane runs by default (the TUI, not the RPC broker).
DEFAULT_TUI_PI_CMD = "pi"

_PROBE_TIMEOUT = 2.0


class TuiStartError(Exception):
    """The TUI start failed before or at placement (generic failure)."""


class TuiStartTimeout(Exception):
    """The control socket never became connectable; the pane was cleaned up."""


def start_timeout(default: float) -> float:
    """The start deadline in seconds, honoring the env override."""
    value = os.environ.get(ENV_START_TIMEOUT)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def start_tui_agent(
    name: str,
    cwd: Path,
    adapter: HerdrAdapter,
    *,
    workspace: str | None,
    agent_space: str,
    pi_cmd: str = DEFAULT_TUI_PI_CMD,
    timeout: float = 15.0,
) -> HerdrPlacement:
    """Place pi in a pane and wait for its control socket; returns the placement."""
    paths = AgentPaths.resolve(name)
    if paths.socket.exists() and _socket_live(name):
        raise TuiStartError(
            f"agent '{name}' is already running (live socket on {paths.socket})"
        )

    workspace_id = workspace or adapter.ensure_workspace(agent_space)
    tab_env: dict[str, str] = {}
    if os.environ.get(ENV_STATE_DIR):
        tab_env[ENV_STATE_DIR] = os.environ[ENV_STATE_DIR]
    placement = adapter.create_tab(workspace_id, name, str(cwd), env=tab_env)

    handshake = {
        ENV_AGENT_NAME: name,
        ENV_AGENT_MANAGED: "1",
        ENV_AGENT_PANE: placement.pane_id,
        **tab_env,
    }
    assignments = " ".join(
        shlex.quote(f"{key}={value}") for key, value in handshake.items()
    )
    # exec: the pane dies with pi, and pi replaces the pane shell outright.
    adapter.run_in_pane(placement.pane_id, f"exec env {assignments} {pi_cmd}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _socket_live(name):
            return placement
        time.sleep(0.1)

    try:
        adapter.close_tab(placement.tab_id)
    except HerdrError:
        pass  # cleanup is best-effort; the timeout is the failure to report
    raise TuiStartTimeout(
        f"agent '{name}' did not become connectable in {timeout}s; pane closed"
    )


def _socket_live(name: str) -> bool:
    try:
        with connect(name, connect_timeout=_PROBE_TIMEOUT):
            return True
    except (HostUnreachableError, OSError):
        return False
