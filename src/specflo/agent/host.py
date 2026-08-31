"""Host core: sole holder of pi's stdio (REQ-01..REQ-05).

PiHost spawns the pi command (the real ``pi --mode rpc`` or the stub) with
both pipes attached, pumps every stdout frame into events.jsonl, and drives
the lifecycle state machine into status.json:

    starting -> idle -> working (agent_start) -> idle (agent_settled)
                                              -> exited (process exit)
                                              -> stopped (stop verb)

Exit detection rides the stdout EOF of the reader thread, so a killed pi is
reflected well inside the 5 s bound (REQ-02). The host never interprets
assistant text (REQ-16) - it reacts only to protocol event types.

``serve()`` adds the control surface (REQ-03): a per-agent Unix domain socket
speaking LF-delimited JSONL. Host verbs ``status`` and ``stop`` are answered
by the host with pi-shaped ``response`` frames; every other command type is
passed through to pi verbatim, and every event or response pi emits (plus the
host's own lifecycle lines) is broadcast to all connected clients, so
request/response correlation happens by the client's own ``id``.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from specflo.agent.herdr import HerdrAdapter, HerdrError
from specflo.agent.policy import DialogPolicy
from specflo.agent.protocol import FrameDecoder, encode_frame, read_frames, write_frame
from specflo.agent.statefiles import (
    AgentPaths,
    EventLog,
    read_status,
    status_snapshot,
    write_status,
)

HOST_VERBS = ("status", "stop")

# How long a broadcast send may block on one slow client before it is dropped.
_SUBSCRIBER_SEND_TIMEOUT = 5.0

# Host lifecycle -> herdr report state (REQ-13). stopped maps to None: the
# stop path releases the registration instead of reporting a state.
HERDR_STATE_FOR = {
    "starting": "unknown",
    "idle": "idle",
    "working": "working",
    "needs-attention": "blocked",
    "exited": "unknown",
    "stopped": None,
}


class PiHost:
    def __init__(
        self,
        name: str,
        pi_cmd: list[str],
        cwd: Path | str | None = None,
        base_dir: Path | str | None = None,
        grace: float = 5.0,
        policy: DialogPolicy | None = None,
        auto_answer: bool = True,
        herdr_pane: str | None = None,
        herdr_workspace: str | None = None,
        herdr_tab: str | None = None,
        transcript: Any | None = None,
    ) -> None:
        self.name = name
        self.pi_cmd = list(pi_cmd)
        self.cwd = cwd
        self.grace = grace
        self.policy = policy if policy is not None else DialogPolicy()
        self.auto_answer = auto_answer
        self._dialog_count = 0
        self._flooded = False
        self.herdr_pane = herdr_pane
        self.herdr_workspace = herdr_workspace
        self.herdr_tab = herdr_tab
        self._herdr = HerdrAdapter() if herdr_pane else None
        self._herdr_seq = 0
        self.transcript = transcript
        self.paths = AgentPaths.resolve(name, base_dir).ensure()
        self.events = EventLog(self.paths.events)
        self.state = "starting"
        self.proc: subprocess.Popen | None = None
        self._stderr_f = None
        self._reader: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._subscribers: dict[socket.socket, threading.Lock] = {}
        self._subs_lock = threading.Lock()
        self._stopping = False
        self._stopped_evt = threading.Event()

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

    # -- control surface (REQ-03) -------------------------------------------

    def serve(self) -> "PiHost":
        """Bind the per-agent Unix socket and accept clients in the background."""
        self.paths.socket.unlink(missing_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.paths.socket))
        self._listener.listen()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """Block until the stop verb has shut this host down."""
        return self._stopped_evt.wait(timeout)

    def close(self) -> None:
        """Tear down for tests/cleanup: kill pi if alive, drain, close files.

        Graceful stop semantics (abort in-flight run, bounded grace, herdr
        release - REQ-17) belong to the stop verb slice, not here.
        """
        self._close_listener()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        with self._subs_lock:
            conns = list(self._subscribers)
            self._subscribers.clear()
        for conn in conns:
            conn.close()
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
            self._log_event({"type": "host_state", "state": state})
            write_status(
                self.paths.status,
                status_snapshot(
                    self.name,
                    state,
                    host_pid=os.getpid(),
                    pi_pid=self.proc.pid if self.proc else None,
                    herdr_workspace=self.herdr_workspace,
                    herdr_tab=self.herdr_tab,
                    herdr_pane=self.herdr_pane,
                ),
            )
            self._push_herdr_state(state)

    def _push_herdr_state(self, state: str) -> None:
        """Report the lifecycle transition into herdr (REQ-13); never fatal."""
        if self._herdr is None:
            return
        herdr_state = HERDR_STATE_FOR.get(state)
        if herdr_state is None:
            return
        self._herdr_seq += 1
        try:
            self._herdr.report_state(
                self.herdr_pane, self.name, herdr_state, seq=self._herdr_seq
            )
        except HerdrError as exc:
            self._log_event({"type": "host_error", "error": f"herdr report: {exc}"})

    def _log_event(self, event: dict[str, Any]) -> None:
        # serializes appends and keeps disk and wire ordering identical
        # across the pump thread and connection threads
        with self._log_lock:
            self.events.append(event)
            self._broadcast(event)
            if self.transcript is not None:
                try:
                    self.transcript.render(event)
                except Exception:  # rendering must never disturb the host
                    pass

    def _broadcast(self, frame: dict[str, Any]) -> None:
        with self._subs_lock:
            subscribers = list(self._subscribers.items())
        data = encode_frame(frame)
        for conn, lock in subscribers:
            try:
                with lock:
                    conn.settimeout(_SUBSCRIBER_SEND_TIMEOUT)
                    conn.sendall(data)
            except OSError:
                self._drop_subscriber(conn)

    def _drop_subscriber(self, conn: socket.socket) -> None:
        with self._subs_lock:
            self._subscribers.pop(conn, None)
        conn.close()

    def _pump(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for event in read_frames(self.proc.stdout):
                self._log_event(event)
                etype = event.get("type")
                if etype == "agent_start":
                    self._dialog_count = 0
                    self._flooded = False
                    self._set_state("working")
                elif etype == "agent_settled":
                    self._set_state("idle")
                elif etype == "extension_ui_request":
                    self._handle_dialog(event)
        except Exception as exc:  # a malformed frame is a protocol violation
            self._log_event({"type": "host_error", "error": str(exc)})
        rc = self.proc.wait()
        self._log_event({"type": "process_exit", "exit_code": rc})
        if not self._stopping:
            self._set_state("exited")

    def _handle_dialog(self, request: dict[str, Any]) -> None:
        """Auto-answer one dialog by policy (REQ-10); enforce the flood cap."""
        if not self.auto_answer:
            return
        answer = self.policy.answer_for(request)
        if answer is None:
            return  # fire-and-forget method, nothing to answer
        self._dialog_count += 1
        if self._dialog_count > self.policy.flood_threshold:
            if not self._flooded:
                self._flooded = True
                self._log_event(
                    {"type": "host_dialog_flood", "count": self._dialog_count}
                )
                self._set_state("needs-attention")
            self._log_event(
                {
                    "type": "host_dialog_skipped",
                    "request_id": request.get("id"),
                    "method": request.get("method"),
                }
            )
            return
        try:
            self.send(answer)
        except (RuntimeError, OSError) as exc:
            self._log_event({"type": "host_error", "error": f"dialog answer: {exc}"})
            return
        self._log_event(
            {
                "type": "host_dialog_answer",
                "request_id": request.get("id"),
                "method": request.get("method"),
                "answer": {
                    k: v for k, v in answer.items() if k not in ("type", "id")
                },
            }
        )

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:  # listener closed
                return
            threading.Thread(
                target=self._serve_conn, args=(conn,), daemon=True
            ).start()

    def _serve_conn(self, conn: socket.socket) -> None:
        send_lock = threading.Lock()
        with self._subs_lock:
            self._subscribers[conn] = send_lock
        decoder = FrameDecoder()
        try:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                try:
                    frames = decoder.feed(chunk)
                except ValueError as exc:
                    self._respond(
                        conn,
                        send_lock,
                        {
                            "type": "response",
                            "command": "parse",
                            "success": False,
                            "error": f"failed to parse command: {exc}",
                        },
                    )
                    continue
                for request in frames:
                    self._handle_request(conn, send_lock, request)
        finally:
            self._drop_subscriber(conn)

    def _respond(
        self, conn: socket.socket, send_lock: threading.Lock, response: dict[str, Any]
    ) -> None:
        try:
            with send_lock:
                conn.settimeout(_SUBSCRIBER_SEND_TIMEOUT)
                conn.sendall(encode_frame(response))
        except OSError:
            self._drop_subscriber(conn)

    def _handle_request(
        self, conn: socket.socket, send_lock: threading.Lock, request: Any
    ) -> None:
        if not isinstance(request, dict) or not request.get("type"):
            self._respond(
                conn,
                send_lock,
                {
                    "type": "response",
                    "command": "parse",
                    "success": False,
                    "error": "command must be an object with a type",
                },
            )
            return
        ctype = request["type"]
        response: dict[str, Any] = {"type": "response", "command": ctype}
        if "id" in request:
            response["id"] = request["id"]

        if ctype == "status":
            response.update(success=True, data=self._status_data())
            self._respond(conn, send_lock, response)
        elif ctype == "stop":
            self._log_event({"type": "host_stop_requested"})
            response.update(success=True)
            self._respond(conn, send_lock, response)
            self._shutdown_from_stop()
        else:
            try:
                self.send(request)
            except (RuntimeError, OSError) as exc:
                response.update(success=False, error=str(exc))
                self._respond(conn, send_lock, response)
            else:
                if ctype in ("prompt", "steer", "follow_up"):
                    self._log_event(
                        {
                            "type": "host_forward",
                            "command": ctype,
                            "message": request.get("message"),
                        }
                    )
            # on success, pi's own response frame reaches the client via
            # the broadcast stream, correlated by the request's id

    def _status_data(self) -> dict[str, Any]:
        return {
            "status": read_status(self.paths.status),
            "paths": {
                "socket": str(self.paths.socket),
                "events": str(self.paths.events),
                "status": str(self.paths.status),
            },
        }

    def _shutdown_from_stop(self) -> None:
        """Graceful stop (REQ-17): abort any in-flight run and give it a
        bounded chance to settle, terminate pi escalating to kill after the
        grace period, land the final stopped state, stop serving. events.jsonl
        and status.json are retained on disk. (herdr release lands with the
        herdr slice.)
        """
        self._stopping = True
        if self.state == "working" and self.proc is not None and self.proc.poll() is None:
            try:
                self.send({"type": "abort"})
                self._log_event({"type": "host_abort_sent"})
            except (RuntimeError, OSError):
                pass
            deadline = time.monotonic() + self.grace
            while time.monotonic() < deadline and self.state == "working":
                time.sleep(0.05)
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=self.grace)
            except subprocess.TimeoutExpired:
                self._log_event({"type": "host_escalated_kill"})
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self._reader is not None:
            self._reader.join(timeout=5)
        self._set_state("stopped")
        if self._herdr is not None:
            try:
                self._herdr.release(self.herdr_pane, self.name)
            except HerdrError as exc:
                self._log_event(
                    {"type": "host_error", "error": f"herdr release: {exc}"}
                )
        self._close_listener()
        self._stopped_evt.set()

    def _close_listener(self) -> None:
        if self._listener is not None:
            # close() alone does not wake a thread blocked in accept();
            # shutdown() does, so the accept loop can actually exit
            try:
                self._listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._listener.close()
            if (
                self._accept_thread is not None
                and self._accept_thread is not threading.current_thread()
            ):
                self._accept_thread.join(timeout=5)
            self.paths.socket.unlink(missing_ok=True)
