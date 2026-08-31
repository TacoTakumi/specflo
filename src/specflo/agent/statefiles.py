"""Per-agent state directory layout and disk writers (REQ-04, REQ-05).

Each agent owns one directory derived from its name alone:

    <base>/<name>/
        sock          Unix domain socket (bound by the host, T-03)
        events.jsonl  append-only timestamped event log
        status.json   atomic point-in-time snapshot

<base> is ``$SPECFLO_AGENT_STATE_DIR`` when set, else ``~/.specflo/agents``.
The directory is persistent (not tmpfs): events.jsonl and status.json are
retained after stop (REQ-17). The socket filename is kept short because
AF_UNIX paths cap at ~108 bytes.

Stdlib only - the agent subsystem imports nothing from pipeline code (REQ-15).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from specflo.agent.protocol import encode_frame

ENV_STATE_DIR = "SPECFLO_AGENT_STATE_DIR"

LIFECYCLE_STATES = frozenset(
    {"starting", "idle", "working", "needs-attention", "exited", "stopped"}
)

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def now_iso() -> str:
    """UTC timestamp, ISO 8601 with millisecond precision."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def default_base_dir() -> Path:
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        return Path(env)
    return Path.home() / ".specflo" / "agents"


@dataclass(frozen=True)
class AgentPaths:
    """Filesystem layout for one agent, derived from its name alone."""

    name: str
    root: Path

    @classmethod
    def resolve(cls, name: str, base_dir: Path | str | None = None) -> "AgentPaths":
        if not _NAME_RE.match(name):
            raise ValueError(
                f"invalid agent name {name!r}: use letters, digits, '.', '_', '-'"
            )
        base = Path(base_dir) if base_dir is not None else default_base_dir()
        return cls(name=name, root=base / name)

    @property
    def socket(self) -> Path:
        return self.root / "sock"

    @property
    def events(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def status(self) -> Path:
        return self.root / "status.json"

    def ensure(self) -> "AgentPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        return self


class EventLog:
    """Append-only events.jsonl writer: one timestamped JSON object per line.

    Opened in append mode, so reopening across host restarts appends rather
    than truncates. Each record gets a ``ts`` field (write time, UTC) placed
    first; the event's own fields follow unchanged.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._f = open(self.path, "ab")

    def append(self, event: dict[str, Any]) -> None:
        record = {"ts": now_iso(), **event}
        self._f.write(encode_frame(record))
        self._f.flush()

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# The REQ-05 field set: every snapshot carries all of these keys, None when
# unknown. herdr ids are flat fields so consumers need no nested lookups.
def status_snapshot(
    name: str,
    state: str,
    *,
    host_pid: int | None = None,
    pi_pid: int | None = None,
    context_percent: float | None = None,
    herdr_workspace: str | None = None,
    herdr_tab: str | None = None,
    herdr_pane: str | None = None,
    last_activity: str | None = None,
) -> dict[str, Any]:
    if state not in LIFECYCLE_STATES:
        raise ValueError(
            f"invalid lifecycle state {state!r}: one of {sorted(LIFECYCLE_STATES)}"
        )
    return {
        "name": name,
        "state": state,
        "host_pid": host_pid,
        "pi_pid": pi_pid,
        "context_percent": context_percent,
        "herdr_workspace": herdr_workspace,
        "herdr_tab": herdr_tab,
        "herdr_pane": herdr_pane,
        "last_activity": last_activity if last_activity is not None else now_iso(),
    }


def write_status(path: Path | str, snapshot: dict[str, Any]) -> None:
    """Atomically replace status.json: write a sibling tmp file, then rename."""
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    data = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_status(path: Path | str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
