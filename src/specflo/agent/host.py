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

The lease wall: a pool daemon binds its pool token (``pool_bind``), then
binds a holder token per lease (``lease_bind``) and clears it when the lease
ends (``lease_clear``). A host is one pool's: a bind of the token it has
succeeds and changes nothing, and any other token is refused. While a lease
is bound, only frames carrying the holder's ``lease_token`` or the daemon's
``pool_token`` may drive the member; any other is refused before it reaches
pi or the event log. A host that was never pool-bound has no wall and is a
plain pipe.

Between leases no lease is bound and a frame needs no token, which is how a
developer drives a console the pool leases out. The process of a console is
its developer's, so the pool says at ``lease_bind`` when the host is one, and
the holder of such a lease drives the pi but does not stop it: a ``stop`` that
only the holder's token lets in is refused, and the pool's token stops the
host as before. A host that runs on past a
lease still faces that lease's former holder, so it remembers the tokens of
the leases it has cleared, as digests and the newest ``ENDED_REMEMBERED`` of
them, and refuses a frame that carries one as its ``lease_token``, with a
lease bound or with none. The memory is this process's alone: a host that is
started again knows nothing of the leases before it.

The wall covers the way out as well: a member's output, pi's answers and the
holder's prompt text all travel in the broadcast. While a lease is bound, an
event goes only to a connection that has shown the holder's ``lease_token`` or
the ``pool_token`` on some frame of its own; it is sent from that frame on,
with nothing replayed. A connection that showed the token of an earlier lease
is a stranger to the next one; the pool token holds across leases. When the
lease is cleared every connection receives again, as on an unbound host.

The event log is one file that outlives every lease, and the wall cannot sit
in front of a file. So the host notes where the log ended when a lease was
bound, and its status answer to a frame that shows a bound credential carries
that offset as ``lease_log_start``: the ``log`` verb prints from there, so a
holder reads its own lease's part and nothing a former holder or the
developer said before it.

Lease activity: while a lease is bound, every command the holder's token lets
through moves ``last_activity`` in status.json to now, and so does a turn that
is running. The daemon reads a lease's idle time from that stamp, so nothing a
refused frame, a bare liveness probe or the daemon itself does may move it.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from specflo.agent.herdr import HerdrAdapter, HerdrError
from specflo.agent.policy import DialogPolicy
from specflo.agent.protocol import FrameDecoder, encode_frame, read_frames, write_frame
from specflo.agent.statefiles import (
    AgentPaths,
    EventLog,
    now_iso,
    read_status,
    status_snapshot,
    write_status,
)

HOST_VERBS = ("status", "stop")

# The pool daemon's verbs for raising and lowering the lease wall.
POOL_VERBS = ("pool_bind", "lease_bind", "lease_clear")

# Credential fields on a frame; pi never sees them once a pool is bound.
TOKEN_FIELDS = ("lease_token", "pool_token")

# How many cleared leases' tokens a host remembers, to turn their former
# holders away; the oldest is forgotten first.
ENDED_REMEMBERED = 256

# While a turn runs, pi's events refresh the activity stamp at most this often
# (seconds): the stamp is read against idle limits of minutes, and every
# refresh is an fsynced rewrite of status.json.
_ACTIVITY_REFRESH = 5.0

# Tells the specflo extension inside pi to serve no control socket of its own.
ENV_SERVE = "SPECFLO_AGENT_SERVE"

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


