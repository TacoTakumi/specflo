"""herdr adapter (REQ-11, REQ-14): placement and agent-state reporting.

A thin, mechanical wrapper over the ``herdr`` CLI, shaped by the T-09 probes:

- ``pane report-agent`` / ``pane release-agent`` take the pane id FIRST
  (the runtime usage line, not the --help synopsis, is authoritative).
- ``pane run <pane> <command>...`` types the command into the pane's shell;
  running the host via ``exec`` makes the pane die with it, which is what
  clears the herdr agent listing (release alone leaves the entry until the
  pane occupant exits).
- ``tab create`` returns the new tab and its root pane in one call.

Unavailability is a clean answer: ``available()`` is False when the binary is
missing or the server does not respond, and every action raises
HerdrUnavailableError instead of leaking subprocess noise (REQ-14's single
warning is the caller's job).

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

DEFAULT_SOURCE = "specflo-agent-host"

# herdr's accepted report states; callers map host lifecycle states onto these
HERDR_STATES = ("idle", "working", "blocked", "unknown")

_CALL_TIMEOUT = 10.0


class HerdrError(RuntimeError):
    """herdr answered with a failure."""


class HerdrUnavailableError(HerdrError):
    """herdr is not installed or its server is not reachable."""


@dataclass(frozen=True)
class HerdrPlacement:
    workspace_id: str
    tab_id: str
    pane_id: str


class HerdrAdapter:
    def __init__(self, binary: str = "herdr", source: str = DEFAULT_SOURCE) -> None:
        self.binary = binary
        self.source = source

    # -- plumbing -----------------------------------------------------------

    def _run(self, *args: str) -> dict[str, Any]:
        try:
            proc = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=_CALL_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise HerdrUnavailableError(f"herdr binary not found: {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrUnavailableError(f"herdr did not answer: {args[0]}") from exc
        if proc.returncode != 0:
            raise HerdrError(
                f"herdr {' '.join(args)} failed (rc {proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        if not proc.stdout.strip():
            return {}
        try:
            return json.loads(proc.stdout).get("result", {})
        except json.JSONDecodeError:
            return {}

    # -- surface ------------------------------------------------------------

    def available(self) -> bool:
        """True when the binary exists and the server answers."""
        try:
            self._run("workspace", "list")
            return True
        except HerdrError:
            return False

    def ensure_workspace(self, label: str) -> str:
        """Return the id of the workspace with this label, creating it if absent."""
        result = self._run("workspace", "list")
        for workspace in result.get("workspaces", []):
            if workspace.get("label") == label:
                return workspace["workspace_id"]
        created = self._run("workspace", "create", "--label", label, "--no-focus")
        return created["workspace"]["workspace_id"]

    def create_tab(
        self, workspace_id: str, label: str, cwd: str
    ) -> HerdrPlacement:
        """Create a labeled tab in the workspace; return its ids and root pane."""
        result = self._run(
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--label",
            label,
            "--cwd",
            cwd,
            "--no-focus",
        )
        return HerdrPlacement(
            workspace_id=workspace_id,
            tab_id=result["tab"]["tab_id"],
            pane_id=result["root_pane"]["pane_id"],
        )

    def run_in_pane(self, pane_id: str, command: str) -> None:
        """Execute a shell command inside the pane (typed into its shell)."""
        self._run("pane", "run", pane_id, command)

    def report_state(
        self,
        pane_id: str,
        agent: str,
        state: str,
        seq: int | None = None,
    ) -> None:
        """Push agent lifecycle state for the pane (pane id first - T-09)."""
        if state not in HERDR_STATES:
            raise ValueError(f"invalid herdr state {state!r}: one of {HERDR_STATES}")
        args = [
            "pane",
            "report-agent",
            pane_id,
            "--source",
            self.source,
            "--agent",
            agent,
            "--state",
            state,
        ]
        if seq is not None:
            args += ["--seq", str(seq)]
        self._run(*args)

    def release(self, pane_id: str, agent: str) -> None:
        """Relinquish lifecycle authority for the pane."""
        self._run(
            "pane",
            "release-agent",
            pane_id,
            "--source",
            self.source,
            "--agent",
            agent,
        )

    def close_tab(self, tab_id: str) -> None:
        self._run("tab", "close", tab_id)
