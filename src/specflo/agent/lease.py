"""The holder's side of a lease: finding its token, and why a lease ended.

A pooled member's host lets only the lease holder drive it. The ``specflo
agent`` verbs find the holder's token in one of three places, first hit wins:

    --lease-token                       the verb's own option
    $SPECFLO_LEASE_TOKEN                the environment
    .specflo/leases/<agent>.token       found upward from the working directory

When a lease ends the pool stops the member's host and leaves one record in
the agent's state directory, beside status.json:

    <base>/<name>/lease-ended.json
        {"cause": "released" | "expired" | "preempted",
         "request_id": <the preempting request's id, else null>,
         "ended_at": <UTC timestamp>}

so the former holder's next verb can name the cause instead of reporting a
host that is merely unreachable. The pool writes the record (``write_ended``)
and clears it when it starts the member for a new lease (``clear_ended``).

Stdlib only, and nothing from the rest of the agent subsystem: this module
knows files and strings, not the host's socket and not pi.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ENV_LEASE_TOKEN = "SPECFLO_LEASE_TOKEN"

#: Where the lease verb stores a token, relative to the client root.
TOKEN_DIR = Path(".specflo") / "leases"

ENDED_FILE = "lease-ended.json"

ENDED_CAUSES = ("released", "expired", "preempted")

# How the host words its refusal of a frame the wall turned away.
_WALL_REFUSAL = "lease held"


# -- the token --------------------------------------------------------------


def token_file(root: Path | str, agent: str) -> Path:
    """The token file for one agent under a client root."""
    return Path(root) / TOKEN_DIR / f"{agent}.token"


def find_token(
    agent: str,
    option: str | None = None,
    environ: Mapping[str, str] | None = None,
    start: Path | str | None = None,
) -> str | None:
    """The lease token for this agent, None when there is none to present."""
    if option:
        return option
    environ = os.environ if environ is None else environ
    if environ.get(ENV_LEASE_TOKEN):
        return environ[ENV_LEASE_TOKEN]
    if not agent or Path(agent).name != agent:
        return None  # a name with a path in it names no token file
    here = Path(start) if start is not None else Path.cwd()
    for directory in (here, *here.parents):
        candidate = token_file(directory, agent)
        if candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
    return None


def is_wall_refusal(error: Any) -> bool:
    """Did the host refuse a frame because a lease is held by someone else?"""
    return isinstance(error, str) and error.startswith(_WALL_REFUSAL)


# -- the ended record -------------------------------------------------------


def write_ended(
    state_dir: Path | str,
    cause: str,
    request_id: str | None = None,
    ended_at: str | None = None,
) -> Path:
    """Record why the lease on this agent ended; atomic, replaces any earlier."""
    if cause not in ENDED_CAUSES:
        raise ValueError(f"invalid lease end cause {cause!r}: one of {ENDED_CAUSES}")
    if cause == "preempted" and not request_id:
        raise ValueError("a preempted lease records the preempting request's id")
    if ended_at is None:
        ended_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    record = {"cause": cause, "request_id": request_id, "ended_at": ended_at}
    path = Path(state_dir) / ENDED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def read_ended(state_dir: Path | str) -> dict[str, Any] | None:
    """The ended record, None when absent or not one this module can report."""
    try:
        with open(Path(state_dir) / ENDED_FILE, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("cause") not in ENDED_CAUSES:
        return None
    return record


def clear_ended(state_dir: Path | str) -> None:
    """Forget the last ending: the member is starting under a new lease."""
    (Path(state_dir) / ENDED_FILE).unlink(missing_ok=True)


def ended_message(record: Mapping[str, Any]) -> str:
    """The cause as the former holder reads it."""
    cause = record["cause"]
    if cause == "preempted":
        return f"lease preempted by {record.get('request_id')}"
    return f"lease {cause}"
