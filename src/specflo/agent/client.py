"""Client library for the per-agent Unix-socket control surface (REQ-03).

AgentClient is what the ``specflo agent`` CLI verbs reuse: it connects to a
host's socket, sends LF-JSONL command frames, and reads the interleaved
stream of correlated responses and broadcast events coming back.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import socket
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from specflo.agent.protocol import FrameDecoder, encode_frame
from specflo.agent.statefiles import AgentPaths


class HostUnreachableError(ConnectionError):
    """The per-agent socket is absent or refuses/loses the connection."""


def connect(
    name: str,
    base_dir: Path | str | None = None,
    connect_timeout: float = 5.0,
) -> "AgentClient":
    """Connect to the agent's socket, derived from its name alone."""
    paths = AgentPaths.resolve(name, base_dir)
    return AgentClient(paths.socket, connect_timeout=connect_timeout)


class AgentClient:
    def __init__(self, socket_path: Path | str, connect_timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(connect_timeout)
        try:
            self._sock.connect(str(self.socket_path))
        except OSError as exc:
            self._sock.close()
            raise HostUnreachableError(
                f"cannot connect to agent socket {self.socket_path}: {exc}"
            ) from exc
        self._decoder = FrameDecoder()
        self._pending: deque[Any] = deque()

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "AgentClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- frame I/O ----------------------------------------------------------

    def send(self, obj: dict[str, Any]) -> str:
        """Send one command frame, assigning a fresh id when absent.

        Returns the id the frame carries, for response correlation.
        """
        if "id" not in obj:
            obj = {"id": f"c-{uuid.uuid4().hex[:12]}", **obj}
        try:
            self._sock.sendall(encode_frame(obj))
        except OSError as exc:
            raise HostUnreachableError(f"send failed: {exc}") from exc
        return obj["id"]

    def read(self, timeout: float | None = 5.0) -> Any:
        """Return the next frame (response or broadcast event), FIFO.

        Raises TimeoutError when no frame arrives inside the deadline and
        HostUnreachableError when the host closes the connection.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._pending:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"no frame from {self.socket_path} within {timeout}s"
                    )
            self._sock.settimeout(remaining)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise TimeoutError(
                    f"no frame from {self.socket_path} within {timeout}s"
                ) from exc
            except OSError as exc:
                raise HostUnreachableError(f"recv failed: {exc}") from exc
            if not chunk:
                raise HostUnreachableError(
                    f"host closed the connection on {self.socket_path}"
                )
            self._pending.extend(self._decoder.feed(chunk))
        return self._pending.popleft()

    def request(self, obj: dict[str, Any], timeout: float | None = 5.0) -> Any:
        """Send a command and return its correlated response frame.

        Frames that arrive in between (broadcast events, other responses)
        stay queued for subsequent read() calls, in arrival order.
        """
        request_id = self.send(obj)
        deadline = None if timeout is None else time.monotonic() + timeout
        skipped: list[Any] = []
        try:
            while True:
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                frame = self.read(remaining)
                if (
                    isinstance(frame, dict)
                    and frame.get("type") == "response"
                    and frame.get("id") == request_id
                ):
                    return frame
                skipped.append(frame)
        finally:
            self._pending.extendleft(reversed(skipped))

    # -- conveniences for the host verbs ------------------------------------

    def status(self, timeout: float | None = 5.0) -> dict[str, Any]:
        response = self.request({"type": "status"}, timeout=timeout)
        if not response.get("success"):
            raise RuntimeError(f"status failed: {response.get('error')}")
        return response["data"]

    def stop(self, timeout: float | None = 5.0) -> Any:
        return self.request({"type": "stop"}, timeout=timeout)