def _token_digest(token: Any) -> bytes | None:
    """Digest of a presented token; None when it is not a usable token."""
    if not isinstance(token, str) or not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).digest()


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
        clock: Callable[[], str] = now_iso,
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
        # per connection, the digests of the valid tokens it has shown
        self._shown: dict[socket.socket, set[bytes]] = {}
        self._subs_lock = threading.Lock()
        self._stopping = False
        self._stopped_evt = threading.Event()
        # the wall keeps digests only, so no token sits in host memory
        self._wall_lock = threading.Lock()
        self._pool_digest: bytes | None = None
        self._lease_digest: bytes | None = None
        # the bound lease is one on a developer's console
        self._lease_console = False
        # where the event log ended when the bound lease was bound
        self._lease_log_start: int | None = None
        # the leases cleared on this host, oldest first, each digest once
        self._ended: deque[bytes] = deque(maxlen=ENDED_REMEMBERED)
        self._clock = clock
        self._last_activity = clock()
        self._refreshed = time.monotonic()

    def start(self) -> "PiHost":
        self._set_state("starting")
        self._stderr_f = open(self.paths.root / "pi-stderr.log", "ab")
        self.proc = subprocess.Popen(
            self.pi_cmd,
            cwd=self.cwd,
            env=self._pi_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_f,
        )
        self._set_state("idle")
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        return self

    @staticmethod
    def _pi_env() -> dict[str, str]:
        """The environment pi starts with: this host's own, serving switched off.

        An rpc agent answers at its host's socket only. A pi that has the
        specflo extension would bind a second socket, named for its working
        directory, and that one knows no lease.
        """
        return {**os.environ, ENV_SERVE: "0"}

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
            self._shown.clear()
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
            self._write_status()
            self._push_herdr_state(state)

    def _write_status(self) -> None:
        """Snapshot the host into status.json, stamped now; state lock held."""
        self._last_activity = self._clock()
        self._refreshed = time.monotonic()
        write_status(
            self.paths.status,
            status_snapshot(
                self.name,
                self.state,
                host_pid=os.getpid(),
                pi_pid=self.proc.pid if self.proc else None,
                herdr_workspace=self.herdr_workspace,
                herdr_tab=self.herdr_tab,
                herdr_pane=self.herdr_pane,
                last_activity=self._last_activity,
            ),
        )

    def _stamp_activity(self) -> None:
        """The holder acted: move last_activity to now, the state untouched."""
        with self._state_lock:
            self._write_status()

    def _refresh_activity(self) -> None:
        """A running turn under a lease keeps the stamp fresh, throttled."""
        if self.state != "working" or self._lease_digest is None:
            return
        if time.monotonic() - self._refreshed < _ACTIVITY_REFRESH:
            return
        self._stamp_activity()

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
        with self._wall_lock:
            lease, pool = self._lease_digest, self._pool_digest
        with self._subs_lock:
            subscribers = list(self._subscribers.items())
            if lease is not None:
                # a lease is bound: only the holder's connections and the
                # daemon's may read the member. The check is made per event,
                # so a bystander falls silent the moment the lease is bound
                # and hears again the moment it is cleared.
                cleared = {lease, pool}
                subscribers = [
                    (conn, lock)
                    for conn, lock in subscribers
                    if not cleared.isdisjoint(self._shown.get(conn, ()))
                ]
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
            self._shown.pop(conn, None)
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
                self._refresh_activity()
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
                except socket.timeout:
                    # a broadcast set a send timeout on this socket; an idle
                    # client is not an error - keep waiting for its next frame
                    continue
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

        if ctype in POOL_VERBS:
            error = self._handle_pool_verb(request)
            # the daemon's connection reads the members it binds
            self._note_tokens(conn, request)
            if error is None:
                response.update(success=True)
            else:
                response.update(success=False, error=error)
            self._respond(conn, send_lock, response)
            return
        # before the frame can reach pi: its answer comes back in the broadcast
        self._note_tokens(conn, request)
        if ctype == "stop" and self._holders_stop_on_a_console(request):
            response.update(
                success=False,
                error=(
                    f"agent '{self.name}' is a developer's console and is not the "
                    "holder's to stop; release the lease to be done with it"
                ),
            )
            self._respond(conn, send_lock, response)
            return
        admitted = self._admitted_by(request)
        if admitted is None:
            response.update(
                success=False,
                error="lease held: a valid lease token is required",
            )
            self._respond(conn, send_lock, response)
            return
        if admitted == "lease_token":
            # only the holder renews its lease, not the daemon and not a probe
            self._stamp_activity()
        if self._pool_digest is not None:
            request = {k: v for k, v in request.items() if k not in TOKEN_FIELDS}

        if ctype == "status":
            data = self._status_data()
            if admitted != "open":
                # a credential was shown under a bound lease: the answer says
                # where that lease's part of the log begins. The bare probe
                # learns nothing of it.
                with self._wall_lock:
                    if self._lease_log_start is not None:
                        data["lease_log_start"] = self._lease_log_start
            response.update(success=True, data=data)
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

    def _handle_pool_verb(self, request: dict[str, Any]) -> str | None:
        """Apply one pool verb; return the refusal reason, None on success."""
        ctype = request["type"]
        pool = _token_digest(request.get("pool_token"))
        # the log lock first, as an event takes them: no event is written
        # between the bind and the note of where the log ended at it
        with self._log_lock, self._wall_lock:
            if ctype == "pool_bind":
                if self._pool_digest is not None:
                    # the bound pool may say so again, which changes nothing:
                    # whoever holds that token commands this host already
                    if pool is not None and hmac.compare_digest(pool, self._pool_digest):
                        return None
                    return "a pool is already bound to this host"
                if pool is None:
                    return "pool_bind needs a pool_token"
                self._pool_digest = pool
                return None
            if (
                self._pool_digest is None
                or pool is None
                or not hmac.compare_digest(pool, self._pool_digest)
            ):
                return f"{ctype} needs the bound pool's pool_token"
            if ctype == "lease_clear":
                if self._lease_digest is not None:
                    # its holder is a former one from now on
                    self._ended.append(self._lease_digest)
                self._lease_digest = None
                self._lease_console = False
                self._lease_log_start = None
                return None
            lease = _token_digest(request.get("lease_token"))
            if lease is None:
                return "lease_bind needs a lease_token"
            if self._lease_digest is not None:
                return "a lease is already bound; clear it first"
            if lease in self._ended:
                # the pool leases under this token again: it is no former one
                self._ended.remove(lease)
            self._lease_digest = lease
            self._lease_console = request.get("console") is True
            # the log outlives a lease: what came before is not this holder's
            self._lease_log_start = self.events.end()
            return None

    def _admitted_by(self, request: dict[str, Any]) -> str | None:
        """The wall: what lets this frame act on the member right now?

        The token field that matched, "open" when no credential was needed,
        None when the frame is refused.
        """
        with self._wall_lock:
            expected = {
                "lease_token": self._lease_digest,
                "pool_token": self._pool_digest,
            }
            ended = tuple(self._ended)
        former = _token_digest(request.get("lease_token"))
        if former is not None and any(hmac.compare_digest(former, e) for e in ended):
            # a lease that was cleared here opens nothing, with none bound
            # either: this host and its pi ran on past it
            return None
        if expected["lease_token"] is None:
            return "open"  # no lease bound, no wall
        presented = [f for f in TOKEN_FIELDS if f in request]
        if not presented:
            # the bare liveness probe stays open to everyone
            return "open" if request["type"] == "status" else None
        for field in presented:
            digest = _token_digest(request[field])
            if digest is not None and hmac.compare_digest(digest, expected[field]):
                return field
        return None

    def _holders_stop_on_a_console(self, request: dict[str, Any]) -> bool:
        """Does only the token of a lease on a console let this stop in?

        Asked before the wall is, and under one hold of its lock: a stop that
        arrives as the lease ends is then turned away here or by the wall, and
        never reaches a process that is the developer's.
        """
        with self._wall_lock:
            if not self._lease_console or self._lease_digest is None:
                return False
            lease, pool = self._lease_digest, self._pool_digest
        shown_pool = _token_digest(request.get("pool_token"))
        if shown_pool is not None and pool is not None and hmac.compare_digest(shown_pool, pool):
            return False
        shown = _token_digest(request.get("lease_token"))
        return shown is not None and hmac.compare_digest(shown, lease)

    def _note_tokens(self, conn: socket.socket, request: dict[str, Any]) -> None:
        """Remember which bound tokens this connection has shown.

        What is kept is the digest the token matched, never a yes or no, so a
        token of a lease that has ended opens nothing under the next one.
        """
        with self._wall_lock:
            expected = {
                "lease_token": self._lease_digest,
                "pool_token": self._pool_digest,
            }
        matched = set()
        for field in TOKEN_FIELDS:
            digest = _token_digest(request.get(field))
            if (
                digest is not None
                and expected[field] is not None
                and hmac.compare_digest(digest, expected[field])
            ):
                matched.add(digest)
        if not matched:
            return
        with self._subs_lock:
            if conn in self._subscribers:
                self._shown.setdefault(conn, set()).update(matched)

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
