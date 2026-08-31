"""Host core: sole holder of pi's stdio (REQ-01, REQ-02, REQ-04, REQ-05).

PiHost spawns the pi command (the real ``pi --mode rpc`` or the stub) with
both pipes attached, pumps every stdout frame into events.jsonl, and drives
the lifecycle state machine into status.json:

    starting -> idle -> working (agent_start) -> idle (agent_settled)
                                              -> exited (process exit)

Exit detection rides the stdout EOF of the reader thread, so a killed pi is
reflected well inside the 5 s bound (REQ-02). The host never interprets
assistant text (REQ-16) - it reacts only to protocol event types.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from specflo.agent.protocol import read_frames, write_frame
from specflo.agent.statefiles import (
    AgentPaths,
    EventLog,
    status_snapshot,
    write_status,
)


class PiHost:
    def __init__(
        self,
        name: str,
        pi_cmd: list[str],
        cwd: Path | str | None = None,
        base_dir: Path | str | None = None,
    ) -> None:
        self.name = name
        self.pi_cmd = list(pi_cmd)
        self.cwd = cwd
        self.paths = AgentPaths.resolve(name, base_dir).ensure()
        self.events = EventLog(self.paths.events)
        self.state = "starting"
        self.proc: subprocess.Popen | None = None
        self._stderr_f = None
        self._reader: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._stdin_lock = threading.Lock()

    def start(self) -> "PiHost":
        self._set_state("starting")
        self._stderr_f = open(self.paths.root / "pi-stderr.log", "ab")
        self.proc = subprocess.Popen(
            self.pi_cmd,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_f,
        )
        self._set_state("idle")
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return self

    def send(self, obj: Any) -> None:
        """Forward one command frame to pi's stdin."""
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("pi process is not running")
        with self._stdin_lock:
            write_frame(self.proc.stdin, obj)

    def close(self) -> None:
        """Tear down for tests/cleanup: kill pi if alive, drain, close files.

        Graceful stop semantics (abort in-flight run, escalation, stopped
        state - REQ-17) belong to the stop verb, not here.
        """
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        if self.proc is not None:
            self.proc.stdin.close()
            self.proc.stdout.close()
        if self._stderr_f is not None:
            self._stderr_f.close()
        self.events.close()

    # -- internals ----------------------------------------------------------

    def _set_state(self, state: str) -> None:
        with self._state_lock:
            self.state = state
            self.events.append({"type": "host_state", "state": state})
            write_status(
                self.paths.status,
                status_snapshot(
                    self.name,
                    state,
                    host_pid=os.getpid(),
                    pi_pid=self.proc.pid if self.proc else None,
                ),
            )

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for event in read_frames(self.proc.stdout):
                self.events.append(event)
                etype = event.get("type")
                if etype == "agent_start":
                    self._set_state("working")
                elif etype == "agent_settled":
                    self._set_state("idle")
        except Exception as exc:  # a malformed frame is a protocol violation
            self.events.append({"type": "host_error", "error": str(exc)})
        rc = self.proc.wait()
        self.events.append({"type": "process_exit", "exit_code": rc})
        self._set_state("exited")
